"""Linemate-style player trends: how often each line has hit, from ESPN game logs.

For a game, the key players on each team (ESPN's team leaders) are looked up,
their game logs for this season and last season are read, and every common
prop line is checked: last 10 games, this season, last season and against this
opponent. The highest line that has hit consistently is that player's "most
likely" line. These are historical frequencies, not odds: books know these
trends too, so check prices before betting.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date

from .espn import Game
from .leagues import LEAGUES

GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/{path}/athletes/{id}/gamelog"
LEADERS_URL = "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/seasons/{season}/types/{type}/teams/{team}/leaders"
ROSTER_URL = "https://site.web.api.espn.com/apis/site/v2/sports/{path}/teams/{team}/roster"

# A line counts as "most likely" when its estimated chance reaches the bar and it
# hit often enough in the last 10. Baseball hitting is far less consistent (even
# stars get a hit in only ~65% of games), so its bar is lower; its estimates are
# shown either way, so the difference stays visible.
BARS = {
    # sport: ((safest bar, safest last-10 minimum), (bigger-payout bar, last-10 minimum))
    "default": ((0.75, 0.70), (0.65, 0.60)),
    "baseball": ((0.60, 0.50), (0.40, 0.30)),
}
# Goalscorer and assist bets are long shots by nature (a top striker scores in maybe 40-50% of
# games), so they get their own bar: a real chance, and at least 2 of the last 10.
SCORER_STATS = ("totalGoals", "goalAssists", "goalOrAssist", "goals", "assists")
SCORER_BAR, SCORER_MIN_L10 = 0.22, 0.2
# Before the matchup adjustment, players a little under the bar are kept too: a good matchup can lift them over it.
SCORER_PRE_BAR = 0.15
WEIGHTS = {"l10": 0.45, "season": 0.30, "last": 0.25}
MIN_GAMES = {"l10": 5, "season": 3, "last": 8}
CACHE_SECONDS = 6 * 3600
# The server has 1 GB of memory, so only small, processed results are cached (never raw
# ESPN responses), the cache is capped, and expired entries are dropped.
CACHE_MAX_ENTRIES = 4000
PLAYERS_AT_ONCE = 6  # game logs fetched at the same time
SKIP_STATUSES = ("Out", "Doubtful", "Injured Reserve", "Suspension")


@dataclass(frozen=True)
class Prop:
    label: str  # e.g. "Rushing Yards"
    stat: str  # stat name in the game log, or a computed name below
    lines: tuple[int, ...]  # "N or more" thresholds, like a book's alternate lines
    anytime: str | None = None  # wording when the line is 1+ (e.g. "Anytime TD")


PROPS = {
    "football": [
        Prop("Passing Yards", "passingYards", (175, 200, 225, 250, 275, 300)),
        Prop("Passing TDs", "passingTouchdowns", (1, 2, 3)),
        Prop("Rushing Yards", "rushingYards", (25, 40, 50, 60, 75, 100)),
        Prop("Receptions", "receptions", (2, 3, 4, 5, 6, 7)),
        Prop("Receiving Yards", "receivingYards", (25, 40, 50, 60, 75, 100)),
        Prop("Touchdowns", "anytimeTouchdowns", (1,), anytime="Anytime TD"),
    ],
    "basketball": [
        Prop("Points", "points", (10, 15, 20, 25, 30, 35)),
        Prop("Rebounds", "totalRebounds", (4, 6, 8, 10, 12)),
        Prop("Assists", "assists", (2, 4, 6, 8, 10)),
        Prop("3-Pointers Made", "threesMade", (1, 2, 3, 4)),
        Prop("Pts + Reb + Ast", "pra", (20, 25, 30, 35, 40, 45, 50)),
    ],
    "hockey": [
        Prop("Shots on Goal", "shotsTotal", (2, 3, 4, 5)),  # books rarely offer 0.5
        Prop("Points", "points", (1, 2)),
        Prop("Goals", "goals", (1,), anytime="Anytime Goalscorer"),
        Prop("Assists", "assists", (1,), anytime="To Record an Assist"),
    ],
    "soccer": [
        Prop("Shots", "totalShots", (1, 2, 3, 4)),
        Prop("Shots on Target", "shotsOnTarget", (1, 2)),
        # Named as FanDuel lists them, so a slip can be matched as-is.
        Prop("Goals", "totalGoals", (1,), anytime="Anytime Goalscorer"),
        Prop("Assists", "goalAssists", (1,), anytime="To Record an Assist"),
        Prop("Goal or Assist", "goalOrAssist", (1,), anytime="To Score or Assist"),
        Prop("Fouls Committed", "foulsCommitted", (1, 2)),
    ],
    "baseball": [
        Prop("Hits", "hits", (1, 2)),
        Prop("Total Bases", "totalBases", (2, 3, 4)),  # 1+ total bases is the same as 1+ hits
        Prop("Runs", "runs", (1,)),
        Prop("RBIs", "RBIs", (1,)),
        Prop("Home Runs", "homeRuns", (1,), anytime="To Hit a Home Run"),
    ],
}

# Which team-leader categories pick a team's key players, and how many from each.
LEADER_PICKS = {
    "football": (("passingLeader", 1), ("rushingLeader", 2), ("receivingLeader", 3)),
    "basketball": (("pointsPerGame", 4), ("reboundsPerGame", 1), ("assistsPerGame", 1)),
    "hockey": (("points", 4),),
    "baseball": (("OPS", 3), ("homeRuns", 2), ("avg", 2)),
    "soccer": (("goalsLeaders", 3), ("assistsLeaders", 2), ("shotsOnTarget", 2)),
}
MAX_PLAYERS = {"football": 5, "basketball": 4, "hockey": 4, "baseball": 4, "soccer": 4}
# Leaders live under the regular season (type 2) for most leagues; soccer uses type 1.
LEADER_TYPES = (2, 1)


def _f(value) -> float:
    try:
        return float(str(value).split("-")[0])  # "3-7" (made-attempted) -> 3
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class PlayerGame:
    when: str  # ISO date
    season: str  # e.g. "2026", "2025-26"
    opponent: str  # abbreviation
    stats: dict[str, float]
    event_id: str = ""


def _computed(stats: dict[str, float]) -> dict[str, float]:
    s = dict(stats)
    s["anytimeTouchdowns"] = s.get("rushingTouchdowns", 0) + s.get("receivingTouchdowns", 0)
    s["pra"] = s.get("points", 0) + s.get("totalRebounds", 0) + s.get("assists", 0)
    s["totalBases"] = s.get("hits", 0) + s.get("doubles", 0) + 2 * s.get("triples", 0) + 3 * s.get("homeRuns", 0)
    s["goalOrAssist"] = s.get("totalGoals", 0) + s.get("goalAssists", 0)
    return s


def parse_gamelog(data: dict) -> tuple[list[PlayerGame], bool]:
    """Regular-season and playoff games (newest first), and whether this is a pitcher's log."""
    names = data.get("names") or []
    raw = {n: n for n in names}
    raw["threePointFieldGoalsMade-threePointFieldGoalsAttempted"] = "threesMade"
    events = data.get("events") or {}
    games = []
    shown = _season_shown(data)
    for st in data.get("seasonTypes") or []:
        label = st.get("displayName") or ""
        if "Preseason" in label:
            continue
        # Cup rounds are labelled "Final", "Quarterfinals"...: use ESPN's season field when it has one.
        season = shown or label.split(" ")[0]
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                meta = events.get(ev.get("eventId")) or {}
                stats = {raw[n]: _f(v) for n, v in zip(names, ev.get("stats") or [])}
                games.append(PlayerGame(meta.get("gameDate", ""), season,
                                        (meta.get("opponent") or {}).get("abbreviation", ""), _computed(stats),
                                        str(ev.get("eventId", ""))))
    games.sort(key=lambda g: g.when, reverse=True)
    return games, "innings" in names


def _season_shown(data: dict) -> str | None:
    """The season this game log covers, as ESPN displays it (e.g. "2025-26" or "2026")."""
    for f in data.get("filters") or []:
        if f.get("name") == "season":
            for o in f.get("options") or []:
                if o.get("value") == f.get("value"):
                    return o.get("displayValue") or o.get("value")
    return None


def seasons_from(data: dict) -> list[str]:
    """Season values ESPN offers for this player, newest first (e.g. ["2026", "2025", ...])."""
    for f in data.get("filters") or []:
        if f.get("name") == "season":
            return [o.get("value") for o in f.get("options") or [] if o.get("value")]
    return []


@dataclass(frozen=True)
class Rate:
    hits: int
    games: int

    @property
    def pct(self) -> float:
        return self.hits / self.games if self.games else 0.0

    @property
    def estimate(self) -> float:
        """Hit rate adjusted for sample size (Laplace): 10/10 -> 92%, 3/3 -> 80%, never 100%."""
        return (self.hits + 1) / (self.games + 2)

    def __str__(self) -> str:
        return f"{self.hits}/{self.games}"


@dataclass(frozen=True)
class Trend:
    player: str
    player_id: str
    team: str  # abbreviation
    prop: Prop
    line: int  # N or more
    l10: Rate
    season: Rate
    last: Rate
    vs: Rate
    season_label: str
    last_label: str
    opponent: str
    probability: float  # weighted hit rate
    note: str = ""  # e.g. the matchup adjustment behind the probability

    @property
    def pick(self) -> str:
        if self.line == 1 and self.prop.anytime:
            return f"{self.player} {self.prop.anytime}"
        return f"{self.player} Over {self.line - 0.5:g} {self.prop.label}"

    @property
    def evidence(self) -> str:
        parts = [f"L10 {self.l10}"]
        if self.season.games:
            parts.append(f"{self.season_label} {self.season}")
        if self.last.games:
            parts.append(f"{self.last_label} {self.last}")
        if self.vs.games:
            parts.append(f"vs {self.opponent} {self.vs}")
        if self.note:
            parts.append(self.note)
        return " · ".join(parts)


def _rate(games: list[PlayerGame], stat: str, line: int) -> Rate:
    return Rate(sum(g.stats.get(stat, 0) >= line for g in games), len(games))


def weighted(rates: dict[str, Rate]) -> float | None:
    usable = {k: r for k, r in rates.items() if r.games >= MIN_GAMES[k]}
    if "l10" not in usable:
        return None
    total = sum(WEIGHTS[k] for k in usable)
    return sum(WEIGHTS[k] * r.estimate for k, r in usable.items()) / total


def best_trends(name: str, pid: str, team: str, opponent: str, games: list[PlayerGame], sport: str,
                bigger: bool = False, props: list[Prop] | None = None, scorers: bool = False,
                bar: float | None = None) -> list[Trend]:
    """For each prop, the highest line this player has hit consistently.

    With bigger=True, each prop's smallest line (the near-certain "gimme") is
    skipped and the bar is lower, giving fewer, higher lines that pay more.
    With scorers=True, only goal and assist bets (anytime goal, to assist, goal
    or assist), held to the lower goalscorer bar.
    """
    target, min_l10 = BARS.get(sport, BARS["default"])[1 if bigger else 0]
    if scorers:
        props = [p for p in PROPS.get(sport, []) if p.stat in SCORER_STATS]
        target, min_l10, bigger = SCORER_BAR if bar is None else bar, SCORER_MIN_L10, False
    if not games:
        return []
    seasons = []
    for g in games:
        if g.season not in seasons:
            seasons.append(g.season)
    current = [g for g in games if g.season == seasons[0]]
    last = [g for g in games if len(seasons) > 1 and g.season == seasons[1]]
    l10 = games[:10]
    vs = [g for g in games if g.opponent == opponent]
    found = []
    for prop in props if props is not None else PROPS[sport]:
        best = None
        for line in prop.lines:
            if bigger and len(prop.lines) > 1 and line == prop.lines[0]:
                continue
            rates = {"l10": _rate(l10, prop.stat, line), "season": _rate(current, prop.stat, line),
                     "last": _rate(last, prop.stat, line)}
            p = weighted(rates)
            if p is None or p < target or rates["l10"].pct < min_l10:
                continue
            best = Trend(name, pid, team, prop, line, rates["l10"], rates["season"], rates["last"],
                         _rate(vs, prop.stat, line), seasons[0], seasons[1] if len(seasons) > 1 else "",
                         opponent, p)
        if best:
            found.append(best)
    return found


# ---------- matchup adjustment for goalscorer and assist bets ----------

@dataclass(frozen=True)
class Matchup:
    """Goals each team is expected to score in this game, next to what it usually scores."""
    expected: dict[str, float]  # team id -> expected goals
    usual: dict[str, float]  # team id -> goals per game recently
    source: str  # "market" (the betting line) or "form" (recent scoring vs the opponent's defence)
    games: int = 5  # how many recent games "usual" comes from


# How strongly the win chances split the expected goals: a -260 soccer favourite takes about 75% of them;
# hockey scores are closer than its win chances suggest.
GOAL_SHARE_SLOPE = {"soccer": 0.45, "hockey": 0.35}
RATIO_RANGE = (0.5, 1.8)
# A few games are a small sample (a cold spell isn't the team's level), so recent scoring is blended
# with a typical team's: about 1.4 goals a game in soccer, 3.0 in the NHL, worth this many games.
TYPICAL_GOALS = {"soccer": 1.4, "hockey": 3.0}
TYPICAL_WEIGHT = 4


def expected_goals(game: Game, chances: dict[str, float] | None, total: float | None, form: dict) -> Matchup | None:
    """Each team's expected goals: from the betting line when there is one (the total split by the win
    chances), otherwise from recent form (its scoring averaged with what the opponent concedes)."""
    home, away = game.home.id, game.away.id
    usual = {tid: f.scored for tid, f in (form or {}).items() if f.games >= 3}
    games = min((f.games for f in (form or {}).values()), default=5)
    if home not in usual or away not in usual:
        return None
    slope = GOAL_SHARE_SLOPE.get(game.league.sport)
    if slope and chances and total and home in chances and away in chances:
        share = min(max(0.5 + slope * (chances[home] - chances[away]), 0.15), 0.85)
        return Matchup({home: total * share, away: total * (1 - share)}, usual, "market", games)
    concede = {tid: f.allowed for tid, f in form.items() if f.games >= 3}
    if home not in concede or away not in concede:
        return None
    return Matchup({home: (usual[home] + concede[away]) / 2, away: (usual[away] + concede[home]) / 2}, usual, "form", games)


def apply_matchup(trends: list[Trend], game: Game, matchup: Matchup | None) -> list[Trend]:
    """Scales each goal and assist chance by how many goals the player's team is expected to score here
    against its usual (a player's goals come and go with his team's), then keeps those over the bar.

    The chance is treated as a scoring rate (Poisson): 1 - e^(-rate x ratio), so a 40% scorer in a game
    where his team should score 1.5x its usual becomes 54%, not 60%.
    """
    from dataclasses import replace
    from math import exp, log
    ids = {t.abbrev: t.id for t in game.teams}
    names = {t.id: t.name for t in game.teams}
    out = []
    for t in trends:
        tid = ids.get(t.team)
        if matchup and t.prop.stat in SCORER_STATS and tid in matchup.expected:
            typical = TYPICAL_GOALS.get(game.league.sport, matchup.usual[tid])
            usual = (matchup.games * matchup.usual[tid] + TYPICAL_WEIGHT * typical) / (matchup.games + TYPICAL_WEIGHT)
            ratio = min(max(matchup.expected[tid] / max(usual, 0.3), RATIO_RANGE[0]), RATIO_RANGE[1])
            p = 1 - exp(log(1 - min(t.probability, 0.95)) * ratio)
            why = "betting line" if matchup.source == "market" else "form vs this defence"
            t = replace(t, probability=min(max(p, 0.02), 0.9),
                        note=f"{names[tid]} expected {matchup.expected[tid]:.1f} goals ({why}), "
                             f"{matchup.usual[tid]:.1f} a game lately")
        if t.prop.stat not in SCORER_STATS or t.probability >= SCORER_BAR:
            out.append(t)
    return sorted(out, key=lambda t: t.probability, reverse=True)


class PropsClient:
    """Fetches key players and game logs (cached), and builds trends for a game."""

    def __init__(self, espn) -> None:
        self.espn = espn
        self._cache: dict[str, tuple[float, float, object]] = {}  # key -> (stored at, ttl, value)
        self._leader_season: dict[str, int] = {}  # league -> the season its team leaders were found under
        self._limit = asyncio.Semaphore(8)
        self._players = asyncio.Semaphore(PLAYERS_AT_ONCE)

    async def _json(self, url: str, params: dict | None = None):
        """Fetches from ESPN (at most 8 at a time). Not cached: callers cache what they keep."""
        async with self._limit:
            return await self.espn._get_json(url, params)

    async def cached(self, key: str, ttl: float, make, keep=None):
        """make()'s result, reused for ttl seconds (only if keep(result), when given, so an empty result
        from a temporary problem is tried again). Keep results small: this lives in memory."""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
        value = await make()
        if keep is not None and not keep(value):
            return value
        self._cache[key] = (now, ttl, value)
        if len(self._cache) > CACHE_MAX_ENTRIES:
            self._trim(now)
        return value

    def _trim(self, now: float) -> None:
        for key in [k for k, (at, ttl, _) in self._cache.items() if now - at >= ttl]:
            del self._cache[key]
        overflow = len(self._cache) - CACHE_MAX_ENTRIES
        if overflow > 0:  # still too many: drop the oldest
            for key in sorted(self._cache, key=lambda k: self._cache[k][0])[:overflow]:
                del self._cache[key]

    async def key_players(self, league_key: str, team_id: str) -> list[tuple[str, str, str]]:
        """(athlete id, name, position) of a team's key players, from ESPN's team leaders. Worked out once
        every few hours per team: finding the current season's leaders can take several requests."""
        return await self.cached(f"key-players:{league_key}:{team_id}", CACHE_SECONDS,
                                 lambda: self._find_key_players(league_key, team_id), keep=bool)

    async def _find_key_players(self, league_key: str, team_id: str) -> list[tuple[str, str, str]]:
        league = LEAGUES[league_key]
        sport_path, league_slug = league.path.split("/")
        picks = LEADER_PICKS[league.sport]
        ids: list[str] = []
        year = date.today().year
        seasons = [year + 1, year, year - 1]  # the newest season that has leaders yet
        if (known := self._leader_season.get(league_key)) in seasons:  # the season that worked for this league
            seasons.remove(known)
            seasons.insert(0, known)
        for season in seasons:
            for kind in LEADER_TYPES:
                url = LEADERS_URL.format(sport=sport_path, league=league_slug, season=season, type=kind, team=team_id)
                try:
                    found = await self.cached(url, CACHE_SECONDS, lambda url=url: self._leader_ids(url, picks))
                except Exception:
                    continue
                ids += [aid for aid in found if aid not in ids]
                if ids:
                    break
            if ids:
                self._leader_season[league_key] = season
                break
        roster = await self._roster(league.path, team_id)
        players = [(aid, *roster[aid]) for aid in ids if aid in roster]
        if league.sport == "baseball":
            players = [p for p in players if p[2] not in ("SP", "RP", "P")]  # batters only
        if league.sport == "soccer":
            players = [p for p in players if p[2] not in ("G", "GK")]  # no goalkeepers
        return players[:MAX_PLAYERS[league.sport]]

    async def _leader_ids(self, url: str, picks) -> list[str]:
        data = await self._json(url)
        cats = {c.get("name"): c.get("leaders") or [] for c in data.get("categories") or []}
        ids: list[str] = []
        for cat, count in picks:
            for leader in cats.get(cat, [])[:count]:
                aid = (leader.get("athlete") or {}).get("$ref", "").split("/athletes/")[-1].split("?")[0]
                if aid and aid not in ids:
                    ids.append(aid)
        return ids

    async def _roster(self, path: str, team_id: str) -> dict[str, tuple[str, str]]:
        url = ROSTER_URL.format(path=path, team=team_id)
        try:
            return await self.cached(url, CACHE_SECONDS, lambda: self._roster_now(url))
        except Exception:
            return {}

    async def _roster_now(self, url: str) -> dict[str, tuple[str, str]]:
        data = await self._json(url)
        groups = data.get("athletes") or []
        items = [i for grp in groups for i in (grp.get("items") or [])] if groups and "items" in groups[0] else groups
        return {str(i.get("id")): (i.get("displayName", "?"), (i.get("position") or {}).get("abbreviation", ""))
                for i in items if isinstance(i, dict)}

    async def player_games(self, path: str, athlete_id: str, fresh: bool = False) -> tuple[list[PlayerGame], bool]:
        """This season's and last season's games for a player (fresh=True skips the cache, for grading)."""
        url = GAMELOG_URL.format(path=path, id=athlete_id)
        if fresh:
            return _slim(parse_gamelog(await self._json(url)), path)
        return await self.cached(url, CACHE_SECONDS, lambda: self._both_seasons(url, path))

    async def _both_seasons(self, url: str, path: str) -> tuple[list[PlayerGame], bool]:
        # A game log is ~1 MB of JSON. Only a few players are fetched at once, and each log is cut down to the
        # stats used straight away, so a parlay over a week of games doesn't hold a hundred of them in memory.
        async with self._players:
            games, pitcher, seasons = await self._season(url)
            if len(seasons) > 1:
                try:
                    older, _, _ = await self._season(url, {"season": seasons[1]})
                    games += [g for g in older if g.season not in {x.season for x in games}]
                except Exception:
                    pass
        games.sort(key=lambda g: g.when, reverse=True)
        return games, pitcher

    async def _season(self, url: str, params: dict | None = None):
        data = await self._json(url, params)
        games, pitcher = _slim(parse_gamelog(data), url)
        return games, pitcher, seasons_from(data)

    async def game_trends(self, game: Game, injured: set[str] = frozenset(), bigger: bool = False,
                          scorers: bool = False) -> list[Trend]:
        """Every key player's most likely lines for this game, most likely first."""
        league = game.league
        trends: list[Trend] = []

        async def one(team, opponent, aid, name):
            if name in injured:
                return []
            try:
                games, pitcher = await self.player_games(league.path, aid)
            except Exception:
                return []
            return [] if pitcher else best_trends(name, aid, team.abbrev, opponent.abbrev, games, league.sport, bigger,
                                                  scorers=scorers, bar=SCORER_PRE_BAR if scorers else None)

        jobs = []
        for team, opponent in ((game.home, game.away), (game.away, game.home)):
            try:
                players = await self.key_players(game.league_key, team.id)
            except Exception:
                players = []
            jobs += [one(team, opponent, aid, name) for aid, name, _ in players]
        for found in await asyncio.gather(*jobs):
            trends += found
        return sorted(trends, key=lambda t: t.probability, reverse=True)


def _slim(parsed: tuple[list[PlayerGame], bool], path_or_url: str) -> tuple[list[PlayerGame], bool]:
    """Keeps only the stats the sport's props use: a player's two seasons drop from ~1 KB to ~0.3 KB a game."""
    sport_path = path_or_url.split("/sports/")[-1].split("/")[0]
    sport = {"football": "football", "basketball": "basketball", "hockey": "hockey",
             "baseball": "baseball", "soccer": "soccer"}.get(sport_path)
    games, pitcher = parsed
    if sport is None:
        return parsed
    keep = {p.stat for p in PROPS[sport]}
    return [PlayerGame(g.when, g.season, g.opponent, {k: v for k, v in g.stats.items() if k in keep}, g.event_id)
            for g in games], pitcher


def injured_names(summary: dict) -> set[str]:
    return {(i.get("athlete") or {}).get("displayName", "")
            for t in summary.get("injuries") or [] for i in t.get("injuries") or []
            if i.get("status") in SKIP_STATUSES}


# ---------- parlays ----------

MAX_LEGS_PER_GAME = 2  # legs in the same game move together, so keep a parlay spread out
MONEYLINE_MIN = 0.60  # favorites at least this likely (no-vig) can be legs
UNDERDOG_MIN = 0.25  # lotto underdogs: real chances only, no long shots


@dataclass(frozen=True)
class Leg:
    pick: str
    probability: float
    evidence: str
    game: str  # e.g. "IND @ WSH"
    game_id: str
    player_id: str | None = None
    # For grading after the game:
    kind: str = "prop"  # "prop" or "moneyline"
    stat: str | None = None  # game-log stat, e.g. "receptions"
    line: int | None = None  # "N or more"
    side: str | None = None  # moneyline: the team id picked
    league: str = ""
    path: str = ""  # ESPN path for this game, e.g. "football/nfl" or "cricket/24289"
    player: str = ""  # the player's name, for matching match commentary


def trend_legs(game: Game, trends: list[Trend]) -> list[Leg]:
    label = f"{game.away.name} @ {game.home.name}"
    return [Leg(t.pick, t.probability, t.evidence, label, game.id, t.player_id, "prop", t.prop.stat, t.line,
                None, game.league_key, game.path, t.player) for t in trends]


def moneyline_leg(game: Game, chances: dict[str, float] | None, odds, underdog: bool = False) -> Leg | None:
    """The clear favorite's moneyline, or with underdog=True the underdog's (for lottos), from the market."""
    if not chances or "draw" in chances:
        return None  # soccer moneylines can also draw, so they're left out
    pick = min if underdog else max
    tid = pick((game.home.id, game.away.id), key=lambda t: chances[t])
    if (chances[tid] < UNDERDOG_MIN) if underdog else (chances[tid] < MONEYLINE_MIN):
        return None
    team = game.home if tid == game.home.id else game.away
    price = odds.home_ml if team is game.home else odds.away_ml
    return Leg(f"{team.name} Moneyline", chances[tid], f"{odds.provider} {price} → {chances[tid]:.0%} implied (no-vig)",
               f"{game.away.name} @ {game.home.name}", game.id, None, "moneyline", None, None, tid,
               game.league_key, game.path)


@dataclass(frozen=True)
class Target:
    """A parlay's payout goal, as a range for the chance that every leg hits."""
    key: str
    name: str
    low: float  # chance of all legs hitting, lowest and highest allowed
    high: float
    aim: float  # the chance to get closest to
    bigger: bool  # use the higher, better-paying lines
    leg_max: float = 1.0  # build mostly from legs at most this likely (near-certain ones only fine-tune)
    min_legs: int = 2
    max_legs: int = 15


TARGETS = {
    # +100 is a 50% chance; +1000 is 1 in 11; +10000 is 1 in 101.
    "safe": Target("safe", "Safe (around +100)", 0.45, 0.55, 0.50, bigger=False),
    "big": Target("big", "Big payout (+1000 to +10000)", 1 / 101, 1 / 11, 0.035, bigger=True, leg_max=0.82),
    # +3000 is 1 in 31; +20000 is 1 in 201. Only 4-10 legs, so each leg pays more.
    "lotto": Target("lotto", "Lotto (4-10 legs, +3000 to +20000)", 1 / 201, 1 / 31, 1 / 80, bigger=True,
                    leg_max=0.72, min_legs=4, max_legs=10),
}
MAX_LEGS = max(t.max_legs for t in TARGETS.values())


def american(p: float) -> str:
    """Fair American odds for a chance p, e.g. 0.5 -> +100, 0.8 -> -400, 0.04 -> +2400."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    if p > 0.5:
        return f"-{round(100 * p / (1 - p))}"
    return f"+{round(100 * (1 - p) / p)}"


def build_to_target(legs: list[Leg], target: Target, per_game: int = MAX_LEGS_PER_GAME,
                    balance: bool = False) -> list[Leg]:
    """Adds the most likely legs until the parlay's estimated odds land in the target range.

    Once a leg can land it in range, the one landing closest to the aim (on a
    log scale, so +2400 is as near to +3500 as +5000 is) finishes the parlay.
    At most `per_game` legs per game and one leg per player. With balance=True
    the kinds of bet take turns (goalscorer, assist, goalscorer, ...).
    """
    from math import log
    chosen: list[Leg] = []
    counts: dict[str, int] = {}
    players: set[str] = set()
    # Most likely first, but a big payout is built from the better-paying legs, so it needs fewer of them.
    pool = sorted(legs, key=lambda l: (l.probability > target.leg_max, -l.probability))
    p = 1.0

    def allowed(leg):
        return counts.get(leg.game_id, 0) < per_game and not (leg.player_id and leg.player_id in players)

    def fewest_of_its_kind(found):
        if not balance or not found:
            return found
        used = {leg.stat: sum(c.stat == leg.stat for c in chosen) for leg in found}
        least = min(used.values())
        return [leg for leg in found if used[leg.stat] == least]

    while (p > target.high or len(chosen) < target.min_legs) and len(chosen) < target.max_legs:
        options = fewest_of_its_kind([leg for leg in pool if allowed(leg)])
        if not options:
            break
        last = len(chosen) + 1 >= target.min_legs  # this leg can finish the parlay
        landing = [leg for leg in options if last and target.low <= p * leg.probability <= target.high]
        leg = (min(landing, key=lambda l: abs(log(p * l.probability / target.aim))) if landing else options[0])
        chosen.append(leg)
        pool.remove(leg)
        counts[leg.game_id] = counts.get(leg.game_id, 0) + 1
        if leg.player_id:
            players.add(leg.player_id)
        p *= leg.probability
    return chosen


def combined(legs: list[Leg]) -> float:
    p = 1.0
    for leg in legs:
        p *= leg.probability
    return p


# ---------- Discord formatting ----------

import discord  # noqa: E402

from .limits import fitted  # noqa: E402

NOTE = ("~% = past hit rate adjusted for sample size. It's history, not odds, and books price these trends in. "
        "Check prices (e.g. your odds bot) before betting.")


@fitted
def trends_embed(game: Game, trends: list[Trend], ml: Leg | None) -> discord.Embed:
    a, b = game.teams
    legend = ["L10 = last 10 games"]
    if trends and trends[0].season_label and trends[0].season_label != "recent":
        legend.append(f"{trends[0].season_label} = most recent season")
    if trends and trends[0].last_label:
        legend.append(f"{trends[0].last_label} = the season before")
    embed = discord.Embed(title=f"📊 Trends: {a.name} vs {b.name}",
                          description=f"{game.league.emoji} {game.league.name} · most likely line per player and stat\n"
                                      f"*{' · '.join(legend)}*",
                          color=discord.Color.purple())
    if ml:
        embed.add_field(name="🏆 Moneyline", value=f"**{ml.pick}** · {ml.evidence}", inline=False)
    for team in (a, b):
        rows = [f"**~{t.probability:.0%}** {t.pick}\n  {t.evidence}" for t in trends if t.team == team.abbrev]
        if rows:
            text = ""
            for row in rows:
                if len(text) + len(row) + 1 > 1024:
                    break
                text += row + "\n"
            embed.add_field(name=team.name, value=text, inline=False)
    if not trends:
        if game.league.feed == "scorepanel":
            why = ("ESPN has no international match history for players, so I build it as matches finish "
                   "(earlier matches in this series count too). Players need 3+ recent matches; check back soon.")
        else:
            why = "No line has hit consistently enough for this matchup's key players yet."
        embed.add_field(name="No trends yet", value=why, inline=False)
    embed.set_footer(text=NOTE)
    return embed


def scorer_lines(trends: list[Trend], limit: int = 1000) -> str:
    """Goalscorer and assist chances for a game report, most likely first."""
    single = {}  # player -> his goal and assist chances
    for t in trends:
        if t.prop.stat in ("totalGoals", "goals", "goalAssists", "assists"):
            single.setdefault(t.player_id, []).append(t.probability)
    text = ""
    for t in sorted(trends, key=lambda t: t.probability, reverse=True):
        if t.prop.stat == "goalOrAssist" and any(abs(c - t.probability) < 0.005 for c in single.get(t.player_id, [])):
            continue  # only ever scores (or only assists): "score or assist" would just repeat that line
        row = f"**~{t.probability:.0%}** {t.pick} ({american(t.probability)})\n  {t.evidence}\n"
        if len(text) + len(row) > limit:
            break
        text += row
    return text.strip()


@fitted
def parlay_embed(league_name: str, emoji: str, legs: list[Leg], target: Target,
                 same_game: bool = False) -> tuple[discord.Embed, str]:
    """The parlay with its evidence and estimated odds, plus a plain slip to copy or screenshot."""
    count = f"{len(legs)} leg" + ("" if len(legs) == 1 else "s")
    chance = combined(legs)
    embed = discord.Embed(title=f"🎟️ {emoji} {league_name} · {target.name}: {count}", color=discord.Color.purple())
    lines = [f"**{i}. {leg.pick}** ({leg.game})\n  ~{leg.probability:.0%} · {leg.evidence}" for i, leg in enumerate(legs, 1)]
    if not target.low <= chance <= target.high or len(legs) < target.min_legs:
        why = "too few games or strong legs right now" if chance > target.high else "the legs available"
        lines.append(f"\n*Closest I could get is {american(chance)} ({why}).*")
    embed.description = "\n".join(lines)
    embed.add_field(
        name=f"Estimated odds: {american(chance)}",
        value=f"About a **{chance:.0%}** chance every leg hits, from the hit rates above. Books price these trends in, "
              "so your book's price will differ: check it with your odds bot. The estimate is optimistic: it treats "
              "the legs as independent.",
        inline=False,
    )
    if same_game:
        embed.add_field(name="Same-game parlay", value="Legs in one game move together (a goal can settle several), "
                        "so the real chance can be higher or lower than the estimate.", inline=False)
    if any(leg.stat in ("goalAssists", "goalOrAssist") for leg in legs):
        embed.add_field(name="Assists, FanDuel rules", value="Assist legs settle like FanDuel: winning a penalty or "
                        "free kick that's scored, a saved or blocked shot turned in, or forcing an own goal counts too. "
                        "The hit rates use official assists, so these legs hit a little more often than shown.",
                        inline=False)
    embed.set_footer(text=("" if same_game else f"At most {MAX_LEGS_PER_GAME} legs per game unless you pick a team or game. ")
                     + NOTE)
    slip = "```\n" + "\n".join(leg.pick for leg in legs) + "\n```"
    return embed, slip
