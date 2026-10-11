"""Who started, and how many shots the opponent gives up: context for total-shots props.

A player's shot record mixes the games he started with the ones he came off the bench for (or sat on it,
logged as 0 shots), and 2+ shots against a side that lets its opponents take 18 a game is a better bet than
against one that allows 8. So each team's recent finished matches are read from ESPN (the team's schedule,
then each match's summary): its total shots, its opponent's, home or away, and who started and who came on.

- starts_only keeps the games the player started.
- opponent_factor says how many more (or fewer) shots this opponent concedes than a typical team, pulled
  toward "average" when it has few games behind it, and kept within -20% / +25%.
- adjust scales a player's chance by that, treating his shots as Poisson: a 2+ shooter averaging 2.4 shots
  gains less from a generous opponent than a 4+ line does.
- venue_rate is his record at home only, or away only.

A finished match never changes, so each one is read once and saved to disk for good (a few hundred bytes
each, dropped after 400 days); schedules are read again every few hours.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil, exp
from pathlib import Path

from .espn import SUMMARY_URL
from .props import PlayerGame, Rate, _rate
from .storage import read_json

log = logging.getLogger(__name__)

SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams/{team}/schedule"
STAT = "totalShots"
SCHEDULE_SECONDS = 6 * 3600
RETRY_SECONDS = 30 * 60  # a summary that failed (or had no box score yet) is tried again after this
SETTLE_SECONDS = 6 * 3600  # a match this old is saved as it is, even if ESPN never filled in its box score
KEEP_DAYS = 400
SUMMARIES_AT_ONCE = 4  # a summary is ~370 KB of JSON
MIN_STARTS = 5  # starts_only keeps every game rather than judge by fewer starts than this
TYPICAL_SHOTS = 12.0  # a team's shots per game, when there's nothing to average
MIN_OPPONENT_GAMES = 4
SHRINK_GAMES = 6  # an opponent's record counts n / (n + 6): 6 games are worth half, 18 three quarters
FACTOR_RANGE = (0.8, 1.25)
CHANCE_RANGE = (0.02, 0.97)


@dataclass(frozen=True)
class PastGame:
    """One team's side of a finished match."""
    event_id: str
    date: str  # ISO kickoff, e.g. "2026-02-11T00:30Z"
    home: bool
    shots_for: int | None  # total shots, on target or not (None when ESPN has no box score)
    shots_against: int | None
    starters: frozenset[str]  # athlete ids (empty when ESPN has no lineup)
    subs: frozenset[str]  # athlete ids who came on


def _finished(competition: dict) -> bool:
    kind = (competition.get("status") or {}).get("type") or {}
    # A postponed or abandoned match is "post" too, but never completed.
    return kind.get("state") == "post" and kind.get("completed") is not False


def parse_schedule(data: dict) -> list[tuple[str, str]]:
    """(event id, ISO date) of the team's finished matches, newest first."""
    found = {}
    for event in (data or {}).get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        if event.get("id") and _finished(comp):
            found[str(event["id"])] = event.get("date") or comp.get("date") or ""
    return sorted(found.items(), key=lambda item: item[1], reverse=True)


def _season_year(data: dict) -> int | None:
    """The season a schedule covers (ESPN's year: 2026 for 2026-27 in Europe)."""
    season = (data or {}).get("requestedSeason") or (data or {}).get("season") or {}
    try:
        return int(season.get("year"))
    except (TypeError, ValueError):
        return None


def _shots(team: dict) -> int | None:
    for stat in team.get("statistics") or []:
        if stat.get("name") == STAT:
            try:
                return int(float(stat.get("displayValue")))
            except (TypeError, ValueError):
                return None
    return None


def _came_on(player: dict) -> bool:
    on = player.get("subbedIn")
    return bool(on.get("didSub")) if isinstance(on, dict) else on is True


def _ids(players: list[dict]) -> frozenset[str]:
    # Interned: the same players turn up in dozens of saved matches.
    return frozenset(sys.intern(str((p.get("athlete") or {}).get("id"))) for p in players
                     if (p.get("athlete") or {}).get("id") is not None)


def _sides(summary: dict) -> dict[str, str]:
    """{team id: "home" or "away"} for a match summary."""
    comp = (((summary or {}).get("header") or {}).get("competitions") or [{}])[0]
    return {str(c.get("id") or (c.get("team") or {}).get("id")): c.get("homeAway") or ""
            for c in comp.get("competitors") or []}


def parse_team_game(summary: dict, team_id: str) -> PastGame | None:
    """This team's side of a match summary: shots each way, home or away, and who started and came on (None when
    the team isn't in it, or ESPN has neither its shots nor its lineup)."""
    team_id = str(team_id)
    sides = _sides(summary)
    if team_id not in sides:
        return None
    header = summary.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    box = (summary.get("boxscore") or {}).get("teams") or []
    shots = {str((t.get("team") or {}).get("id")): _shots(t) for t in box}
    against = [s for tid, s in shots.items() if tid != team_id]
    roster = next((r.get("roster") or [] for r in summary.get("rosters") or []
                   if str((r.get("team") or {}).get("id")) == team_id), [])
    game = PastGame(str(header.get("id") or comp.get("id") or ""), comp.get("date") or "", sides[team_id] == "home",
                    shots.get(team_id), against[0] if len(against) == 1 else None,
                    _ids([p for p in roster if p.get("starter")]), _ids([p for p in roster if _came_on(p)]))
    if not game.event_id or (game.shots_for is None and not game.starters):
        return None
    return game


def _complete(game: PastGame) -> bool:
    return game.shots_for is not None and game.shots_against is not None and bool(game.starters)


def _to_json(game: PastGame) -> dict:
    return {"date": game.date, "home": game.home, "for": game.shots_for, "against": game.shots_against,
            "starters": sorted(game.starters), "subs": sorted(game.subs)}


def _from_json(key: str, raw: dict) -> PastGame:
    return PastGame(key.split(":")[0], str(raw["date"]), bool(raw["home"]), raw.get("for"), raw.get("against"),
                    frozenset(sys.intern(str(p)) for p in raw.get("starters") or []),
                    frozenset(sys.intern(str(p)) for p in raw.get("subs") or []))


def _write(path: Path, text: str) -> None:
    """Saves atomically, so a crash leaves the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class MatchHistory:
    """Teams' recent finished matches from ESPN. Schedules are kept in memory for 6 hours; each match is kept on
    disk (a JSON file at `path`, keyed "event id:team id") for 400 days, so a restart doesn't read them again."""

    def __init__(self, espn, path: str | Path) -> None:
        self.espn = espn
        self.path = Path(path)
        self._games: dict[str, PastGame] = {}
        saved = read_json(self.path, {})
        for key, raw in (saved if isinstance(saved, dict) else {}).items():
            try:
                self._games[key] = _from_json(key, raw)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # one bad entry costs one match, not the file
        self._schedules: dict[str, tuple[float, list[tuple[str, str]], int | None]] = {}
        self._later: dict[str, tuple[float, PastGame | None]] = {}  # not saved: retried after RETRY_SECONDS
        self._unsaved = False
        self._limit = asyncio.Semaphore(SUMMARIES_AT_ONCE)
        self._saving = asyncio.Lock()

    async def recent(self, league_path: str, team_id: str, n: int = 10) -> list[PastGame]:
        """The team's last n finished matches in this league (ESPN path, e.g. "soccer/bra.1"), newest first. Early in
        a season last season's are added to make up n; matches whose summary can't be read are left out."""
        team_id = str(team_id)
        url = SCHEDULE_URL.format(path=league_path, team=team_id)
        schedule, year = await self._schedule(url)
        if len(schedule) < n and year:
            older, _ = await self._schedule(url, year - 1)
            schedule = schedule + [s for s in older if s not in schedule]
        # Older matches than the disk keeps would only be read again and again.
        schedule = [(eid, day) for eid, day in schedule if day[:10] >= _cutoff()]
        games = await asyncio.gather(*(self._game(league_path, eid, team_id) for eid, _ in schedule[:n]))
        if self._unsaved:
            await self._save()
        return [g for g in games if g is not None]

    async def _schedule(self, url: str, season: int | None = None) -> tuple[list[tuple[str, str]], int | None]:
        key = f"{url}#{season}"
        now = time.monotonic()
        hit = self._schedules.get(key)
        if hit and now - hit[0] < SCHEDULE_SECONDS:
            return hit[1], hit[2]
        try:
            data = await self.espn._get_json(url, {"season": season} if season else None)
        except Exception:
            log.warning("No ESPN schedule from %s (season %s)", url, season, exc_info=True)
            return [], None  # not kept: tried again next time
        found = (parse_schedule(data), _season_year(data))
        self._schedules[key] = (now, *found)
        return found

    def _known(self, key: str) -> tuple[bool, PastGame | None]:
        """(whether this match side has been read, what was found)."""
        if key in self._games:
            return True, self._games[key]
        later = self._later.get(key)
        if later and time.monotonic() < later[0]:
            return True, later[1]
        return False, None

    async def _game(self, league_path: str, event_id: str, team_id: str) -> PastGame | None:
        key = f"{event_id}:{team_id}"
        known, game = self._known(key)
        if known:
            return game
        async with self._limit:
            known, game = self._known(key)
            if known:  # read while this waited (the other team's lookup of the same match)
                return game
            try:
                summary = await self.espn._get_json(SUMMARY_URL.format(path=league_path), {"event": event_id})
            except Exception as e:
                log.warning("No ESPN summary for %s: %r", event_id, e)
                summary = {}
        self._keep(event_id, summary)
        known, game = self._known(key)
        if not known:  # it failed, or the team isn't in it: tried again later
            self._later[key] = (time.monotonic() + RETRY_SECONDS, None)
        return game

    def _keep(self, event_id: str, summary: dict) -> None:
        """Keeps both teams' sides of a match: the other team's history needs it too."""
        now = time.monotonic()
        for tid in _sides(summary):
            key = f"{event_id}:{tid}"
            if (game := parse_team_game(summary, tid)) is None:
                continue
            if _complete(game) or _age_seconds(game.date) > SETTLE_SECONDS:
                self._games[key] = game
                self._later.pop(key, None)
                self._unsaved = True
            else:  # it just finished: ESPN may still be filling in the box score
                self._later[key] = (now + RETRY_SECONDS, game)
        if len(self._later) > 1000:
            self._later = {k: v for k, v in self._later.items() if v[0] > now}

    async def _save(self) -> None:
        async with self._saving:
            if not self._unsaved:
                return
            self._unsaved = False
            cutoff = _cutoff()
            self._games = {k: g for k, g in self._games.items() if g.date[:10] >= cutoff}
            text = json.dumps({k: _to_json(g) for k, g in self._games.items()}, separators=(",", ":"))
            try:
                await asyncio.to_thread(_write, self.path, text)
            except Exception:
                self._unsaved = True
                log.warning("Couldn't save the match history to %s", self.path, exc_info=True)


def _cutoff() -> str:
    """The oldest match date kept (ISO day)."""
    return (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).date().isoformat()


def _age_seconds(iso: str) -> float:
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")  # no usable date: nothing to wait for
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


# ---------- the model ----------

def starts_only(games: list[PlayerGame], history: list[PastGame], player_id: str) -> list[PlayerGame]:
    """The player's games without the ones his team's lineups show he didn't start (off the bench, or unused).
    Games the history doesn't cover (older, for another team, or with no lineup on ESPN) are kept. If fewer than
    5 games would remain, the original list comes back unchanged: too few starts to judge him by."""
    lineups: dict[str, set[str]] = {}
    for h in history:
        if h.starters:
            lineups.setdefault(h.event_id, set()).update(h.starters)
    pid = str(player_id)
    kept = [g for g in games if g.event_id not in lineups or pid in lineups[g.event_id]]
    return kept if len(kept) >= MIN_STARTS else games


def league_average(histories: Iterable[Iterable[PastGame]] | Iterable[PastGame]) -> float:
    """A typical team's shots per game: the mean shots_for over every match given (each team's history, or one flat
    list; a team's match given twice counts once). 12.0 when there are none."""
    seen: dict[tuple[str, bool], int] = {}
    for item in histories:
        for g in [item] if isinstance(item, PastGame) else item:
            if g.shots_for is not None:
                seen[(g.event_id, g.home)] = g.shots_for
    return sum(seen.values()) / len(seen) if seen else TYPICAL_SHOTS


def opponent_factor(opponent_history: list[PastGame], league_avg: float) -> float:
    """How many shots the opponent gives up against a typical team's: 1.15 means 15% more. A few games are a small
    sample, so the record counts n / (n + 6) and the rest is "average"; kept within 0.8 to 1.25, and 1.0 when
    there are fewer than 4 games to go on."""
    allowed = [g.shots_against for g in opponent_history if g.shots_against is not None]
    n = len(allowed)
    if n < MIN_OPPONENT_GAMES or league_avg <= 0:
        return 1.0
    raw = sum(allowed) / n / league_avg
    shrunk = 1 + (raw - 1) * n / (n + SHRINK_GAMES)
    return min(max(shrunk, FACTOR_RANGE[0]), FACTOR_RANGE[1])


def poisson_tail(lam: float, line: int) -> float:
    """The chance of `line` or more when lam are expected (Poisson): P(X >= line)."""
    line = ceil(line)  # "over 2.5" is 3 or more
    if line <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = below = exp(-lam)
    for k in range(1, line):
        term *= lam / k
        below += term
    return min(max(1 - below, 0.0), 1.0)


def adjust(chance: float, games: list[PlayerGame], line: int, factor: float, stat: str = STAT) -> float:
    """The chance scaled for the opponent: his average shots over these games (lam) times the factor, through the
    Poisson tail at this line, relative to his usual. Kept within 2% to 97%; unchanged when the factor is 1 or he
    has no shots on record."""
    lam = sum(g.stats.get(stat, 0) for g in games) / len(games) if games else 0.0
    if lam <= 0 or factor == 1:
        return chance
    usual = poisson_tail(lam, line)
    if usual <= 0:
        return chance
    return min(max(chance * poisson_tail(lam * factor, line) / usual, CHANCE_RANGE[0]), CHANCE_RANGE[1])


def venue_rate(games: list[PlayerGame], line: int, home: bool, stat: str = STAT) -> Rate:
    """How often he reached the line at home (home=True) or away; games with no venue on record are left out."""
    return _rate([g for g in games if g.home is not None and g.home == home], stat, line)
