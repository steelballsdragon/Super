"""The bot's wiring of the new pieces: people's followed games get their own notifications (and the edits when ESPN
fills a play in), lineup-confirmed red alerts wait for both starting XIs, and the weekly red alerts report."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sportsbot import redalerts as R
from sportsbot.bot import SportsBot, register_commands
from sportsbot.espn import parse_scoreboard, parse_scoring_plays
from sportsbot.leagues import LEAGUES
from sportsbot.plays import PlayResolver
from sportsbot.props import Availability, Lineup
from sportsbot.storage import SubscriptionStore


def nhl_board(away=0, home=0):
    comp = {"status": {"type": {"state": "in", "name": "STATUS_IN_PROGRESS", "shortDetail": "2nd 5:00"}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "12", "displayName": "New York Islanders",
                                                                            "abbreviation": "NYI"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "11", "displayName": "New Jersey Devils",
                                                                            "abbreviation": "NJ"}}]}
    return parse_scoreboard({"events": [{"id": "7", "date": "2026-10-04T23:00Z", "competitions": [comp]}]}, LEAGUES["nhl"])


class Message:
    def __init__(self, embed, channel_id):
        self.embed, self.channel = embed, SimpleNamespace(id=channel_id)

    async def edit(self, embed=None, **kw):
        self.embed = embed


def test_a_followed_game_notifies_even_with_no_channel_and_edits_follow(tmp_path):
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 5, None)
    plays = []

    async def feed(event_id):
        return parse_scoring_plays({"plays": plays}, "hockey")
    bot.play_resolvers["nhl"] = PlayResolver(feed)
    boards = [nhl_board()]

    async def scoreboard(league, date=None):
        return boards[0]
    bot.espn.scoreboard = scoreboard
    dms = []

    async def dm(embed=None, view=None, **kw):
        dms.append(Message(embed, 555))
        return dms[-1]
    bot.get_user = lambda uid: SimpleNamespace(send=dm)
    bot.notify.store.add(42, boards[0])  # no channel follows the NHL; this person follows the game
    assert "nhl" in bot.notify.store.leagues()

    asyncio.run(bot._poll_league("nhl"))
    boards[0] = nhl_board(home=1)
    asyncio.run(bot._poll_league("nhl"))  # the goal, posted at once (ESPN hasn't described it yet)
    [post] = dms
    plays.append({"id": "g1", "scoringPlay": True, "text": "Brayden Schenn Goal (1) Snap Shot, assists: Victor Eklund (1)",
                  "team": {"id": "12"}, "period": {"displayValue": "2nd"}, "clock": {"displayValue": "5:00"},
                  "awayScore": 0, "homeScore": 1, "strength": {"text": "Even Strength"}})
    asyncio.run(bot._poll_league("nhl"))
    assert len(dms) == 1 and "Brayden Schenn Goal (1) Snap Shot" in post.embed.description  # the DM was edited
    asyncio.run(bot.espn.close())


def soccer_game(start):
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": "10", "abbreviation": "FLA",
                                                                         "displayName": "Flamengo"}},
                            {"homeAway": "away", "score": "0", "team": {"id": "20", "abbreviation": "FLU",
                                                                         "displayName": "Fluminense"}}]}
    [g] = parse_scoreboard({"events": [{"id": "99", "date": f"{start:%Y-%m-%dT%H:%MZ}", "competitions": [comp]}]},
                           LEAGUES["brasileirao"])
    return g


def test_lineup_confirmed_alerts_wait_for_both_lineups(tmp_path, monkeypatch):
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 5, None)
    register_commands(bot)
    bot.settings.update(9, redalerts=True)
    g = soccer_game(datetime.now(timezone.utc) + timedelta(minutes=40))

    async def week_games(key):
        return [g] if key == "brasileirao" else []
    bot.week_games = week_games
    lineups = {}

    async def availability(league, game_id, path=""):
        return Availability(lineups=dict(lineups))
    bot._availability = availability
    asked = []

    async def game_alerts(self, game, confirmed_only=False):
        asked.append(confirmed_only)
        price = R.Price(2, "+150", 2.5)
        return [R.Alert("Pedro", "p1", "FLA", "FLU", game, price, 0.6, R.Rate(7, 10), R.Rate(5, 7), R.Rate(10, 20),
                        R.Rate(0, 0), "2026", "2025", confirmed=True)]
    monkeypatch.setattr(R.RedAlerts, "game_alerts", game_alerts)
    sent = []

    async def send(cid, embed=None, content=None, view=None):
        sent.append((cid, getattr(embed, "title", None), content))
    bot._send = send

    asyncio.run(bot.red_confirmed())  # lineups not out: nothing, and it looks again later
    assert not sent and not asked and not bot.state.get("red_confirmed", "99")
    lineups["10"] = Lineup(frozenset({"p1"}), frozenset({"p1"}), "FLA")
    asyncio.run(bot.red_confirmed())  # only one side out: still waiting
    assert not sent
    lineups["20"] = Lineup(frozenset({"x"}), frozenset({"x"}), "FLU")
    asyncio.run(bot.red_confirmed())
    titles = [t for _, t, _ in sent if t]
    assert titles and titles[0].startswith("🚨✅ Lineup-confirmed red alerts") and asked == [True]
    assert bot.state.get("red_confirmed", "99")
    sent.clear()
    asyncio.run(bot.red_confirmed())  # once per game
    assert not sent
    asyncio.run(bot.espn.close())


def test_weekly_report_goes_out_once_on_monday_morning(tmp_path):
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 5, None)
    bot.settings.update(9, redalerts=True, timezone="UTC")
    sent = []

    async def send(cid, embed=None, content=None, view=None):
        sent.append((cid, embed.title))
    bot._send = send
    asyncio.run(bot._post_red_report(datetime(2026, 10, 11, 9, 30, tzinfo=timezone.utc)))  # a Sunday
    assert not sent
    monday = datetime(2026, 10, 12, 9, 5, tzinfo=timezone.utc)
    asyncio.run(bot._post_red_report(monday))
    asyncio.run(bot._post_red_report(monday + timedelta(minutes=30)))
    assert sent == [(9, "🚨 Red alerts this week")]
    asyncio.run(bot.espn.close())
