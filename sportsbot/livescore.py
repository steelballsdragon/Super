"""Faster soccer goals from LiveScore's free app feed.

ESPN's data for the European leagues (La Liga, Serie A, ...) can reach it a minute or two after a goal, while
LiveScore has the score, scorer and assist within seconds. So for live soccer games the bot also reads LiveScore's
live list (one small request for every match in the world) and, when it shows a goal ESPN doesn't have yet, uses
that score and goal straight away. ESPN stays the source for everything else, and once ESPN catches up its own
data takes over. If LiveScore is unreachable the bot simply waits for ESPN, as before.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace

from .espn import Game, Goal

log = logging.getLogger(__name__)

LIVE_URL = "https://prod-public-api.livescore.com/v1/api/app/live/soccer/0"
EVENT_URL = "https://prod-public-api.livescore.com/v1/api/app/scoreboard/soccer/{eid}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/129.0 Safari/537.36"}
TTL = 4.0  # both feeds are re-read at most this often
REST_AFTER = 3  # failures in a row before resting
REST_SECONDS = 120
STICKY_SECONDS = 15 * 60  # a goal seen on LiveScore is kept this long while LiveScore can't be read
GOAL, PENALTY, OWN_GOAL, ASSIST = 36, 37, 39, 63
# Words that don't tell clubs apart ("Real" does: Real Madrid vs Real Sociedad keeps both words, see same_team).
GENERIC = {"fc", "cf", "sc", "afc", "ac", "ssc", "as", "us", "cd", "ud", "rc", "rcd", "club", "de", "del", "la",
           "calcio", "sv", "vfb", "vfl", "tsg", "fsv", "bsc", "sd", "ca", "cp", "sl", "cfc", "the", "and"}


@dataclass(frozen=True)
class LiveMatch:
    eid: str
    home: str
    away: str
    home_score: int
    away_score: int
    minute: str


@dataclass(frozen=True)
class LiveGoal:
    home_side: bool  # the team whose score went up
    minute: str  # ESPN's style: 62' or 45'+6'
    scorer: str
    assist: str  # "" when unassisted
    penalty: bool
    own_goal: bool


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_live(data: dict) -> list[LiveMatch]:
    out = []
    for stage in (data or {}).get("Stages") or []:
        for e in stage.get("Events") or []:
            try:
                home, away = e["T1"][0]["Nm"], e["T2"][0]["Nm"]
            except (KeyError, IndexError, TypeError):
                continue
            hs, as_ = _int(e.get("Tr1")), _int(e.get("Tr2"))
            if hs is None or as_ is None:
                continue
            out.append(LiveMatch(str(e.get("Eid") or ""), home, away, hs, as_, str(e.get("Eps") or "")))
    return out


def parse_goals(data: dict) -> list[LiveGoal]:
    """Goals in order, from a match's incidents (each goal group carries the score after it)."""
    goals, before = [], (0, 0)
    groups = [g for per in sorted(((data or {}).get("Incs-s") or {}).items()) for g in per[1] or []]
    for g in groups:
        score = g.get("Sc")
        if not (isinstance(score, list) and len(score) == 2):
            continue
        after = (_int(score[0]) or 0, _int(score[1]) or 0)
        if after <= before or sum(after) != sum(before) + 1:
            before = max(before, after)
            continue
        incs = g.get("Incs") or [g]
        main = next((i for i in incs if i.get("IT") in (GOAL, PENALTY, OWN_GOAL)), None)
        if main is None:
            before = after
            continue
        assist = next((i.get("Pn") or i.get("Ln") or "" for i in incs if i.get("IT") == ASSIST), "")
        extra = _int(g.get("MinEx"))
        minute = f"{_int(g.get('Min')) or 0}'" + (f"+{extra}'" if extra else "")
        goals.append(LiveGoal(after[0] > before[0], minute, main.get("Pn") or main.get("Ln") or "", assist,
                              main.get("IT") == PENALTY, main.get("IT") == OWN_GOAL))
        before = after
    return goals


def words(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    found = set(re.findall(r"[a-z0-9]+", plain))
    return (found - GENERIC) or found


def same_team(a: str, b: str) -> bool:
    """ "Deportivo Alaves" and "Alavés", "Atletico Madrid" and "Atlético Madrid"; not Real Madrid and Real Sociedad."""
    wa, wb = words(a), words(b)
    return bool(wa) and bool(wb) and (wa <= wb or wb <= wa)


def find(game: Game, live: list[LiveMatch]) -> LiveMatch | None:
    hits = [m for m in live if same_team(game.home.name, m.home) and same_team(game.away.name, m.away)]
    return hits[0] if len(hits) == 1 else None


class FastGoals:
    def __init__(self, session_getter, clock=time.monotonic):
        self._session = session_getter
        self._clock = clock
        self._live: tuple[float, list[LiveMatch] | None] = (-1e9, None)
        self._events: dict[str, tuple[float, list[LiveGoal]]] = {}
        self._ahead: dict[str, tuple[Game, float]] = {}  # ESPN game id -> (the faster version, when last confirmed)
        self._lock = asyncio.Lock()
        self.failures, self.rest_until = 0, 0.0
        self.last_ok: float | None = None
        self.last_error = ""
        self.used = 0  # goals posted ahead of ESPN

    def status(self) -> str:
        if self.rest_until > self._clock():
            return f"resting after errors ({self.last_error})"
        if self.last_ok is None:
            return "not needed yet" if not self.last_error else f"⚠️ {self.last_error}"
        return f"ok · {self.used} goals posted ahead of ESPN"

    async def _get(self, url: str) -> dict | None:
        if self.rest_until > self._clock():
            return None
        try:
            session = await self._session()
            async with session.get(url, params={"locale": "en"}, headers=HEADERS) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:120]
            if self.failures >= REST_AFTER:
                self.rest_until = self._clock() + REST_SECONDS
                log.warning("LiveScore unavailable (%s); using ESPN alone for %ss", self.last_error, REST_SECONDS)
            return None
        self.failures, self.last_ok = 0, time.time()
        return data

    async def live(self) -> list[LiveMatch] | None:
        async with self._lock:  # every soccer league asks in the same cycle: one request
            at, matches = self._live
            if self._clock() - at < TTL:
                return matches
            data = await self._get(LIVE_URL)
            matches = parse_live(data) if data is not None else None
            self._live = (self._clock(), matches)
            return matches

    async def goals(self, eid: str) -> list[LiveGoal] | None:
        at, goals = self._events.get(eid, (-1e9, None))
        if goals is not None and self._clock() - at < TTL:
            return goals
        data = await self._get(EVENT_URL.format(eid=eid))
        if data is None:
            return goals
        goals = parse_goals(data)
        self._events[eid] = (self._clock(), goals)
        return goals

    async def overlay(self, games: list[Game]) -> list[Game]:
        """The games, with any goal LiveScore already has and ESPN doesn't."""
        now = self._clock()
        live_ids = {g.id for g in games if g.state == "in"}
        if not live_ids and not self._ahead:
            return games
        matches = await self.live() if live_ids else None
        out = []
        for g in games:
            espn = (g.home.score, g.away.score)
            kept = self._ahead.get(g.id)
            match = find(g, matches) if matches and g.id in live_ids else None
            if match is not None:
                fast = (match.home_score, match.away_score)
                if fast != espn and fast[0] >= espn[0] and fast[1] >= espn[1]:
                    new = await self._with_goals(g, match)
                    before = kept[0].home.score + kept[0].away.score if kept else sum(espn)
                    self.used += max(0, sum(fast) - before)
                    self._ahead[g.id] = (new, now)
                    out.append(new)
                    continue
                self._ahead.pop(g.id, None)  # ESPN has caught up (or LiveScore took a goal back)
                out.append(g)
                continue
            if kept is not None:
                ahead = (kept[0].home.score, kept[0].away.score)
                if now - kept[1] < STICKY_SECONDS and ahead != espn and ahead[0] >= espn[0] and ahead[1] >= espn[1]:
                    # LiveScore can't be read right now: keep its goal rather than "take it back"
                    out.append(replace(kept[0], state=g.state, status_name=g.status_name, detail=g.detail,
                                       period=g.period))
                    continue
                self._ahead.pop(g.id, None)
            out.append(g)
        for gid in [gid for gid in self._ahead if gid not in {g.id for g in games}]:
            self._ahead.pop(gid, None)
        return out

    async def _with_goals(self, g: Game, match: LiveMatch) -> Game:
        total = match.home_score + match.away_score
        goals = g.goals
        found = await self.goals(match.eid)
        if found is not None and len(found) >= total and len(found) > len(goals):
            extra = tuple(Goal(team_id=g.home.id if lg.home_side else g.away.id, minute=lg.minute, scorer=lg.scorer,
                               penalty=lg.penalty, own_goal=lg.own_goal, assist=lg.assist)
                          for lg in found[len(goals):total])
            goals = goals + extra
        return replace(g, home=replace(g.home, score=match.home_score), away=replace(g.away, score=match.away_score),
                       goals=goals)
