"""Detects noteworthy changes between successive scoreboard snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from .espn import Game, Goal

KICKOFF = "kickoff"
SCORE = "score"
HALFTIME = "halftime"
FINAL = "final"


@dataclass(frozen=True)
class Update:
    kind: str
    game: Game
    new_goals: tuple[Goal, ...] = ()
    score_decreased: bool = False  # e.g. a goal overturned by VAR


def diff_game(prev: Game, cur: Game) -> list[Update]:
    updates: list[Update] = []
    if prev.state == "pre" and cur.state == "in":
        updates.append(Update(KICKOFF, cur))
    if (prev.home.score, prev.away.score) != (cur.home.score, cur.away.score):
        decreased = cur.home.score + cur.away.score < prev.home.score + prev.away.score
        new_goals = () if decreased else cur.goals[len(prev.goals):]
        updates.append(Update(SCORE, cur, new_goals, decreased))
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
