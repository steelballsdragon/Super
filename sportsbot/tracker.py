"""Detects noteworthy changes between successive scoreboard snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from .espn import Game, Goal, ScoringPlay

KICKOFF = "kickoff"
SCORE = "score"
PERIOD = "period"  # basketball/hockey: end of a quarter or period
HALFTIME = "halftime"
WICKET = "wicket"
INNINGS = "innings"  # cricket: innings break
FINAL = "final"

# Statuses meaning a basketball quarter or hockey period just ended.
END_OF_PERIOD = ("STATUS_END_PERIOD", "STATUS_INTERMISSION")

# Sports where every change in score is posted. Basketball scores change too
# often (posted per quarter instead) and cricket posts wickets, not runs.
PER_SCORE_SPORTS = ("football", "soccer", "baseball", "hockey")


@dataclass(frozen=True)
class Update:
    kind: str
    game: Game
    new_goals: tuple[Goal, ...] = ()
    score_decreased: bool = False  # e.g. a goal overturned by VAR
    prev_total: int = 0  # combined score before this update
    play: ScoringPlay | None = None  # NFL/MLB/NHL scoring play details, when known
    count: int = 1  # wickets that fell since the last snapshot


def _batting(game: Game) -> str | None:
    """ID of the team currently batting in a cricket match."""
    for team in game.teams:
        if team.innings and team.innings[-1].batting:
            return team.id
    return None


def diff_game(prev: Game, cur: Game) -> list[Update]:
    sport = cur.league.sport
    updates: list[Update] = []
    if prev.state == "pre" and cur.state == "in":
        updates.append(Update(KICKOFF, cur))
    if sport in PER_SCORE_SPORTS and (prev.home.score, prev.away.score) != (cur.home.score, cur.away.score):
        prev_total = prev.home.score + prev.away.score
        decreased = cur.home.score + cur.away.score < prev_total
        new_goals = () if decreased else cur.goals[len(prev.goals):]
        updates.append(Update(SCORE, cur, new_goals, decreased, prev_total))
    if sport == "cricket" and cur.state == "in":
        fallen = (cur.home.wickets + cur.away.wickets) - (prev.home.wickets + prev.away.wickets)
        if fallen > 0:
            updates.append(Update(WICKET, cur, count=fallen))
        if _batting(prev) is not None and _batting(cur) not in (None, _batting(prev)):
            updates.append(Update(INNINGS, cur))
    if sport in ("basketball", "hockey") and cur.status_name in END_OF_PERIOD and prev.status_name not in END_OF_PERIOD:
        updates.append(Update(PERIOD, cur))
    if cur.status_name == "STATUS_HALFTIME" and prev.status_name != "STATUS_HALFTIME":
        updates.append(Update(HALFTIME, cur))
    if prev.state != "post" and cur.state == "post":
        updates.append(Update(FINAL, cur))
    return updates


class Tracker:
    """Remembers the last snapshot per league and reports what changed.

    The first snapshot of a league is recorded silently so that starting the
    bot (or following a new league) doesn't flood channels with old results.
    """

    def __init__(self) -> None:
        self._games: dict[str, dict[str, Game]] = {}

    def update(self, league_key: str, games: list[Game]) -> list[Update]:
        previous = self._games.get(league_key)
        self._games[league_key] = {g.id: g for g in games}
        if previous is None:
            return []
        updates: list[Update] = []
        for game in games:
            prev = previous.get(game.id)
            if prev is not None:
                updates.extend(diff_game(prev, game))
        return updates

    def forget(self, league_key: str) -> None:
        self._games.pop(league_key, None)
