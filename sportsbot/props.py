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
WEIGHTS = {"l10": 0.45, "season": 0.30, "last": 0.25}
MIN_GAMES = {"l10": 5, "season": 3, "last": 8}
CACHE_SECONDS = 6 * 3600
# The server has 1 GB of memory, so only small, processed results are cached (never raw
# ESPN responses), the cache is capped, and expired entries are dropped.
CACHE_MAX_ENTRIES = 4000
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
        Prop("Goals", "goals", (1,), anytime="Anytime Goal"),
        Prop("Assists", "assists", (1,)),
    ],
    "soccer": [
        Prop("Shots", "totalShots", (1, 2, 3, 4)),
        Prop("Shots on Target", "shotsOnTarget", (1, 2)),
        Prop("Goals", "totalGoals", (1,), anytime="Anytime Goal"),
        Prop("Assists", "goalAssists", (1,), anytime="To Assist"),
        Prop("Goal or Assist", "goalOrAssist", (1,), anytime="Goal or Assist"),
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
                bigger: bool = False, props: list[Prop] | None = None) -> list[Trend]:
    """For each prop, the highest line this player has hit consistently.

    With bigger=True, each prop's smallest line (the near-certain "gimme") is
    skipped and the bar is lower, giving fewer, higher lines that pay more.
    """
    target, min_l10 = BARS.get(sport, BARS["default"])[1 if bigger else 0]
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


class PropsClient:
    """Fetches key players and game logs (cached), and builds trends for a game."""

    def __init__(self, espn) -> None:
        self.espn = espn
        self._cache: dict[str, tuple[float, float, object]] = {}  # key -> (stored at, ttl, value)
        self._limit = asyncio.Semaphore(8)

    async def _json(self, url: str, params: dict | None = None):
        """Fetches from ESPN (at most 8 at a time). Not cached: callers cache what they keep."""
        async with self._limit:
            return await self.espn._get_json(url, params)

    async def cached(self, key: str, ttl: float, make):
        """make()'s result, reused for ttl seconds. Keep results small: this lives in memory."""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
        value = await make()
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
        """(athlete id, name, position) of a team's key players, from ESPN's team leaders."""
        league = LEAGUES[league_key]
        sport_path, league_slug = league.path.split("/")
        picks = LEADER_PICKS[league.sport]
        ids: list[str] = []
        year = date.today().year
        for season in (year + 1, year, year - 1):  # newest season that has leaders yet
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
        latest = await self._json(url)
        games, pitcher = parse_gamelog(latest)
        seasons = seasons_from(latest)
        if len(seasons) > 1:
            try:
                older, _ = parse_gamelog(await self._json(url, {"season": seasons[1]}))
                games += [g for g in older if g.season not in {x.season for x in games}]
            except Exception:
                pass
        games.sort(key=lambda g: g.when, reverse=True)
        return _slim((games, pitcher), url)

    async def game_trends(self, game: Game, injured: set[str] = frozenset(), bigger: bool = False) -> list[Trend]:
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
            return [] if pitcher else best_trends(name, aid, team.abbrev, opponent.abbrev, games, league.sport, bigger)

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


def trend_legs(game: Game, trends: list[Trend]) -> list[Leg]:
    label = f"{game.away.name} @ {game.home.name}"
    return [Leg(t.pick, t.probability, t.evidence, label, game.id, t.player_id, "prop", t.prop.stat, t.line,
                None, game.league_key, game.path) for t in trends]


def moneyline_leg(game: Game, chances: dict[str, float] | None, odds) -> Leg | None:
    if not chances or "draw" in chances:
        return None  # soccer moneylines can also draw, so they're left out
    tid = max((game.home.id, game.away.id), key=lambda t: chances[t])
    if chances[tid] < MONEYLINE_MIN:
        return None
    team = game.home if tid == game.home.id else game.away
    price = odds.home_ml if team is game.home else odds.away_ml
    return Leg(f"{team.name} Moneyline", chances[tid], f"{odds.provider} {price} → {chances[tid]:.0%} implied (no-vig)",
               f"{game.away.name} @ {game.home.name}", game.id, None, "moneyline", None, None, tid,
               game.league_key, game.path)


def build_parlay(legs: list[Leg], size: int) -> list[Leg]:
    """The most likely legs, at most two per game and one per player."""
    chosen: list[Leg] = []
    per_game: dict[str, int] = {}
    players: set[str] = set()
    for leg in sorted(legs, key=lambda l: l.probability, reverse=True):
        if per_game.get(leg.game_id, 0) >= MAX_LEGS_PER_GAME or (leg.player_id and leg.player_id in players):
            continue
        chosen.append(leg)
        per_game[leg.game_id] = per_game.get(leg.game_id, 0) + 1
        if leg.player_id:
            players.add(leg.player_id)
        if len(chosen) == size:
            break
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


@fitted
def parlay_embed(league_name: str, emoji: str, legs: list[Leg], style: str = "Safest",
                 requested: int | None = None) -> tuple[discord.Embed, str]:
    """The parlay with its evidence, plus a plain slip to copy or screenshot."""
    count = f"{len(legs)} leg" + ("" if len(legs) == 1 else "s")
    embed = discord.Embed(title=f"🎟️ {emoji} {league_name} parlay: {count} ({style})", color=discord.Color.purple())
    lines = [f"**{i}. {leg.pick}** ({leg.game})\n  ~{leg.probability:.0%} · {leg.evidence}" for i, leg in enumerate(legs, 1)]
    if requested and len(legs) < requested:
        lines.append(f"\n*Only {len(legs)} of {requested} legs: there aren't enough games right now "
                     f"(max {MAX_LEGS_PER_GAME} legs per game).*")
    embed.description = "\n".join(lines)[:4000]
    embed.add_field(
        name="Chance all legs hit (estimate)",
        value=f"**{combined(legs):.0%}**\n*Optimistic: it assumes the legs are independent, and the best-looking trends "
              "from many players tend to overstate themselves.*",
        inline=False,
    )
    embed.set_footer(text="At most 2 legs per game. " + NOTE)
    slip = "```\n" + "\n".join(leg.pick for leg in legs) + "\n```"
    return embed, slip
