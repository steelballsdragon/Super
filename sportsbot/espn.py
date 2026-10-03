"""Minimal client for ESPN's public scoreboard API."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiohttp

from .leagues import League

log = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    abbrev: str
    score: int
    logo: str | None = None


@dataclass(frozen=True)
class Goal:
    """A soccer scoring play (goal, penalty or own goal)."""

    team_id: str
    minute: str
    scorer: str
    penalty: bool = False
    own_goal: bool = False

    def describe(self) -> str:
        tag = " (pen)" if self.penalty else " (OG)" if self.own_goal else ""
        return f"{self.minute} {self.scorer}{tag}"


@dataclass(frozen=True)
class Game:
    id: str
    league_key: str
    home: Team
    away: Team
    state: str  # "pre", "in" or "post"
    status_name: str  # e.g. STATUS_HALFTIME, STATUS_FINAL
    detail: str  # human-readable status, e.g. "Q3 4:12" or "67'"
    start: str  # ISO timestamp
    last_play: str | None = None
    goals: tuple[Goal, ...] = field(default_factory=tuple)

    @property
    def teams(self) -> tuple[Team, Team]:
        return (self.away, self.home)

    def involves(self, query: str) -> bool:
        q = query.strip().lower()
        return any(
            q == t.abbrev.lower() or q in t.name.lower() for t in self.teams
        )

    def scoreline(self) -> str:
        return f"{self.away.name} {self.away.score} - {self.home.score} {self.home.name}"


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_team(competitor: dict) -> Team:
    team = competitor.get("team", {})
    return Team(
        id=str(team.get("id", competitor.get("id", ""))),
        name=team.get("displayName") or team.get("name", "?"),
        abbrev=team.get("abbreviation", "?"),
        score=_int(competitor.get("score")),
        logo=team.get("logo"),
    )


def _parse_goals(details: list[dict]) -> tuple[Goal, ...]:
    goals = []
    for d in details:
        if not d.get("scoringPlay") or d.get("shootout"):
            continue
        athletes = d.get("athletesInvolved") or [{}]
        goals.append(
            Goal(
                team_id=str(d.get("team", {}).get("id", "")),
                minute=d.get("clock", {}).get("displayValue", ""),
                scorer=athletes[0].get("displayName", "Unknown"),
                penalty=bool(d.get("penaltyKick")),
                own_goal=bool(d.get("ownGoal")),
            )
        )
    return tuple(goals)


def parse_scoreboard(data: dict, league: League) -> list[Game]:
    games = []
    for event in data.get("events", []):
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue
        status = comp.get("status") or event.get("status") or {}
        stype = status.get("type", {})
        situation = comp.get("situation") or {}
        games.append(
            Game(
                id=str(event.get("id")),
                league_key=league.key,
                home=_parse_team(home),
                away=_parse_team(away),
                state=stype.get("state", "pre"),
                status_name=stype.get("name", ""),
                detail=stype.get("shortDetail") or stype.get("detail", ""),
                start=event.get("date", ""),
                last_play=(situation.get("lastPlay") or {}).get("text"),
                goals=_parse_goals(comp.get("details") or []),
            )
        )
    return games


class ESPNClient:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                trust_env=True,
            )
            self._owns_session = True
        return self._session

    async def scoreboard(self, league: League) -> list[Game]:
        session = await self._get_session()
        url = BASE_URL.format(path=league.path)
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return parse_scoreboard(data, league)

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
