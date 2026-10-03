import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sportsbot.bot import SportsBot, register_commands
from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.schedule import games_on
from sportsbot.settings import SettingsStore, StateStore
from sportsbot.storage import SubscriptionStore

NFL = LEAGUES["nfl"]


def game(gid, start, home=("Buffalo Bills", "Buffalo Bills"), away=("New England Patriots", "New England Patriots"), odds=None):
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": home[0], "abbreviation": home[0], "displayName": home[1]}},
                            {"homeAway": "away", "score": "0", "team": {"id": away[0], "abbreviation": away[0], "displayName": away[1]}}],
            "odds": [odds] if odds else []}
    [g] = parse_scoreboard({"events": [{"id": gid, "date": start, "competitions": [comp]}]}, NFL)
    return g


def make_bot(tmp_path):
    bot = SportsBot(SubscriptionStore(tmp_path / "subs.json"), 10, None,
                    SettingsStore(tmp_path / "settings.json"), StateStore(tmp_path / "state.json"))
    register_commands(bot)
    sent = []

    async def send(channel_id, embed=None, content=None):
        sent.append((channel_id, embed.title if embed else content))
    bot._send = send
    return bot, sent


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def test_reminder_posts_once_fifteen_minutes_before(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.store.add(1, "nfl", "Bills")
    bot.store.add(2, "nfl", "Chiefs")
    for cid in (1, 2):
        bot.settings.update(cid, reminders=True)
    bot.latest["nfl"] = [game("a", "2026-10-04T17:00Z")]
    asyncio.run(bot._send_reminders(now=ts("2026-10-04T16:40Z")))  # 20 min out: too early
    assert sent == []
    asyncio.run(bot._send_reminders(now=ts("2026-10-04T16:46Z")))
    assert sent == [(1, "⏰ **New England Patriots vs Buffalo Bills** starts <t:1791133200:R> · 🏈 NFL")]
    asyncio.run(bot._send_reminders(now=ts("2026-10-04T16:50Z")))
    assert len(sent) == 1  # only once, and not to the Chiefs channel


def test_reminder_includes_the_line_when_odds_are_on(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.store.add(1, "nfl")
    bot.settings.update(1, reminders=True)
    odds = {"provider": {"name": "DraftKings"}, "overUnder": 44.5,
            "pointSpread": {"home": {"close": {"line": "-3"}}, "away": {"close": {"line": "+3"}}}}
    g = game("a", "2026-10-04T17:00Z", odds=odds)
    bot.odds.remember([g])
    bot.latest["nfl"] = [g]
    asyncio.run(bot._send_reminders(now=ts("2026-10-04T16:50Z")))
    assert sent[0][1].endswith("\nSpread: New England Patriots +3 · Buffalo Bills -3 · Total: O/U 44.5")


def test_games_on_uses_the_channels_local_day(tmp_path):
    late = game("late", "2026-10-05T00:20Z")  # Sunday night in Toronto, Monday morning in India
    early = game("early", "2026-10-04T13:30Z")

    class Espn:
        async def scoreboard(self, league, date=None):
            return {"20261004": [early], "20261005": [late]}.get(date, [])
    toronto = asyncio.run(games_on(Espn(), "nfl", date(2026, 10, 4), ZoneInfo("America/Toronto")))
    india = asyncio.run(games_on(Espn(), "nfl", date(2026, 10, 4), ZoneInfo("Asia/Kolkata")))
    assert [g.id for g in toronto] == ["early", "late"]
    assert [g.id for g in india] == ["early"]


def test_daily_schedule_posts_once_at_the_chosen_hour(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.store.add(1, "nfl")
    bot.settings.update(1, daily_hour=9, timezone="America/Toronto")

    async def schedule_for(channel_id, tz):
        return SimpleNamespace(title="📅 Today's games")
    bot.schedule_for = schedule_for
    at = lambda iso: asyncio.run(bot._post_daily_schedules(now=datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)))
    at("2026-10-04T12:59")  # 8:59 in Toronto
    assert sent == []
    at("2026-10-04T13:05")  # 9:05 in Toronto
    at("2026-10-04T13:30")
    assert sent == [(1, "📅 Today's games")]
    at("2026-10-05T16:00")  # next day, but noon: outside the window (bot was down)
    assert len(sent) == 1
    at("2026-10-06T13:00")
    assert len(sent) == 2


def test_daily_schedule_stays_quiet_with_no_games(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.store.add(1, "nfl")
    bot.settings.update(1, daily_hour=9)

    async def schedule_for(channel_id, tz):
        return None
    bot.schedule_for = schedule_for
    asyncio.run(bot._post_daily_schedules(now=datetime(2026, 10, 4, 13, 5, tzinfo=timezone.utc)))
    assert sent == []


def test_daily_command_rejects_unknown_timezone(tmp_path):
    bot, _ = make_bot(tmp_path)
    replies = []

    async def send_message(msg, ephemeral=False):
        replies.append(msg)
    inter = SimpleNamespace(channel_id=1, response=SimpleNamespace(send_message=send_message))
    asyncio.run(bot.tree.get_command("daily").callback(inter, True, 9, "Mars/Olympus"))
    assert "don't recognise the time zone" in replies[0] and bot.settings.get(1).daily_hour is None
    asyncio.run(bot.tree.get_command("daily").callback(inter, True, 8, "Asia/Kolkata"))
    assert bot.settings.get(1).daily_hour == 8 and bot.settings.get(1).timezone == "Asia/Kolkata"
