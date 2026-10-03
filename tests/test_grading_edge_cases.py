"""Grading must cope with postponed games and games the bot never saw end."""

import asyncio
import time
from datetime import datetime, timezone

from sportsbot.bot import SportsBot
from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.parlays import EXPIRE_SECONDS, settle
from sportsbot.props import Leg
from sportsbot.research import Lean
from sportsbot.settings import SettingsStore, StateStore
from sportsbot.storage import SubscriptionStore


def game(state, name, home=0, away=0, detail="Final"):
    comp = {"status": {"type": {"state": state, "name": name, "shortDetail": detail}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "1", "abbreviation": "KC", "displayName": "Kansas City Royals"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "2", "abbreviation": "MIL", "displayName": "Milwaukee Brewers"}}]}
    [g] = parse_scoreboard({"events": [{"id": "9", "date": "", "competitions": [comp]}]}, LEAGUES["mlb"])
    return g


def make_bot(tmp_path):
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 10, None, SettingsStore(tmp_path / "settings.json"),
                    StateStore(tmp_path / "state.json"))
    sent = []

    async def send(cid, embed=None, content=None):
        sent.append(embed)
    bot._send = send
    return bot, sent


ML = Leg("Kansas City Royals Moneyline", 0.6, "", "MIL @ KC", "9", None, "moneyline", None, None, "1", "mlb", "baseball/mlb")
OVER = Lean("total", "Over 8.5", "over", 8.5, None, "Low")


def test_postponed_game_voids_parlay_legs(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.parlays.record(1, "mlb", "Safest", [ML])
    bot.latest["mlb"] = [game("post", "STATUS_POSTPONED", detail="Postponed")]
    asyncio.run(settle(bot, bot.parlays))
    assert sent[0].title == "🎟️ ➖ Parlay void: 0/0 legs hit" and "Postponed" in sent[0].description


def test_lean_graded_even_if_the_bot_never_saw_the_game_end(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.leans.record(game("pre", "STATUS_SCHEDULED"), [OVER])
    bot.latest["mlb"] = [game("post", "STATUS_FINAL", 6, 5)]  # first seen already final (e.g. after a restart)
    asyncio.run(bot.leans.settle_pending(bot))
    assert bot.leans.pending() == 0 and bot.leans.summary()["all"]["win"] == 1


def test_lean_on_postponed_game_is_voided_and_not_counted(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.leans.record(game("pre", "STATUS_SCHEDULED"), [OVER])
    bot.latest["mlb"] = [game("post", "STATUS_POSTPONED", detail="Postponed")]
    asyncio.run(bot.leans.settle_pending(bot))
    assert bot.leans.pending() == 0 and bot.leans.summary() == {} and bot.leans.pending_leagues() == set()


def test_game_off_the_scoreboard_is_looked_up_directly(tmp_path):
    bot, _ = make_bot(tmp_path)
    bot.leans.record(game("pre", "STATUS_SCHEDULED"), [OVER])

    async def summary(path, event_id):
        return {"header": {"competitions": [{"status": {"type": {"state": "post", "name": "STATUS_FINAL"}},
                                             "competitors": [{"homeAway": "home", "score": "2", "team": {"id": "1"}},
                                                             {"homeAway": "away", "score": "3", "team": {"id": "2"}}]}]}}
    bot.espn.summary = summary
    asyncio.run(bot.leans.settle_pending(bot))
    assert bot.leans.summary()["all"]["loss"] == 1  # 5 runs, under 8.5


def test_unresolved_leans_and_parlays_expire_after_a_week(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.leans.record(game("pre", "STATUS_SCHEDULED"), [OVER])
    bot.parlays.record(1, "mlb", "Safest", [ML])
    old = time.time() - EXPIRE_SECONDS - 60
    for key, lean in bot.state.items("leans"):
        bot.state.set("leans", key, {**lean, "at": old})
    for key, p in bot.state.items("parlays"):
        bot.state.set("parlays", key, {**p, "created": old})

    async def never_final(path, event_id):
        return {"header": {"competitions": [{"status": {"type": {"state": "pre"}}}]}}
    bot.espn.summary = never_final
    asyncio.run(bot.leans.settle_pending(bot))
    asyncio.run(settle(bot, bot.parlays))
    assert bot.leans.pending() == 0 and bot.parlays.pending() == [] and "never settled" in sent[0].description


def test_games_off_the_scoreboard_are_not_looked_up_every_minute(tmp_path, monkeypatch):
    import sportsbot.parlays as parlays
    bot, _ = make_bot(tmp_path)
    bot.leans.record(game("pre", "STATUS_SCHEDULED"), [OVER])
    clock = [1_000_000.0]
    monkeypatch.setattr(parlays.time, "time", lambda: clock[0])
    starts_in_two_hours = datetime.fromtimestamp(clock[0] + 7200, timezone.utc).isoformat()
    calls = []
    status = {"state": "pre"}

    async def summary(path, event_id):
        calls.append(clock[0])
        return {"header": {"competitions": [{"date": starts_in_two_hours, "status": {"type": status},
                                             "competitors": [{"homeAway": "home", "score": "2", "team": {"id": "1"}},
                                                             {"homeAway": "away", "score": "3", "team": {"id": "2"}}]}]}}
    bot.espn.summary = summary

    def check(minutes_later):
        clock[0] += minutes_later * 60
        asyncio.run(bot.leans.settle_pending(bot))
    check(0)
    for _ in range(100):  # every minute until the game starts: no more lookups
        check(1)
    assert len(calls) == 1
    check(25)  # it has started: look again, then every 10 minutes
    status.update(state="in")
    for _ in range(9):
        check(1)
    assert len(calls) == 2
    status.update(state="post", name="STATUS_FINAL")
    check(1)
    assert len(calls) == 3 and bot.leans.summary()["all"]["loss"] == 1
