"""Today's games for a channel, in the channel's time zone."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .espn import ESPNClient, Game, start_time
from .leagues import LEAGUES

COMMON_TIMEZONES = (
    "America/Toronto", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Vancouver", "America/Halifax", "Europe/London", "Europe/Paris", "Asia/Kolkata", "Asia/Dubai",
    "Asia/Singapore", "Australia/Sydney", "Pacific/Auckland", "UTC",
)


def local_day(game: Game, tz: ZoneInfo) -> date | None:
    start = start_time(game)
    return start.astimezone(tz).date() if start else None


async def games_on(espn: ESPNClient, key: str, day: date, tz: ZoneInfo) -> list[Game]:
    """The league's games starting on `day` in `tz`.

    ESPN's dates follow US Eastern time, so the days either side are fetched too
    and filtered down to the local day.
    """
    league = LEAGUES[key]
    if league.feed == "scorepanel":
        batches = [await espn.scoreboard(league)]
    else:
        days = [(day + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)]
        batches = await asyncio.gather(*(espn.scoreboard(league, d) for d in days))
    seen, games = set(), []
    for g in (g for batch in batches for g in batch):
        if g.id not in seen and local_day(g, tz) == day:
            seen.add(g.id)
            games.append(g)
    return sorted(games, key=lambda g: g.start)


def today(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()
