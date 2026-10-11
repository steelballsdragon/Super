"""🚨 Red alerts: total-shots props that DraftKings prices long for players who keep hitting them.

For each soccer game, DraftKings' "Shots Milestones" prices (1+, 2+, 3+ shots...) come from ESPN, which carries
DraftKings' odds. Each priced player's game log says how often he actually reached that many shots (all shots,
not just on target): over his last 10 games, this season and last season, weighted and adjusted for sample size,
the same way as the rest of the research. Then the matchup, when the teams' recent matches are on hand
(shotmodel):

- games his team's lineups show he came off the bench for are left out, so a few 0-shot cameos don't hide a
  starter's record;
- his record at home (or away, whichever this game is) counts for a fifth, once he has 5 such games;
- the opponent: a side whose opponents take 15% more shots than a typical team's makes every line likelier, and
  a high line more so.

An alert is a line where that chance says the bet comes in clearly more often than DraftKings' price implies, at a
price worth having: the player hits it consistently, the book pays as if he doesn't. The best go out as singles
(each with a stake: a quarter of the Kelly bet, in units of 1% of the bankroll), a parlay and a lotto, all graded
after the games. 💪 marks the strongest: a big edge on a full 10-game record. Close to kickoff, once both teams'
lineups are out, a second look keeps only confirmed starters (and DraftKings' newest prices).

The graded posts feed back in. A weekly report gives the singles' record and profit at DraftKings' prices, by
league, line and price, and each league's edge bar is tuned from its last 60 days: a league whose alerts have been
beating DraftKings needs a smaller edge, one whose alerts have been losing needs a bigger one.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import discord

from .espn import Game
from .leagues import LEAGUES
from .limits import MESSAGE, clip, fitted
from .props import PlayerGame, Leg, Rate, _rate, weighted
from .shotmodel import PastGame, adjust, league_average, opponent_factor, starts_only, venue_rate

log = logging.getLogger(__name__)

PROPBETS_URL = ("https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{eid}/competitions/{eid}"
                "/odds/{provider}/propBets")
DRAFTKINGS = "100"  # ESPN's id for DraftKings
SHOTS_MARKET = "Shots Milestones"  # all shots; "Shots on Target Milestones" is a different market
STAT = "totalShots"
MIN_DECIMAL = 1.80  # -125 or longer: prices worth having
MIN_CHANCE = 0.50  # the player hits this line at least half the time
MIN_EDGE = 0.15  # chance x DraftKings' payout at least 1.15: 15%+ expected return
MIN_RECENT_RATE = 0.60  # hit in at least 60% of his last (up to) 10 games
MIN_GAMES = 5  # games on record (ESPN has no earlier season for some players, so early on this is all there is)
PLAYERS_PER_GAME = 18  # the likeliest shooters by DraftKings' own 1+ price
SINGLES = 6
PARLAY_LEGS = 3
LOTTO_LEGS = (4, 6)
PROPS_TTL = 30 * 60
FRESH_PROPS_TTL = 5 * 60  # lineup-confirmed looks: DraftKings reprices as the lineups come out
HISTORY_GAMES = 10  # each team's recent matches: who started, and how many shots it conceded
MIN_VENUE_GAMES = 5  # home (or away) games before that record counts
VENUE_WEIGHT = 0.2
KELLY_SHARE = 0.25  # a quarter of the Kelly bet
UNIT = 0.01  # a stake unit here is 1% of the bankroll
STAKE_STEP, STAKE_RANGE = 0.25, (0.25, 2.0)
STRONG_EDGE, STRONG_GAMES, STRONG_CHANCE = 0.50, 10, 0.55  # 💪
NOTABLE_FACTOR = 0.03  # the opponent's effect is mentioned from 3% either way


@dataclass(frozen=True)
class Price:
    line: int  # N or more shots
    american: str
    decimal: float

    @property
    def implied(self) -> float:
        return 1 / self.decimal


def parse_shot_odds(data: dict) -> dict[str, dict[int, Price]]:
    """{athlete id: {line: DraftKings price}} for the total-shots market."""
    out: dict[str, dict[int, Price]] = {}
    for item in (data or {}).get("items") or []:
        if ((item.get("type") or {}).get("name") or "") != SHOTS_MARKET:
            continue
        m = re.search(r"/athletes/(\d+)", ((item.get("athlete") or {}).get("$ref") or ""))
        cur = item.get("current") or {}
        over, target = cur.get("over") or {}, cur.get("target") or {}
        try:
            line, dec = int(float(target.get("value"))), float(over.get("decimal"))
        except (TypeError, ValueError):
            continue
        if not m or line < 1 or dec <= 1:
            continue
        # The American price is what's saved with a post (and read back to grade its profit): never left blank.
        american = str(over.get("american") or over.get("alternateDisplayValue") or "") or american_of(dec)
        out.setdefault(m.group(1), {})[line] = Price(line, american, dec)
    return out


@dataclass(frozen=True)
class Alert:
    player: str
    player_id: str
    team: str  # abbreviation
    opponent: str
    game: Game
    price: Price
    chance: float
    l10: Rate
    season: Rate
    last: Rate
    vs: Rate
    season_label: str
    last_label: str
    factor: float = 1.0  # shots the opponent allows against a typical team's (1.12: 12% more)
    starts: int = 0  # games his record counts once bench appearances are left out (0: no lineups to go by)
    venue: Rate | None = None  # his record at this line at home (or away, if this game is away)
    confirmed: bool = False  # in a confirmed starting XI

    @property
    def line(self) -> int:
        return self.price.line

    @property
    def edge(self) -> float:
        """Expected return per unit staked, if the record is right."""
        return self.chance * self.price.decimal - 1

    @property
    def stake(self) -> float:
        """Units to stake (1 unit = 1% of the bankroll): a quarter of the Kelly bet, to the nearest 0.25, from 0.25
        to 2. Kelly is the share of the bankroll that grows it fastest if the chance is right; a quarter of it
        allows for the chance being too high, and rides out cold runs."""
        d = self.price.decimal
        kelly = (self.chance * d - 1) / (d - 1) if d > 1 else 0.0
        units = math.floor(kelly * KELLY_SHARE / UNIT / STAKE_STEP + 0.5) * STAKE_STEP
        return min(max(units, STAKE_RANGE[0]), STAKE_RANGE[1])

    @property
    def strong(self) -> bool:
        """💪: a big edge, on a full 10-game record, for a bet that comes in more often than not."""
        return self.edge >= STRONG_EDGE and self.l10.games >= STRONG_GAMES and self.chance >= STRONG_CHANCE

    @property
    def home(self) -> bool:
        return self.team == self.game.home.abbrev

    @property
    def pick(self) -> str:
        return f"{self.player} {self.line}+ Shots"

    @property
    def evidence(self) -> str:
        parts = [f"L10 {self.l10}"]
        if self.season.games:
            parts.append(f"{self.season_label} {self.season}")
        if self.last.games:
            parts.append(f"{self.last_label} {self.last}")
        if self.vs.games:
            parts.append(f"vs {self.opponent} {self.vs}")
        if self.venue is not None and self.venue.games:
            parts.append(f"{'home' if self.home else 'away'} {self.venue}")
        return " · ".join(parts)

    @property
    def matchup(self) -> str:
        """The opponent's effect (when it's 3% or more) and the games counted, e.g. "opponent allows +12% shots"."""
        parts = []
        if round(abs(self.factor - 1), 6) >= NOTABLE_FACTOR:
            parts.append(f"opponent allows {self.factor - 1:+.0%} shots")
        if self.starts:
            parts.append(f"{self.starts} games, bench left out")
        return " · ".join(parts)

    def leg(self) -> Leg:
        g = self.game
        return Leg(self.pick, self.chance, f"DK {self.price.american} · {self.evidence}",
                   f"{g.away.name} @ {g.home.name}", g.id, self.player_id, "prop", STAT, self.line, None, g.league_key,
                   g.path, self.player, self.team, g.start)


def alerts_for(player: str, pid: str, team: str, opponent: str, game: Game, games: list[PlayerGame],
               prices: dict[int, Price], factor: float = 1.0, home: bool | None = None, min_edge: float = MIN_EDGE,
               starts: int = 0) -> list[Alert]:
    """The player's priced lines that clear every bar (at most the best one per player).

    His chance at each line is his weighted record; with home given (True at home, False away), his record at this
    venue counts for a fifth once he has 5 such games; then the opponent's factor (shotmodel.adjust, over his last
    10 games). An alert needs that chance to beat DraftKings' price by min_edge. starts is passed through to the
    alert (the games left once bench appearances are dropped)."""
    if len(games) < MIN_GAMES:
        return []
    seasons: list[str] = []
    for g in games:
        if g.season not in seasons:
            seasons.append(g.season)
    current = [g for g in games if g.season == seasons[0]]
    last = [g for g in games if len(seasons) > 1 and g.season == seasons[1]]
    l10, vs = games[:10], [g for g in games if g.opponent == opponent]
    found = []
    for line, price in sorted(prices.items()):
        rates = {"l10": _rate(l10, STAT, line), "season": _rate(current, STAT, line), "last": _rate(last, STAT, line)}
        chance = weighted(rates)
        if chance is None or rates["l10"].pct < MIN_RECENT_RATE:
            continue
        venue = venue_rate(games, line, home) if home is not None else None
        if venue is not None and venue.games >= MIN_VENUE_GAMES:
            chance = (1 - VENUE_WEIGHT) * chance + VENUE_WEIGHT * venue.estimate
        chance = adjust(chance, l10, line, factor)
        if chance < MIN_CHANCE or price.decimal < MIN_DECIMAL or chance * price.decimal - 1 < min_edge:
            continue
        found.append(Alert(player, pid, team, opponent, game, price, chance, rates["l10"], rates["season"],
                           rates["last"], _rate(vs, STAT, line), seasons[0], seasons[1] if len(seasons) > 1 else "",
                           factor=factor, starts=starts, venue=venue))
    return sorted(found, key=lambda a: a.edge, reverse=True)[:1]


def without_bench(games: list[PlayerGame], history: list[PastGame], player_id: str) -> tuple[list[PlayerGame], int]:
    """His games without the ones his team's lineups show he didn't start (shotmodel.starts_only), and how many are
    left. (games, 0) when the lineups cover none of his games, or he started too few to be judged by his starts."""
    lineups: dict[str, set[str]] = {}
    for h in history:
        if h.starters:
            lineups.setdefault(h.event_id, set()).update(h.starters)
    pid = str(player_id)
    covered = [g for g in games if g.event_id in lineups]
    if not covered:
        return games, 0
    kept = starts_only(games, history, pid)
    if len(kept) == len(games) and any(pid not in lineups[g.event_id] for g in covered):
        return games, 0  # he came off the bench, but started too few to leave those out
    return kept, len(kept)


def shots_average(histories: Iterable[Iterable[PastGame]]) -> float:
    """A typical team's shots per game, from these teams' recent matches: both sides of each match count (each
    team's own shots, and its opponents'), so one strong attack doesn't make every opponent look stingy."""
    sides = []
    for history in histories:
        for g in history:
            sides.append(g)
            sides.append(PastGame(g.event_id, g.date, not g.home, g.shots_against, g.shots_for, frozenset(),
                                  frozenset()))
    return league_average(sides)


def spread(alerts: list[Alert], count: int, per_game: int = 1) -> list[Alert]:
    """The best alerts, at most per_game from any one game (legs in one game move together)."""
    out, used = [], {}
    for a in alerts:
        if used.get(a.game.id, 0) >= per_game:
            continue
        out.append(a)
        used[a.game.id] = used.get(a.game.id, 0) + 1
        if len(out) == count:
            break
    return out


def decimal_of(alerts: list[Alert]) -> float:
    d = 1.0
    for a in alerts:
        d *= a.price.decimal
    return d


def american_of(decimal: float) -> str:
    return f"+{(decimal - 1) * 100:.0f}" if decimal >= 2 else f"-{100 / (decimal - 1):.0f}"


class RedAlerts:
    """Finds a game's alerts. history (a shotmodel.MatchHistory) adds lineups and the opponent; without it the
    alerts go by the players' records alone. edges(league key) gives the edge a league's alerts need (MIN_EDGE when
    it isn't given, or fails)."""

    def __init__(self, espn, props, availability, history=None, edges: Callable[[str], float] | None = None):
        self.espn, self.props, self.availability = espn, props, availability  # availability(league, game id, path)
        self.history, self.edges = history, edges

    def min_edge(self, league_key: str) -> float:
        """The edge this league's alerts need: edges(league key), or MIN_EDGE when that's not given, None or fails."""
        if self.edges is None:
            return MIN_EDGE
        try:
            edge = self.edges(league_key)
            return MIN_EDGE if edge is None else float(edge)
        except Exception:
            log.warning("No tuned edge for %s", league_key, exc_info=True)
            return MIN_EDGE

    async def shot_odds(self, game: Game, fresh: bool = False) -> dict[str, dict[int, Price]]:
        """DraftKings' shot prices for the game (kept 30 minutes; fresh=True: kept 5, for the lineup-confirmed look)."""
        sport, league = game.league.path.split("/", 1)
        url = PROPBETS_URL.format(sport=sport, league=league, eid=game.id, provider=DRAFTKINGS)

        async def fetch():
            return parse_shot_odds(await self.espn._get_json(url, {"lang": "en", "region": "us", "limit": 1000}))
        if fresh:
            return await self.props.cached(f"dk-shots-fresh:{game.id}", FRESH_PROPS_TTL, fetch)
        return await self.props.cached(f"dk-shots:{game.id}", PROPS_TTL, fetch)

    async def _available(self, game: Game):
        try:
            return await self.availability(game.league_key, game.id, game.path)
        except Exception:
            return None

    async def _histories(self, game: Game) -> dict[str, list[PastGame]]:
        """{team id: its last HISTORY_GAMES matches} for both teams ({} without a match history)."""
        if self.history is None:
            return {}

        async def recent(team):
            try:
                return await self.history.recent(game.league.path, team.id, HISTORY_GAMES)
            except Exception:
                log.warning("No match history for %s", team.id, exc_info=True)
                return []
        home, away = await asyncio.gather(recent(game.home), recent(game.away))
        return {str(game.home.id): home, str(game.away.id): away}

    async def game_alerts(self, game: Game, confirmed_only: bool = False) -> list[Alert]:
        """The game's alerts, at most one per player. confirmed_only=True: [] until both teams' lineups are out,
        then only their starters' alerts, marked confirmed, at DraftKings' newest prices."""
        available = None
        if confirmed_only:
            available = await self._available(game)
            if available is None or not all(_announced(available, t) for t in (game.home, game.away)):
                return []
        try:
            odds = await self.shot_odds(game, fresh=confirmed_only)
        except Exception:
            log.warning("No DraftKings shot props for %s", game.id, exc_info=True)
            return []
        if not odds:
            return []
        if not confirmed_only:
            available = await self._available(game)
        sides = ((game.home, game.away), (game.away, game.home))
        rosters, histories = await asyncio.gather(
            asyncio.gather(*(self.props._roster(game.league.path, team.id) for team, _ in sides)),
            self._histories(game))
        average = shots_average(histories.values())
        roster = {}
        for (team, opponent), players in zip(sides, rosters):
            factor = opponent_factor(histories.get(str(opponent.id), []), average)
            for aid, (name, _) in players.items():
                roster[aid] = (name, team, opponent, factor, histories.get(str(team.id), []))
        # The likeliest shooters by DraftKings' own 1+ price (its starters), so a big slate stays a few hundred logs;
        # players who can't play (injured, or not in an announced XI) don't take a place.
        playing = [aid for aid in odds if aid in roster
                   and (available is None or available.allows(roster[aid][1].id, aid, roster[aid][0]))]
        priced = sorted(playing, key=lambda a: min(p.decimal for p in odds[a].values()))
        min_edge = self.min_edge(game.league_key)
        jobs = [self._player(aid, *roster[aid], game, odds[aid], min_edge, confirmed_only)
                for aid in priced[:PLAYERS_PER_GAME]]
        return [a for found in await asyncio.gather(*jobs) for a in found]

    async def _player(self, aid, name, team, opponent, factor, history, game, prices, min_edge, confirmed
                      ) -> list[Alert]:
        try:
            games, _ = await self.props.player_games(game.league.path, aid)
        except Exception:
            return []
        games, starts = without_bench(games, history, aid)
        found = alerts_for(name, aid, team.abbrev, opponent.abbrev, game, games, prices, factor=factor,
                           home=team is game.home, min_edge=min_edge, starts=starts)
        return [replace(a, confirmed=True) for a in found] if confirmed else found


def _announced(available, team) -> bool:
    """Whether the team's lineup is out (by abbreviation, or by id in case the summary's abbreviation differs)."""
    return available.announced(team.abbrev) or str(team.id) in (getattr(available, "lineups", None) or {})


# ----- the posts -----

SMALL_SAMPLE = 8  # fewer recent games than this: flagged


def _line(i: int, a: Alert, single: bool = True) -> str:
    small = " · ⚠️ small sample" if a.l10.games < SMALL_SAMPLE else ""
    head = (f"{'💪 ' if a.strong else ''}**{i}. {a.pick}** · DK **{a.price.american}** "
            f"({a.game.away.name} @ {a.game.home.name}){' · ✅ starting' if a.confirmed else ''}")
    odds = (f"Hits it ~{a.chance:.0%} by his record, DK prices {a.price.implied:.0%} · edge {a.edge:+.0%}"
            + (f" · stake {a.stake:g}u" if single else ""))
    why = " · ".join(p for p in (a.evidence, a.matchup) if p)
    return f"{head}\n  {odds} · {why}{small}"


@fitted
def singles_embed(alerts: list[Alert], day: str, confirmed: bool = False):
    title = "🚨✅ Lineup-confirmed red alerts" if confirmed else "🚨 Red alerts"
    embed = discord.Embed(title=f"{title} · {day}: {len(alerts)} shot single{'s' if len(alerts) != 1 else ''}",
                          color=discord.Color.red(),
                          description="\n".join(_line(i, a) for i, a in enumerate(alerts, 1)))
    embed.add_field(name="Why these", value=(
        "Total shots (not just on target). Each player has reached this line far more often than DraftKings' price "
        "says: the edge is his chance x DK's payout, minus your stake. His chance counts his starts only (once the "
        "lineups say who started), his home or away record, and how many shots the opponent gives up. "
        + ("Every player here is in his team's starting XI. " if confirmed else
           "Records can't see injuries or rotation, so check the lineups. ")
        + "Stakes are in units of 1% of your bankroll: a quarter of the Kelly bet, at most 2. 💪 = strongest."),
        inline=False)
    embed.set_footer(text="Prices: DraftKings via ESPN, as of this post · graded here after the games")
    slip = "```\n" + "\n".join(f"{a.pick}  {a.price.american}" for a in alerts) + "\n```"
    return embed, slip


@fitted
def combo_embed(alerts: list[Alert], lotto: bool):
    dk = decimal_of(alerts)
    chance = 1.0
    for a in alerts:
        chance *= a.chance
    name = "Lotto" if lotto else "Parlay"
    confirmed = bool(alerts) and all(a.confirmed for a in alerts)
    title = f"🚨✅ Lineup-confirmed red alert {name.lower()}" if confirmed else f"🚨 Red alert {name.lower()}"
    embed = discord.Embed(title=f"{title}: {len(alerts)} legs at about {american_of(dk)} on DK",
                          color=discord.Color.dark_red() if lotto else discord.Color.red(),
                          description="\n".join(_line(i, a, single=False) for i, a in enumerate(alerts, 1)))
    embed.add_field(name=f"DK about {american_of(dk)} · fair {american_of(1 / chance) if chance else '—'}",
                    value=f"Every leg hits about **{chance:.0%}** of the time by these records (one leg per game, so "
                          f"they're close to independent); DraftKings pays as if it's {1 / dk:.1%}."
                          + (" Every player is in his team's starting XI." if confirmed else "")
                          + (" A long shot: small stakes only." if lotto else ""), inline=False)
    embed.set_footer(text="Prices: DraftKings via ESPN, multiplied leg by leg · graded here after the games")
    slip = "```\n" + "\n".join(a.pick for a in alerts) + "\n```"
    return embed, slip


def strong_line(alerts: list[Alert]) -> str:
    """A message for pinging a role about the 💪 alerts ("" when there are none), with room for the mention."""
    strong = [a for a in alerts if a.strong]
    if not strong:
        return ""
    picks = " · ".join(f"**{a.pick}** (DK {a.price.american}, stake {a.stake:g}u)" for a in strong)
    return clip(f"💪 Strong red alert{'s' if len(strong) != 1 else ''}: {picks}", MESSAGE - 100)


# ----- how they've done, and the edge each league needs -----

STYLE = "Red alert"  # every red alert post's style starts with this
CONFIRMED_STYLE = " · lineups confirmed"  # added to the style of a lineup-confirmed post
TUNE_DAYS = 60
TUNE_MIN_LEGS = 20  # graded singles in a league before its edge bar moves
TUNE_ROI = 0.10  # ROI this far either side of break-even moves the bar
TUNED_EDGES = (0.10, 0.25)  # the bar for a league that's been winning, and for one that's been losing
PRICE_BANDS = ((2.5, "-125 to +150"), (4.0, "+151 to +300"), (math.inf, "+301 and longer"))


def post_style(kind: str, confirmed: bool = False) -> str:
    """The style a post is recorded under: kind "singles", "parlay" or "lotto"."""
    return f"{STYLE} {kind}" + (CONFIRMED_STYLE if confirmed else "")


def american_decimal(american: str) -> float | None:
    """DraftKings' payout per unit staked (stake included) for an American price: "+150" -> 2.5, "-125" -> 1.8."""
    text = str(american).strip().upper()
    if text in ("EVEN", "EV"):
        return 2.0
    try:
        value = float(text)
    except ValueError:
        return None
    if value >= 100:
        return 1 + value / 100
    if value <= -100:
        return 1 - 100 / value
    return None


def dk_decimal(evidence: str) -> float | None:
    """The DraftKings price saved in a leg's evidence ("DK +150 · L10 7/10 ...")."""
    m = re.search(r"\bDK ([+-]?\d+(?:\.\d+)?|EVEN|EV)(?![\w.])", evidence or "", re.IGNORECASE)
    return american_decimal(m.group(1)) if m else None


@dataclass(frozen=True)
class Record:
    """Graded singles at 1 unit each, at DraftKings' price when they were posted."""
    hits: int = 0
    misses: int = 0
    profit: float = 0.0  # units, over the singles with a price
    priced: int = 0  # singles with a price (all of them, unless one was saved without)
    predicted: float = 0.0  # the chances given, added up

    @property
    def graded(self) -> int:
        return self.hits + self.misses

    @property
    def win(self) -> float:
        return self.hits / self.graded if self.graded else 0.0

    @property
    def roi(self) -> float:
        return self.profit / self.priced if self.priced else 0.0

    def __str__(self) -> str:
        return f"{self.hits}-{self.misses} · {self.profit:+.2f}u ({self.roi:+.0%})"


def record_of(legs: Iterable[dict]) -> Record:
    """The record of graded legs (status "hit" or "miss", "decimal" their DK price or None)."""
    hits = misses = priced = 0
    profit = predicted = 0.0
    for leg in legs:
        hit = leg.get("status") == "hit"
        hits, misses = hits + hit, misses + (not hit)
        predicted += float(leg.get("probability") or 0)
        if decimal := leg.get("decimal"):
            priced += 1
            profit += decimal - 1 if hit else -1
    return Record(hits, misses, profit, priced, predicted)


def _seconds(when: datetime | float) -> float:
    return when.timestamp() if isinstance(when, datetime) else float(when)


def _red(parlays: Iterable[dict], start: float, end: float) -> list[dict]:
    """Red alert posts made between start and end, oldest first."""
    found = [p for p in parlays if str(p.get("style") or "").startswith(STYLE)
             and start <= float(p.get("created") or 0) <= end]
    return sorted(found, key=lambda p: float(p.get("created") or 0))


def _kind(parlay: dict) -> str:
    style = str(parlay.get("style") or "").lower()
    if parlay.get("round_robin") == 1 or "single" in style:
        return "singles"
    return "lotto" if "lotto" in style else "parlay"


def _pick_key(leg: dict) -> tuple:
    return leg.get("game_id"), leg.get("player_id") or leg.get("pick"), leg.get("stat"), leg.get("line")


def settled_singles(parlays: Iterable[dict], start: datetime | float, end: datetime | float) -> list[dict]:
    """Every single from red alert posts made between start and end that's been settled (hit, miss or void), each
    pick once (the same post in two channels is one pick), with "decimal" (its DK price) and "confirmed" added."""
    seen, out = set(), []
    for p in _red(parlays, _seconds(start), _seconds(end)):
        if _kind(p) != "singles":
            continue
        for leg in p.get("legs") or []:
            if leg.get("status") not in ("hit", "miss", "void") or _pick_key(leg) in seen:
                continue
            seen.add(_pick_key(leg))
            out.append({**leg, "decimal": dk_decimal(leg.get("evidence") or ""),
                        "confirmed": CONFIRMED_STYLE.strip(" ·") in str(p.get("style"))})
    return out


def tuned_edges(parlays: Iterable[dict], now: datetime | float, days: int = TUNE_DAYS) -> dict[str, float]:
    """{league key: the edge its alerts need} for leagues with 20+ graded, priced singles in the last `days`: 10% when
    they've returned +10% or better at 1 unit each, 25% at -10% or worse, else MIN_EDGE. Other leagues: not listed
    (they keep MIN_EDGE)."""
    end = _seconds(now)
    by_league: dict[str, list[dict]] = {}
    for leg in settled_singles(parlays, end - days * 86400, end):
        if leg["status"] in ("hit", "miss") and leg["decimal"]:
            by_league.setdefault(leg.get("league") or "", []).append(leg)
    out = {}
    for league, legs in by_league.items():
        if len(legs) < TUNE_MIN_LEGS:
            continue
        roi = round(record_of(legs).roi, 6)
        out[league] = TUNED_EDGES[0] if roi >= TUNE_ROI else TUNED_EDGES[1] if roi <= -TUNE_ROI else MIN_EDGE
    return out


def _league_name(key: str) -> str:
    return LEAGUES[key].name if key in LEAGUES else key or "Other"


def tuning_note(parlays: list[dict], since: datetime | float, now: datetime | float) -> str:
    """One plain line on what the auto-tuning changed between since and now."""
    before, after = tuned_edges(parlays, since), tuned_edges(parlays, now)
    changes = [f"{_league_name(k)} {before.get(k, MIN_EDGE):.0%} → {after.get(k, MIN_EDGE):.0%}"
               for k in sorted(set(before) | set(after), key=_league_name)
               if before.get(k, MIN_EDGE) != after.get(k, MIN_EDGE)]
    if changes:
        return ("Edge needed for an alert: " + ", ".join(changes)
                + ". Leagues beating DraftKings get a lower bar, losing ones a higher one.")
    tuned = [f"{_league_name(k)} {v:.0%}" for k, v in sorted(after.items(), key=lambda kv: _league_name(kv[0]))
             if v != MIN_EDGE]
    if tuned:
        return f"No changes this week. Edge needed: {', '.join(tuned)}; every other league {MIN_EDGE:.0%}."
    return (f"No changes: every league still needs a {MIN_EDGE:.0%} edge (a league is tuned once it has "
            f"{TUNE_MIN_LEGS} graded singles in {TUNE_DAYS} days).")


def _band(decimal: float) -> str:
    return next(label for top, label in PRICE_BANDS if decimal <= top)


def _breakdown(legs: list[dict], key, order=None) -> str:
    groups: dict = {}
    for leg in legs:
        groups.setdefault(key(leg), []).append(leg)
    names = sorted(groups, key=order or (lambda k: (-len(groups[k]), str(k))))
    return "\n".join(f"{name}: {record_of(groups[name])}" for name in names)


def _day(when: datetime | float) -> str:
    d = when if isinstance(when, datetime) else datetime.fromtimestamp(float(when), timezone.utc)
    return f"{d:%a %b} {d.day}"


@fitted
def report_embed(parlays: list[dict], since: datetime | float, now: datetime | float) -> discord.Embed:
    """The week's red alerts: the singles' record, profit and ROI at 1 unit each at DraftKings' prices (by league,
    line and price), parlays and lottos won and lost, and what the auto-tuning changed. Posts made between since
    and now count (times as datetimes or Unix seconds)."""
    parlays = list(parlays)
    settled = settled_singles(parlays, since, now)
    graded = [leg for leg in settled if leg["status"] in ("hit", "miss")]
    voids = len(settled) - len(graded)
    combos: dict[str, dict[str, int]] = {"parlay": {"won": 0, "lost": 0}, "lotto": {"won": 0, "lost": 0}}
    seen = set()
    for p in _red(parlays, _seconds(since), _seconds(now)):
        key = (_kind(p), tuple(sorted(map(str, map(_pick_key, p.get("legs") or [])))))
        if key[0] != "singles" and p.get("status") in ("won", "lost") and key not in seen:
            seen.add(key)
            combos[key[0]][p["status"]] += 1
    embed = discord.Embed(title="🚨 Red alerts this week", color=discord.Color.red())
    embed.set_footer(text=f"1 unit on every single at DraftKings' price when posted · {_day(since)} to {_day(now)}")
    played = sum(sum(c.values()) for c in combos.values())
    if not graded and not played:
        embed.description = ("Nothing graded this week: no red alerts went out, or their games haven't finished. "
                             "The record picks up with the next ones.")
        embed.add_field(name="Auto-tuning", value=tuning_note(parlays, since, now), inline=False)
        return embed
    lines = []
    if graded:
        rec = record_of(graded)
        lines.append(f"**Singles {rec.hits}-{rec.misses}** · won {rec.win:.0%}, predicted "
                     f"{rec.predicted / rec.graded:.0%}" + (f" · {voids} void" if voids else ""))
        lines.append(f"**{rec.profit:+.2f}u** at DraftKings' prices, 1 unit on each · ROI **{rec.roi:+.0%}**")
        if confirmed := [leg for leg in graded if leg["confirmed"]]:
            lines.append(f"✅ Lineup-confirmed singles: {record_of(confirmed)}")
    else:
        lines.append("No singles graded this week.")
    embed.description = "\n".join(lines)
    if graded:
        priced = [leg for leg in graded if leg["decimal"]]
        embed.add_field(name="By league", value=_breakdown(graded, lambda leg: _league_name(leg.get("league", ""))),
                        inline=False)
        embed.add_field(name="By line", value=_breakdown(graded, lambda leg: f"{leg.get('line')}+ shots",
                                                         order=lambda k: (len(k), k)), inline=False)
        if priced:
            bands = [label for _, label in PRICE_BANDS]
            embed.add_field(name="By price", value=_breakdown(priced, lambda leg: _band(leg["decimal"]),
                                                              order=bands.index), inline=False)
    if played:
        embed.add_field(name="Parlays and lottos", value=(
            f"Parlays {combos['parlay']['won']} won, {combos['parlay']['lost']} lost · "
            f"lottos {combos['lotto']['won']} won, {combos['lotto']['lost']} lost"), inline=False)
    embed.add_field(name="Auto-tuning", value=tuning_note(parlays, since, now), inline=False)
    return embed
