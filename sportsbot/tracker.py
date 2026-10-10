"""Detects noteworthy changes between successive scoreboard snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from .espn import Game, Goal, ScoringPlay

KICKOFF = "kickoff"
SCORE = "score"
PERIOD = "period"  # football/basketball/hockey: end of a quarter or period
HALFTIME = "halftime"
WICKET = "wicket"
INNINGS = "innings"  # cricket: innings break
OVERS = "overs"  # cricket: score every 5 (T20) or 10 (ODI/Test) overs
FINAL = "final"
CALLED_OFF = "called_off"  # postponed, cancelled, suspended or abandoned

# ESPN marks games that won't be played (or finished) today as over ("post"),
# so these must not be reported as a final result.
CALLED_OFF_WORDS = ("POSTPONED", "CANCEL", "SUSPENDED", "ABANDON")

def called_off(game: Game) -> bool:
    return any(w in game.status_name for w in CALLED_OFF_WORDS)


def _shootout_decided(game: Game) -> bool:
    """NHL games settled by a shootout: ESPN adds one 'goal' to the winner."""
    return game.league.sport == "hockey" and game.state == "post" and "SO" in game.detail.split("/")


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
    count: int = 1  # wickets that fell, or the over count reached (OVERS)
    edit: bool = False  # a correction to a scoring play already posted (e.g. ESPN named the scorer): edit that post
    # The score posted the moment it changed, before ESPN described the play: the key its posts are kept under, so
    # they're edited into the play when it shows up (edit=True), or removed if it was part of a play already posted
    # (drop=True).
    provisional: str = ""
    drop: bool = False


def _batting(game: Game) -> str | None:
    """ID of the team currently batting in a cricket match."""
    for team in game.teams:
        if team.innings and team.innings[-1].batting:
            return team.id
    return None


def _batting_innings(game: Game):
    """(team, innings) currently batting in a cricket match, if any."""
    for team in game.teams:
        if team.innings and team.innings[-1].batting:
            return team, len(team.innings)
    return None, 0


def over_interval(game: Game) -> int:
    """How often to post the score: every 5 overs in T20s and shorter, 10 otherwise."""
    overs = [t.max_overs for t in game.teams if t.max_overs]
    return 5 if overs and max(overs) <= 20 else 10


def overs_milestone(prev: Game, cur: Game) -> int | None:
    """The over count just reached (e.g. 10), if the batting side passed one."""
    prev_team, prev_n = _batting_innings(prev)
    team, n = _batting_innings(cur)
    if team is None or prev_team is None or team.id != prev_team.id or n != prev_n:
        return None
    every = over_interval(cur)
    before = int(prev_team.innings[-1].overs) // every
    after = int(team.innings[-1].overs) // every
    return after * every if after > before else None


def diff_game(prev: Game, cur: Game) -> list[Update]:
    sport = cur.league.sport
    updates: list[Update] = []
    if prev.state == "pre" and cur.state == "in":
        updates.append(Update(KICKOFF, cur))
    if (
        sport in PER_SCORE_SPORTS
        and (prev.home.score, prev.away.score) != (cur.home.score, cur.away.score)
        and not _shootout_decided(cur)  # the final post names the shootout winner
    ):
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
        elif fallen <= 0 and (milestone := overs_milestone(prev, cur)):
            # A wicket post already shows the score, so skip the over update then.
            updates.append(Update(OVERS, cur, count=milestone))
    if sport in ("football", "basketball", "hockey") and cur.status_name in END_OF_PERIOD and prev.status_name not in END_OF_PERIOD:
        updates.append(Update(PERIOD, cur))
    if cur.status_name == "STATUS_HALFTIME" and prev.status_name != "STATUS_HALFTIME":
        updates.append(Update(HALFTIME, cur))
    if prev.state != "post" and cur.state == "post":
        updates.append(Update(CALLED_OFF if called_off(cur) else FINAL, cur))
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

    def games(self, league_key: str) -> list[Game]:
        """The league's games as of the last snapshot."""
        return list(self._games.get(league_key, {}).values())

    def forget(self, league_key: str) -> None:
        self._games.pop(league_key, None)
