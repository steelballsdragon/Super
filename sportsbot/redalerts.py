"""🚨 Red alerts: total-shots props that DraftKings prices long for players who keep hitting them.

For each soccer game, DraftKings' "Shots Milestones" prices (1+, 2+, 3+ shots...) come from ESPN, which carries
DraftKings' odds. Each priced player's game log says how often he actually reached that many shots (all shots,
not just on target): over his last 10 games, this season and last season, weighted and adjusted for sample size,
the same way as the rest of the research. An alert is a line where that record says the bet comes in clearly
more often than DraftKings' price implies, at a price worth having: the player hits it consistently, the book
pays as if he doesn't. The best go out as singles, a parlay and a lotto, all graded after the games.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .espn import Game
from .props import PlayerGame, Leg, Rate, _rate, weighted

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
        american = str(over.get("american") or over.get("alternateDisplayValue") or "")
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

    @property
    def line(self) -> int:
        return self.price.line

    @property
    def edge(self) -> float:
        """Expected return per unit staked, if the record is right."""
        return self.chance * self.price.decimal - 1

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
        return " · ".join(parts)

    def leg(self) -> Leg:
        g = self.game
        return Leg(self.pick, self.chance, f"DK {self.price.american} · {self.evidence}", f"{g.away.name} @ {g.home.name}",
                   g.id, self.player_id, "prop", STAT, self.line, None, g.league_key, g.path, self.player, self.team,
                   g.start)


def alerts_for(player: str, pid: str, team: str, opponent: str, game: Game, games: list[PlayerGame],
               prices: dict[int, Price]) -> list[Alert]:
    """The player's priced lines that clear every bar (at most the best one per player)."""
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
        if chance is None or rates["l10"].pct < MIN_RECENT_RATE or chance < MIN_CHANCE:
            continue
        if price.decimal < MIN_DECIMAL or chance * price.decimal - 1 < MIN_EDGE:
            continue
        found.append(Alert(player, pid, team, opponent, game, price, chance, rates["l10"], rates["season"],
                           rates["last"], _rate(vs, STAT, line), seasons[0], seasons[1] if len(seasons) > 1 else ""))
    return sorted(found, key=lambda a: a.edge, reverse=True)[:1]


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
    def __init__(self, espn, props, availability):
        self.espn, self.props, self.availability = espn, props, availability  # availability(league, game id, path)

    async def shot_odds(self, game: Game) -> dict[str, dict[int, Price]]:
        sport, league = game.league.path.split("/", 1)
        url = PROPBETS_URL.format(sport=sport, league=league, eid=game.id, provider=DRAFTKINGS)

        async def fetch():
            return parse_shot_odds(await self.espn._get_json(url, {"lang": "en", "region": "us", "limit": 1000}))
        return await self.props.cached(f"dk-shots:{game.id}", PROPS_TTL, fetch)

    async def game_alerts(self, game: Game) -> list[Alert]:
        try:
            odds = await self.shot_odds(game)
        except Exception:
            log.warning("No DraftKings shot props for %s", game.id, exc_info=True)
            return []
        if not odds:
            return []
        try:
            available = await self.availability(game.league_key, game.id, game.path)
        except Exception:
            available = None
        roster = {}
        for team, opponent in ((game.home, game.away), (game.away, game.home)):
            for aid, (name, _) in (await self.props._roster(game.league.path, team.id)).items():
                roster[aid] = (name, team, opponent)
        # The likeliest shooters by DraftKings' own 1+ price (its starters), so a big slate stays a few hundred logs.
        priced = sorted((aid for aid in odds if aid in roster), key=lambda a: min(p.decimal for p in odds[a].values()))
        jobs = []
        for aid in priced[:PLAYERS_PER_GAME]:
            name, team, opponent = roster[aid]
            if available is not None and not available.allows(team.id, aid, name):
                continue
            jobs.append(self._player(aid, name, team, opponent, game, odds[aid]))
        return [a for found in await asyncio.gather(*jobs) for a in found]

    async def _player(self, aid, name, team, opponent, game, prices) -> list[Alert]:
        try:
            games, _ = await self.props.player_games(game.league.path, aid)
        except Exception:
            return []
        return alerts_for(name, aid, team.abbrev, opponent.abbrev, game, games, prices)


# ----- the posts -----

SMALL_SAMPLE = 8  # fewer recent games than this: flagged


def _line(i: int, a: Alert) -> str:
    small = " · ⚠️ small sample" if a.l10.games < SMALL_SAMPLE else ""
    return (f"**{i}. {a.pick}** · DK **{a.price.american}** ({a.game.away.name} @ {a.game.home.name})\n"
            f"  Hits it ~{a.chance:.0%} by his record, DK prices {a.price.implied:.0%} · edge {a.edge:+.0%} · "
            f"{a.evidence}{small}")


def singles_embed(alerts: list[Alert], day: str):
    import discord
    embed = discord.Embed(title=f"🚨 Red alerts · {day}: {len(alerts)} shot single{'s' if len(alerts) != 1 else ''}",
                          color=discord.Color.red(),
                          description="\n".join(_line(i, a) for i, a in enumerate(alerts, 1)))
    embed.add_field(name="Why these", value=(
        "Total shots (not just on target). Each player has reached this line far more often than DraftKings' price "
        "says: the edge is his record's chance x DK's payout, minus your stake. Records can't see injuries, "
        "rotation or a tough matchup, so stake small and check the lineups."), inline=False)
    embed.set_footer(text="Prices: DraftKings via ESPN, as of this post · graded here after the games")
    slip = "```\n" + "\n".join(f"{a.pick}  {a.price.american}" for a in alerts) + "\n```"
    return embed, slip


def combo_embed(alerts: list[Alert], lotto: bool):
    import discord
    dk = decimal_of(alerts)
    chance = 1.0
    for a in alerts:
        chance *= a.chance
    name = "Lotto" if lotto else "Parlay"
    embed = discord.Embed(title=f"🚨 Red alert {name.lower()}: {len(alerts)} legs at about {american_of(dk)} on DK",
                          color=discord.Color.dark_red() if lotto else discord.Color.red(),
                          description="\n".join(_line(i, a) for i, a in enumerate(alerts, 1)))
    embed.add_field(name=f"DK about {american_of(dk)} · fair {american_of(1 / chance) if chance else '—'}",
                    value=f"Every leg hits about **{chance:.0%}** of the time by these records (one leg per game, so "
                          f"they're close to independent); DraftKings pays as if it's {1 / dk:.1%}."
                          + (" A long shot: small stakes only." if lotto else ""), inline=False)
    embed.set_footer(text="Prices: DraftKings via ESPN, multiplied leg by leg · graded here after the games")
    slip = "```\n" + "\n".join(a.pick for a in alerts) + "\n```"
    return embed, slip
