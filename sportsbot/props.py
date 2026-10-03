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
from dataclasses import dataclass, field
from datetime import date

from .espn import Game
from .leagues import LEAGUES

GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/{path}/athletes/{id}/gamelog"
LEADERS_URL = "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/seasons/{season}/types/2/teams/{team}/leaders"
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
}
MAX_PLAYERS = {"football": 5, "basketball": 4, "hockey": 4, "baseball": 4}


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


def _computed(stats: dict[str, float]) -> dict[str, float]:
    s = dict(stats)
    s["anytimeTouchdowns"] = s.get("rushingTouchdowns", 0) + s.get("receivingTouchdowns", 0)
    s["pra"] = s.get("points", 0) + s.get("totalRebounds", 0) + s.get("assists", 0)
    s["totalBases"] = s.get("hits", 0) + s.get("doubles", 0) + 2 * s.get("triples", 0) + 3 * s.get("homeRuns", 0)
    return s


def parse_gamelog(data: dict) -> tuple[list[PlayerGame], bool]:
    """Regular-season and playoff games (newest first), and whether this is a pitcher's log."""
    names = data.get("names") or []
    raw = {n: n for n in names}
    raw["threePointFieldGoalsMade-threePointFieldGoalsAttempted"] = "threesMade"
    events = data.get("events") or {}
    games = []
    for st in data.get("seasonTypes") or []:
        label = st.get("displayName") or ""
        if "Preseason" in label:
            continue
        season = label.split(" ")[0]
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                meta = events.get(ev.get("eventId")) or {}
                stats = {raw[n]: _f(v) for n, v in zip(names, ev.get("stats") or [])}
                games.append(PlayerGame(meta.get("gameDate", ""), season,
                                        (meta.get("opponent") or {}).get("abbreviation", ""), _computed(stats)))
    games.sort(key=lambda g: g.when, reverse=True)
    return games, "innings" in names


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
                bigger: bool = False) -> list[Trend]:
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
    for prop in PROPS[sport]:
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
        self._cache: dict[str, tuple[float, object]] = {}
        self._limit = asyncio.Semaphore(8)

    async def _json(self, url: str, params: dict | None = None):
        key = url + str(sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        async with self._limit:
            data = await self.espn._get_json(url, params)
        self._cache[key] = (time.monotonic(), data)
        return data

    async def key_players(self, league_key: str, team_id: str) -> list[tuple[str, str, str]]:
        """(athlete id, name, position) of a team's key players, from ESPN's team leaders."""
        league = LEAGUES[league_key]
        sport_path, league_slug = league.path.split("/")
        picks = LEADER_PICKS[league.sport]
        ids: list[str] = []
        year = date.today().year
        for season in (year + 1, year, year - 1):  # newest season that has leaders yet
            try:
                data = await self._json(LEADERS_URL.format(sport=sport_path, league=league_slug, season=season, team=team_id))
            except Exception:
                continue
            cats = {c.get("name"): c.get("leaders") or [] for c in data.get("categories") or []}
            for cat, count in picks:
                for leader in cats.get(cat, [])[:count]:
                    aid = (leader.get("athlete") or {}).get("$ref", "").split("/athletes/")[-1].split("?")[0]
                    if aid and aid not in ids:
                        ids.append(aid)
            if ids:
                break
        roster = await self._roster(league.path, team_id)
        players = [(aid, *roster[aid]) for aid in ids if aid in roster]
        if league.sport == "baseball":
            players = [p for p in players if p[2] not in ("SP", "RP", "P")]  # batters only
        return players[:MAX_PLAYERS[league.sport]]

    async def _roster(self, path: str, team_id: str) -> dict[str, tuple[str, str]]:
        try:
            data = await self._json(ROSTER_URL.format(path=path, team=team_id))
        except Exception:
            return {}
        groups = data.get("athletes") or []
        items = [i for grp in groups for i in (grp.get("items") or [])] if groups and "items" in groups[0] else groups
        return {str(i.get("id")): (i.get("displayName", "?"), (i.get("position") or {}).get("abbreviation", ""))
                for i in items if isinstance(i, dict)}

    async def player_games(self, path: str, athlete_id: str) -> tuple[list[PlayerGame], bool]:
        """This season's and last season's games for a player."""
        url = GAMELOG_URL.format(path=path, id=athlete_id)
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
        return games, pitcher

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


def trend_legs(game: Game, trends: list[Trend]) -> list[Leg]:
    label = f"{game.away.abbrev} @ {game.home.abbrev}"
    return [Leg(t.pick, t.probability, t.evidence, label, game.id, t.player_id) for t in trends]


def moneyline_leg(game: Game, chances: dict[str, float] | None, odds) -> Leg | None:
    if not chances or "draw" in chances:
        return None  # soccer moneylines can also draw, so they're left out
    tid = max((game.home.id, game.away.id), key=lambda t: chances[t])
    if chances[tid] < MONEYLINE_MIN:
        return None
    team = game.home if tid == game.home.id else game.away
    price = odds.home_ml if team is game.home else odds.away_ml
    return Leg(f"{team.name} Moneyline", chances[tid], f"{odds.provider} {price} → {chances[tid]:.0%} implied (no-vig)",
               f"{game.away.abbrev} @ {game.home.abbrev}", game.id)


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

NOTE = ("~% = past hit rate adjusted for sample size. It's history, not odds, and books price these trends in. "
        "Check prices (e.g. your odds bot) before betting.")


def trends_embed(game: Game, trends: list[Trend], ml: Leg | None) -> discord.Embed:
    a, b = game.teams
    embed = discord.Embed(title=f"📊 Trends: {a.name} vs {b.name}",
                          description=f"{game.league.emoji} {game.league.name} · most likely line per player and stat\n"
                                      f"*{trends[0].season_label if trends else ''} = this season · "
                                      f"{trends[0].last_label if trends else ''} = last season · L10 = last 10 games*",
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
            embed.add_field(name=f"{team.abbrev}", value=text, inline=False)
    if not trends:
        embed.add_field(name="No trends", value="Not enough game logs for this matchup's key players yet.", inline=False)
    embed.set_footer(text=NOTE)
    return embed


def parlay_embed(league_name: str, emoji: str, legs: list[Leg], style: str = "Safest") -> tuple[discord.Embed, str]:
    """The parlay with its evidence, plus a plain slip to copy or screenshot."""
    embed = discord.Embed(title=f"🎟️ {emoji} {league_name} parlay: {len(legs)} legs ({style})", color=discord.Color.purple())
    lines = [f"**{i}. {leg.pick}** ({leg.game})\n  ~{leg.probability:.0%} · {leg.evidence}" for i, leg in enumerate(legs, 1)]
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
