"""Cricket player trends, built from match scorecards.

ESPN has no game logs for cricket players, so history is rebuilt match by
match: for the IPL, every match day of this season and last (ESPN only has
recent seasons); for internationals, earlier matches in the current series
plus every match the bot has recorded since (history grows as matches are
played).

ESPN's match summary only carries the latest innings, so each scorecard is
rebuilt from the ball-by-ball commentary instead: every ball carries the
batter's and bowler's running totals, and the last one per player per innings
is their final line. One request returns a whole match.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, timedelta

from .espn import Game
from .props import PlayerGame, Prop, best_trends

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
PLAYBYPLAY_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/playbyplay"
WHOLE_MATCH = 2000  # commentary items per request; covers a full limited-overs match

IPL_MONTHS = ((3, 10), (6, 10))  # the IPL runs roughly mid-March to early June
SERIES_LOOKBACK_DAYS = 60
HISTORY_CACHE = 7 * 86400  # finished matches never change
KEEP_RECORDED = 400  # international scorecards kept in state

BATTING = [
    Prop("Runs", "runs", (10, 20, 30, 40, 50)),
    Prop("Fours", "fours", (1, 2, 3, 4)),
    Prop("Sixes", "sixes", (1, 2, 3)),
]
BOWLING = [Prop("Wickets", "wickets", (1, 2, 3))]
TOP_BATTERS, TOP_BOWLERS = 4, 3
MIN_APPEARANCES = 3


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Scorecard:
    event_id: str
    when: str
    teams: dict  # team id -> {"name": ..., "abbrev": ...}
    batting: list  # (player id, name, team id, runs, balls, fours, sixes)
    bowling: list  # (player id, name, team id, overs, wickets)

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "when": self.when, "teams": self.teams,
                "batting": self.batting, "bowling": self.bowling}

    @classmethod
    def from_dict(cls, d: dict) -> "Scorecard":
        return cls(d["event_id"], d["when"], d["teams"], [tuple(r) for r in d["batting"]], [tuple(r) for r in d["bowling"]])


def parse_scorecard(commentary: dict, event_id: str, when: str) -> Scorecard | None:
    """A full scorecard (every innings) rebuilt from ball-by-ball commentary."""
    items = ((commentary.get("commentary") or {}).get("items")) or []
    teams, bat, bowl = {}, {}, {}

    def team_of(t):
        tid = str((t or {}).get("id", ""))
        if tid and tid not in teams:
            teams[tid] = {"name": t.get("displayName") or t.get("name", ""), "abbrev": t.get("abbreviation", "")}
        return tid

    for it in sorted(items, key=lambda i: _i(i.get("sequence"))):
        innings = _i(it.get("period"))
        batting_team = team_of(it.get("team"))
        for key in ("batsman", "otherBatsman"):
            b = it.get(key) or {}
            a = b.get("athlete") or {}
            if a.get("id"):
                # Later balls carry bigger running totals; the last one is the final line.
                bat[(innings, str(a["id"]))] = (str(a["id"]), a.get("displayName", ""), team_of(b.get("team")) or batting_team,
                                                _i(b.get("totalRuns")), _i(b.get("faced")), _i(b.get("fours")), _i(b.get("sixes")))
        bw = it.get("bowler") or {}
        a = bw.get("athlete") or {}
        if a.get("id"):
            bowl[(innings, str(a["id"]))] = (str(a["id"]), a.get("displayName", ""), team_of(bw.get("team")),
                                             str(bw.get("overs", "")), _i(bw.get("wickets")))
    batting = [row for row in bat.values() if row[4] > 0 or row[3] > 0]  # faced a ball or scored
    bowling = list(bowl.values())
    if not batting and not bowling:
        return None
    return Scorecard(event_id, when, teams, batting, bowling)


def player_histories(cards: list[Scorecard], team_id: str, season_of) -> tuple[dict, dict]:
    """{player id: (name, [PlayerGame])} for this team's batters and bowlers, newest first."""
    bat, bowl = {}, {}
    for card in sorted(cards, key=lambda c: c.when, reverse=True):
        if team_id not in card.teams:
            continue
        opponent = next((t["abbrev"] or t["name"] for tid, t in card.teams.items() if tid != team_id), "")
        season = season_of(card)
        for pid, name, tid, runs, balls, fours, sixes in card.batting:
            if tid == team_id:
                bat.setdefault(pid, (name, []))[1].append(
                    PlayerGame(card.when, season, opponent, {"runs": runs, "fours": fours, "sixes": sixes}))
        for pid, name, tid, overs, wickets in card.bowling:
            if tid == team_id:
                bowl.setdefault(pid, (name, []))[1].append(PlayerGame(card.when, season, opponent, {"wickets": wickets}))
    return bat, bowl


def key_cricketers(bat: dict, bowl: dict) -> tuple[list, list]:
    batters = sorted((p for p in bat.items() if len(p[1][1]) >= MIN_APPEARANCES),
                     key=lambda p: -sum(g.stats["runs"] for g in p[1][1]))[:TOP_BATTERS]
    bowlers = sorted((p for p in bowl.items() if len(p[1][1]) >= MIN_APPEARANCES),
                     key=lambda p: -sum(g.stats["wickets"] for g in p[1][1]))[:TOP_BOWLERS]
    return batters, bowlers


class CricketHistory:
    """Collects scorecards for a team (cached), and records finished matches."""

    def __init__(self, props_client, state) -> None:
        self._props = props_client
        self._state = state

    async def _json(self, url, params=None):
        return await self._props._json(url, params, ttl=HISTORY_CACHE)

    async def _finished_ids_on(self, path: str, day: date, team_ids: set[str]) -> list[tuple[str, str]]:
        try:
            data = await self._json(SCOREBOARD_URL.format(path=path), {"dates": day.strftime("%Y%m%d")})
        except Exception:
            return []
        found = []
        for e in data.get("events") or []:
            comp = (e.get("competitions") or [{}])[0]
            if ((comp.get("status") or {}).get("type") or {}).get("state") != "post":
                continue
            ids = {str((c.get("team") or {}).get("id")) for c in comp.get("competitors") or []}
            if ids & team_ids:
                found.append((str(e.get("id")), e.get("date", "")))
        return found

    async def scorecards(self, game: Game) -> list[Scorecard]:
        team_ids = {game.home.id, game.away.id}
        today = date.today()
        days = []
        if game.league.feed == "scoreboard":  # IPL: this season and last
            for year in (today.year, today.year - 1):
                start, end = date(year, *IPL_MONTHS[0]), date(year, *IPL_MONTHS[1])
                days += [start + timedelta(d) for d in range((end - start).days + 1) if start + timedelta(d) < today]
        else:  # internationals: earlier matches in the current series
            days = [today - timedelta(d) for d in range(1, SERIES_LOOKBACK_DAYS + 1)]
        path = game.path
        found = await asyncio.gather(*(self._finished_ids_on(path, d, team_ids) for d in days))
        events = {eid: when for day in found for eid, when in day}
        cards = []

        async def one(eid, when):
            try:
                data = await self._json(PLAYBYPLAY_URL.format(path=path), {"event": eid, "limit": WHOLE_MATCH})
                return parse_scorecard(data, eid, when)
            except Exception:
                return None
        cards = [c for c in await asyncio.gather(*(one(e, w) for e, w in events.items())) if c]
        seen = {c.event_id for c in cards}
        cards += [Scorecard.from_dict(d) for eid, d in self._state.items("cricket_cards") if eid not in seen
                  and team_ids & set(d["teams"])]
        return cards

    async def record(self, game: Game) -> None:
        """Keeps a finished international match's scorecard, so history grows over time."""
        try:
            data = await self._props.espn._get_json(PLAYBYPLAY_URL.format(path=game.path),
                                                    {"event": game.id, "limit": WHOLE_MATCH})
        except Exception:
            return
        card = parse_scorecard(data, game.id, game.start)
        if card is None:
            return
        self._state.set("cricket_cards", game.id, card.to_dict())
        stored = sorted(self._state.items("cricket_cards"), key=lambda kv: kv[1].get("when", ""))
        for eid, _ in stored[:-KEEP_RECORDED]:
            self._state.delete("cricket_cards", eid)

    async def trends(self, game: Game, bigger: bool = False) -> list:
        cards = await self.scorecards(game)
        ipl = game.league.feed == "scoreboard"
        season_of = (lambda c: c.when[:4]) if ipl else (lambda c: "recent")
        found = []
        for team, opponent in ((game.home, game.away), (game.away, game.home)):
            bat, bowl = player_histories(cards, team.id, season_of)
            batters, bowlers = key_cricketers(bat, bowl)
            opp = opponent.abbrev or opponent.name
            for pid, (name, games) in batters:
                found += best_trends(name, pid, team.abbrev, opp, games, "cricket", bigger, props=BATTING)
            for pid, (name, games) in bowlers:
                found += best_trends(name, pid, team.abbrev, opp, games, "cricket", bigger, props=BOWLING)
        return sorted(found, key=lambda t: t.probability, reverse=True)
