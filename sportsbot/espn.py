"""Minimal client for ESPN's public scoreboard and game summary APIs."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field, replace

import aiohttp

from .leagues import LEAGUES, League

log = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/summary"
SCOREPANEL_URL = "https://site.web.api.espn.com/apis/site/v2/sports/{path}/scorepanel"
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams"
PLAYBYPLAY_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/playbyplay"

# ESPN has no team list for international cricket, so team suggestions start
# from the national sides and add any team currently playing.
INTERNATIONAL_CRICKET_TEAMS = (
    "Afghanistan", "Australia", "Bangladesh", "Canada", "England", "India", "Ireland",
    "Namibia", "Nepal", "Netherlands", "New Zealand", "Oman", "Pakistan", "Scotland",
    "South Africa", "Sri Lanka", "United Arab Emirates", "United States of America",
    "West Indies", "Zimbabwe",
)


@dataclass(frozen=True)
class Innings:
    """One cricket innings: runs, wickets and overs bowled."""

    runs: int
    wickets: int
    overs: float
    batting: bool


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    abbrev: str
    score: int
    logo: str | None = None
    score_text: str = ""  # cricket, e.g. "161/5 (18/20 ov, target 156)"
    innings: tuple[Innings, ...] = ()
    winner: bool = False  # set by ESPN once a game is decided
    shootout: int | None = None  # soccer penalty shootout goals

    @property
    def wickets(self) -> int:
        return sum(i.wickets for i in self.innings)

    @property
    def max_overs(self) -> int | None:
        """Overs per innings in limited-overs cricket, e.g. 20 from "51/1 (3.4/20 ov)"."""
        m = re.search(r"/(\d+) ov", self.score_text)
        return int(m.group(1)) if m else None


@dataclass(frozen=True)
class Goal:
    """A soccer scoring play (goal, penalty or own goal)."""

    team_id: str
    minute: str
    scorer: str
    penalty: bool = False
    own_goal: bool = False
    assist: str | None = None  # filled in from the match details when ESPN has it

    def describe(self) -> str:
        tag = " (pen)" if self.penalty else " (OG)" if self.own_goal else ""
        return f"{self.minute} {self.scorer}{tag}"


@dataclass(frozen=True)
class ScoringPlay:
    """An NFL, MLB or NHL scoring play, e.g. a touchdown with its yardage and kick."""

    id: str
    kind: str  # e.g. "Passing Touchdown", "Field Goal Good"; empty for MLB
    category: str  # e.g. "Touchdown", "Field Goal", "Home Run", "Power-Play Goal"
    text: str  # e.g. "Roman Wilson 12 Yd pass from Aaron Rodgers (Chris Boswell Kick)"
    team_id: str
    team_abbrev: str
    when: str  # e.g. "Q1 2:59", "Bottom 1st" or "2nd 16:58"
    away_score: int
    home_score: int

    @property
    def total(self) -> int:
        return self.away_score + self.home_score


@dataclass(frozen=True)
class Leader:
    """A standout player, e.g. PASS: A. Rodgers 22/40, 299 YDS, 3 TD."""

    category: str  # stat category ("PASS") or team abbreviation ("NY")
    athlete: str
    stats: str


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
    leaders: tuple[Leader, ...] = field(default_factory=tuple)
    period: int = 0
    summary: str = ""  # cricket, e.g. "India won toss & batted", "RCB won by 5 wkts"
    path: str = ""  # ESPN path for this game's details, e.g. "cricket/24289"
    odds: "Odds | None" = None  # betting line, when ESPN has one

    @property
    def league(self) -> League:
        return LEAGUES[self.league_key]

    @property
    def teams(self) -> tuple[Team, Team]:
        """Both teams in display order: home first for soccer and cricket, away first (US style) otherwise."""
        if self.league.sport in ("soccer", "cricket"):
            return (self.home, self.away)
        return (self.away, self.home)

    def involves(self, query: str) -> bool:
        q = query.strip().lower()
        return any(
            q == t.abbrev.lower() or q in t.name.lower() for t in self.teams
        )

    def scoreline(self) -> str:
        first, second = self.teams
        if self.league.sport == "cricket":
            return " · ".join(f"{t.name} {t.score_text}".strip() for t in self.teams)
        return f"{first.name} {first.score} - {second.score} {second.name}"


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def start_time(game: "Game"):
    """The game's start as an aware datetime, or None if ESPN didn't give one."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(game.start.replace("Z", "+00:00"))
    except ValueError:
        return None


def period_label(n: int, sport: str = "football") -> str:
    """Period name: Q1-Q4 then OT, 2OT... (hockey: 1st-3rd then OT, 2OT...)."""
    regulation = 3 if sport == "hockey" else 4
    if n > regulation:
        return "OT" if n == regulation + 1 else f"{n - regulation}OT"
    if sport == "hockey":
        return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, str(n))
    return f"Q{n}"


def _parse_team(competitor: dict, sport: str) -> Team:
    team = competitor.get("team", {})
    innings: tuple[Innings, ...] = ()
    score_text = ""
    score = _int(competitor.get("score"))
    if sport == "cricket":
        score_text = competitor.get("score") or ""
        innings = tuple(
            Innings(_int(l.get("runs")), _int(l.get("wickets")), _float(l.get("overs")), bool(l.get("isBatting")))
            for l in competitor.get("linescores") or []
        )
        score = sum(i.runs for i in innings)
    return Team(
        id=str(team.get("id", competitor.get("id", ""))),
        name=team.get("displayName") or team.get("name", "?"),
        abbrev=team.get("abbreviation", "?"),
        score=score,
        logo=team.get("logo"),
        score_text=score_text,
        innings=innings,
        winner=competitor.get("winner") is True,
        shootout=competitor.get("shootoutScore") if isinstance(competitor.get("shootoutScore"), int) else None,
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


# NFL lists game-wide leaders per stat category.
LEADER_CATEGORIES = {"passingYards": "PASS", "rushingYards": "RUSH", "receivingYards": "REC"}
# NBA, MLB and NHL list leaders per team. NBA's and MLB's overall "rating"
# leader has the fullest stat line (e.g. "36 PTS, 7 AST, 3 STL"); NHL only has
# a bare number for its points leader, so it's labelled.
TEAM_RATING_CATEGORIES = ("rating", "MLBRating", "points")


def _list(value) -> list[dict]:
    """ESPN sometimes sends a link object where other leagues send a list."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _top(cat: dict) -> tuple[str, str] | None:
    top = (_list(cat.get("leaders")) or [None])[0]
    if not top:
        return None
    athlete = top.get("athlete") or {}
    return athlete.get("shortName") or athlete.get("displayName", "?"), top.get("displayValue", "")


def _parse_leaders(comp: dict, competitors: list[dict]) -> tuple[Leader, ...]:
    leaders = []
    for cat in _list(comp.get("leaders")):
        label = LEADER_CATEGORIES.get(cat.get("name", ""))
        top = _top(cat)
        if label and top:
            leaders.append(Leader(label, *top))
    if leaders:
        return tuple(leaders)
    for c in competitors:
        cats = {cat.get("name"): cat for cat in _list(c.get("leaders"))}
        name = next((n for n in TEAM_RATING_CATEGORIES if n in cats), None)
        top = _top(cats[name]) if name else None
        if top:
            athlete, stats = top
            if name == "points":
                stats = f"{stats} PTS"
            leaders.append(Leader((c.get("team") or {}).get("abbreviation", "?"), athlete, stats))
    return tuple(leaders)


def parse_scoring_plays(summary: dict, sport: str = "football") -> list[ScoringPlay]:
    if sport == "baseball":
        return _parse_baseball_plays(summary)
    if sport == "hockey":
        return _parse_hockey_plays(summary)
    plays = []
    for p in summary.get("scoringPlays") or []:
        team = p.get("team") or {}
        period = _int((p.get("period") or {}).get("number"))
        clock = (p.get("clock") or {}).get("displayValue", "")
        plays.append(
            ScoringPlay(
                id=str(p.get("id", "")),
                kind=(p.get("type") or {}).get("text", ""),
                category=(p.get("scoringType") or {}).get("displayName", ""),
                text=(p.get("text") or "").strip(),
                team_id=str(team.get("id", "")),
                team_abbrev=team.get("abbreviation", ""),
                when=f"{period_label(period)} {clock}".strip() if period else clock,
                away_score=_int(p.get("awayScore")),
                home_score=_int(p.get("homeScore")),
            )
        )
    return plays


def _parse_baseball_plays(summary: dict) -> list[ScoringPlay]:
    plays = []
    for p in summary.get("plays") or []:
        if not p.get("scoringPlay"):
            continue
        text = (p.get("text") or "").strip()
        runs = _int(p.get("scoreValue"))
        if "homered" in text.lower():
            category = "Grand Slam" if runs == 4 else "Home Run"
        else:
            category = "Runs Scored" if runs > 1 else "Run Scored"
        period = p.get("period") or {}
        inning = (period.get("displayValue") or "").replace(" Inning", "")
        plays.append(
            ScoringPlay(
                id=str(p.get("id", "")),
                kind="",
                category=category,
                text=text,
                team_id=str((p.get("team") or {}).get("id", "")),
                team_abbrev="",
                when=f"{period.get('type', '')} {inning}".strip(),
                away_score=_int(p.get("awayScore")),
                home_score=_int(p.get("homeScore")),
            )
        )
    return plays


def _parse_hockey_plays(summary: dict) -> list[ScoringPlay]:
    plays = []
    for p in summary.get("plays") or []:
        if not p.get("scoringPlay"):
            continue
        strength = p.get("strength") or {}
        strength = f"{strength.get('abbreviation', '')} {strength.get('text', '')}".lower()
        text = (p.get("text") or "").strip()
        if "empty net" in text.lower():
            category = "Empty-Net Goal"
        elif "power" in strength:
            category = "Power-Play Goal"
        elif "short" in strength:
            category = "Shorthanded Goal"
        else:
            category = "Goal"
        period = (p.get("period") or {}).get("displayValue", "")
        clock = (p.get("clock") or {}).get("displayValue", "")
        plays.append(
            ScoringPlay(
                id=str(p.get("id", "")),
                kind="",
                category=category,
                text=text,
                team_id=str((p.get("team") or {}).get("id", "")),
                team_abbrev="",
                when=f"{period} {clock}".strip(),
                away_score=_int(p.get("awayScore")),
                home_score=_int(p.get("homeScore")),
            )
        )
    return plays


@dataclass(frozen=True)
class GoalDetail:
    """A goal as described in a soccer match's details, which name the assister."""

    minute: str
    scorer: str
    assist: str | None


def parse_goal_details(summary: dict) -> list[GoalDetail]:
    details = []
    for k in summary.get("keyEvents") or []:
        if not k.get("scoringPlay"):
            continue
        names = [(p.get("athlete") or {}).get("displayName", "") for p in _list(k.get("participants"))]
        if not names:
            continue
        own_goal = "own goal" in ((k.get("type") or {}).get("text") or "").lower()
        details.append(
            GoalDetail(
                minute=(k.get("clock") or {}).get("displayValue", ""),
                scorer=names[0],
                assist=names[1] if len(names) > 1 and not own_goal else None,
            )
        )
    return details


def _parse_event(event: dict, league: League) -> Game | None:
    from .odds import parse_odds  # odds.py imports Game from here
    comps = event.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if home is None or away is None:
        return None
    status = comp.get("status") or event.get("status") or {}
    stype = status.get("type", {})
    situation = comp.get("situation") or {}
    return Game(
        id=str(event.get("id")),
        league_key=league.key,
        home=_parse_team(home, league.sport),
        away=_parse_team(away, league.sport),
        state=stype.get("state", "pre"),
        status_name=stype.get("name", ""),
        detail=stype.get("shortDetail") or stype.get("detail", ""),
        start=event.get("date", ""),
        last_play=(situation.get("lastPlay") or {}).get("text"),
        goals=_parse_goals(comp.get("details") or []),
        leaders=_parse_leaders(comp, competitors),
        period=_int(status.get("period")),
        summary=status.get("summary") or "",
        path=league.path,
        odds=parse_odds(comp),
    )


def parse_scoreboard(data: dict, league: League) -> list[Game]:
    games = []
    for event in data.get("events", []):
        try:
            game = _parse_event(event, league)
        except Exception:
            # One oddly shaped game shouldn't stop updates for the whole league.
            log.exception("Skipping unreadable %s event %s", league.key, event.get("id"))
            continue
        if game is not None:
            games.append(game)
    return games


def is_international(event: dict) -> bool:
    """True for Tests, ODIs and T20Is (men's and women's)."""
    comp = (event.get("competitions") or [{}])[0]
    return str((comp.get("class") or {}).get("internationalClassId", "0")) not in ("", "0")


def parse_scorepanel(data: dict, league: League) -> list[Game]:
    """Every current international cricket match, across all series."""
    games = []
    for block in data.get("scores", []):
        series = (_list(block.get("leagues")) or [{}])[0].get("id")
        events = [e for e in block.get("events", []) if is_international(e)]
        for game in parse_scoreboard({"events": events}, league):
            # Each series has its own ESPN path, needed for ball-by-ball commentary.
            games.append(replace(game, path=f"{league.path}/{series}") if series else game)
    return games


@dataclass(frozen=True)
class Ball:
    """One delivery from a cricket match's ball-by-ball commentary."""

    sequence: int
    over: str  # e.g. "2.3"
    short: str  # e.g. "Seales to Shubman Gill, OUT"
    text: str  # full commentary
    kind: str  # "no run", "run", "four", "six", "out", "wide", ...
    team: str  # batting team abbreviation
    runs: int  # innings total after this ball
    wickets: int
    dismissal: str  # e.g. "Shubman Gill c †Hope b Seales 1 (6b 0x4 0x6)"; "" if none
    over_number: int
    over_complete: bool
    over_runs: int


def parse_balls(data: dict) -> tuple[list[Ball], int]:
    """Balls on one commentary page, plus how many pages there are."""
    commentary = data.get("commentary") or {}
    balls = []
    for it in _list(commentary.get("items")):
        short = (it.get("shortText") or "").strip()
        if not short:
            continue
        over, inn, out = it.get("over") or {}, it.get("innings") or {}, it.get("dismissal") or {}
        dismissal = ""
        if out.get("dismissal"):
            dismissal = re.sub(r"\s+SR: [\d.]+$", "", html.unescape(out.get("text") or "")).strip()
            dismissal = re.sub(r"\s{2,}", " ", dismissal)
        balls.append(
            Ball(
                sequence=_int(it.get("sequence")),
                over=str(over.get("actual", "")),
                short=short,
                text=html.unescape(it.get("text") or "").strip(),
                kind=(it.get("playType") or {}).get("description", ""),
                team=(it.get("team") or {}).get("abbreviation", ""),
                runs=_int(inn.get("runs")),
                wickets=_int(inn.get("wickets")),
                dismissal=dismissal,
                over_number=_int(over.get("number")),
                # ESPN flags the over complete a little after its last ball, so a
                # sixth legal delivery also counts.
                over_complete=bool(over.get("complete"))
                or (_int(over.get("ball")) >= 6 and (it.get("playType") or {}).get("description") not in ("wide", "no ball")),
                over_runs=_int(over.get("runs")),
            )
        )
    return balls, max(_int(commentary.get("pageCount")), 1)


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

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def scoreboard(self, league: League, date: str | None = None) -> list[Game]:
        """Current games, or a given day's (YYYYMMDD, by ESPN's US Eastern day)."""
        if league.feed == "scorepanel":
            data = await self._get_json(SCOREPANEL_URL.format(path=league.path))  # current matches only
            return parse_scorepanel(data, league)
        data = await self._get_json(BASE_URL.format(path=league.path), {"dates": date} if date else None)
        return parse_scoreboard(data, league)

    async def scoring_plays(self, league: League, event_id: str) -> list[ScoringPlay]:
        data = await self._get_json(SUMMARY_URL.format(path=league.path), {"event": event_id})
        return parse_scoring_plays(data, league.sport)

    async def teams(self, league: League) -> list[tuple[str, str]]:
        """(name, abbreviation) of every team in the league, for suggestions."""
        if league.feed == "scorepanel":
            names = dict.fromkeys(INTERNATIONAL_CRICKET_TEAMS, "")
            names.update((t.name, t.abbrev) for g in await self.scoreboard(league) for t in g.teams)
            return sorted(names.items())
        if league.sport == "cricket":
            data = await self._get_json(BASE_URL.format(path=league.path))
            raw = _list(data.get("teams"))
        else:
            data = await self._get_json(TEAMS_URL.format(path=league.path))
            raw = [t.get("team") or {} for s in _list(data.get("sports")) for l in _list(s.get("leagues")) for t in _list(l.get("teams"))]
        return sorted({(t.get("displayName", ""), t.get("abbreviation", "")) for t in raw if t.get("displayName")})

    async def summary(self, path: str, event_id: str) -> dict:
        """A game's full ESPN summary (odds, predictor, form, injuries, ...)."""
        return await self._get_json(SUMMARY_URL.format(path=path), {"event": event_id})

    async def balls(self, path: str, event_id: str, page: int | None = None) -> tuple[list[Ball], int]:
        params = {"event": event_id}
        if page:
            params["page"] = page
        return parse_balls(await self._get_json(PLAYBYPLAY_URL.format(path=path), params))

    async def goal_details(self, league: League, event_id: str) -> list[GoalDetail]:
        data = await self._get_json(SUMMARY_URL.format(path=league.path), {"event": event_id})
        return parse_goal_details(data)

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
