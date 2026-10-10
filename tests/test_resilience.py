"""The update loop must keep posting no matter what goes wrong in one league or step."""

import asyncio
import logging
import tempfile
from pathlib import Path

import sportsbot.bot as botmod
from sportsbot.bot import SportsBot
from sportsbot.storage import SubscriptionStore


def make_bot():
    bot = SportsBot(SubscriptionStore(Path(tempfile.mkdtemp()) / "subscriptions.json"), 10, None)
    bot.wait_until_ready = lambda: asyncio.sleep(0)
    bot.poll.change_interval(seconds=0.02)
    return bot


async def run_for(bot, seconds):
    bot.poll.start()
    await asyncio.sleep(seconds)
    running = bot.poll.is_running()
    bot.poll.cancel()
    await bot.espn.close()
    return running


def test_one_broken_league_does_not_stop_the_others(caplog, monkeypatch):
    import sportsbot.bot
    monkeypatch.setattr(sportsbot.bot, "IDLE_POLL_SECONDS", 0)  # check even idle leagues every cycle here
    caplog.set_level(logging.CRITICAL)
    bot = make_bot()
    bot.store.add(1, "nfl")
    bot.store.add(1, "epl")
    checks = {"nfl": 0}

    async def scoreboard(league, date=None):
        if league.key == "epl":
            return [object()]  # unexpected data
        checks["nfl"] += 1
        return []
    bot.espn.scoreboard = scoreboard
    assert asyncio.run(run_for(bot, 0.3)) is True
    assert checks["nfl"] >= 5


def test_every_step_is_isolated(caplog):
    caplog.set_level(logging.CRITICAL)
    bot = make_bot()
    steps = {"after": 0}

    async def boom(*a, **k):
        raise RuntimeError("broken step")

    async def counted():
        steps["after"] += 1
    bot._refresh_boards = boom
    bot._send_reminders = boom
    bot._post_daily_schedules = counted  # still runs after the failing steps
    assert asyncio.run(run_for(bot, 0.2)) is True
    assert steps["after"] >= 3


def test_a_failing_cycle_is_retried_next_cycle(caplog):
    caplog.set_level(logging.CRITICAL)
    bot = make_bot()
    calls = {"n": 0}
    real = bot.store.leagues

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("unexpected")  # outside any per-step guard
        return real()
    bot.store.leagues = flaky
    assert asyncio.run(run_for(bot, 0.3)) is True
    assert calls["n"] > 3  # kept going after the failure


def test_loop_restarts_itself_if_it_ever_dies(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="sportsbot")
    monkeypatch.setattr(botmod, "LOOP_RESTART_SECONDS", 0.05)
    bot = make_bot()
    calls = {"n": 0}

    async def body(self):  # replaces the guarded loop body, so the error really escapes
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("escaped everything")
    bot.poll.coro = body
    assert asyncio.run(run_for(bot, 0.5)) is True
    assert any("Update loop crashed" in r.getMessage() for r in caplog.records)
    assert calls["n"] > 3  # restarted and kept going after dying


def test_live_game_that_drops_off_the_scoreboard_still_gets_its_final(tmp_path):
    from sportsbot.espn import parse_scoreboard
    from sportsbot.tracker import FINAL

    def nhl(state, name, home, away, gid="401", date="2026-10-03T02:00Z"):
        comp = {"status": {"type": {"state": state, "name": name, "shortDetail": "Final"}},
                "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "1", "abbreviation": "VAN", "displayName": "Vancouver Canucks"}},
                                {"homeAway": "away", "score": str(away), "team": {"id": "2", "abbreviation": "EDM", "displayName": "Edmonton Oilers"}}]}
        return {"events": [{"id": gid, "date": date, "competitions": [comp]}]}

    bot = SportsBot(SubscriptionStore(tmp_path / "subscriptions.json"), 10, None)
    bot.store.add(5, "nhl")
    asked, sent = [], []
    boards = {None: nhl("in", "STATUS_IN_PROGRESS", 2, 2)}

    async def scoreboard(league, date=None):
        asked.append(date)
        return parse_scoreboard(boards.get(date, {"events": []}), league)

    async def deliver(channel_id, game, kind, embed=None, content=None, view=None):
        sent.append(kind)
    bot.espn.scoreboard = scoreboard
    bot._deliver = deliver
    bot.play_resolvers.pop("nhl")  # scoring plays aren't what this tests
    asyncio.run(bot._poll_league("nhl"))
    # ESPN moves on to the next day while the late game is still going...
    boards[None] = nhl("pre", "STATUS_SCHEDULED", 0, 0, gid="402", date="2026-10-03T23:00Z")
    boards["20261002"] = nhl("in", "STATUS_IN_PROGRESS", 3, 2)  # 10pm Eastern on Oct 2
    asyncio.run(bot._poll_league("nhl"))
    # ...and it ends: the final still posts.
    boards["20261002"] = nhl("post", "STATUS_FINAL", 3, 2)
    asyncio.run(bot._poll_league("nhl"))
    assert FINAL in sent and asked == [None, None, "20261002", None, "20261002"]
    # Once it's over it isn't looked up any more.
    asyncio.run(bot._poll_league("nhl"))
    assert asked[-1] is None
    assert [g.id for g in bot.latest["nhl"]] == ["402"]
    asyncio.run(bot.espn.close())
