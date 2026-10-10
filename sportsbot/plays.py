"""Turns NFL, MLB and NHL score changes into posts about the actual scoring plays."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from .espn import Game, Goal, GoalDetail, ScoringPlay
from .tracker import FINAL, SCORE, Update

log = logging.getLogger(__name__)

# How long to wait for ESPN to publish a scoring play before falling back to a
# plain score update.
PLAY_WAIT_SECONDS = 120
# Once the game is over, hold the final result at most this long for the
# winning play (e.g. a walk-off home run) so the play is posted first.
FINAL_HOLD_SECONDS = 60
# ESPN fills plays in after first publishing them (an NHL goal's scorer and assists, a touchdown's extra
# point) and corrects them (assists changed minutes later). A posted play is re-checked this often, for
# this long, and its post edited when it changes.
WATCH_CHECK_SECONDS = 20
WATCH_SECONDS = 15 * 60

Clock = Callable[[], float]

FetchPlays = Callable[[str], Awaitable[list[ScoringPlay]]]


@dataclass
class _Pending:
    base_total: int
    since: float
    held: list[Update] = field(default_factory=list)  # e.g. the final, posted after the play


class PlayResolver:
    """Replaces NFL score updates with one update per new scoring play.

    ESPN's play-by-play can lag the scoreboard, so a score change stays
    pending until its play shows up (or we give up and post the bare score).
    Plays are remembered by ID so a touchdown isn't posted again when its
    extra point is added a moment later.
    """

    def __init__(self, fetch: FetchPlays, clock: Clock = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._posted: dict[str, set[str]] = {}
        self._pending: dict[str, _Pending] = {}
        self._watching: dict[str, dict[str, tuple[ScoringPlay, float]]] = {}  # game -> play -> (as posted, when)
        self._checked: dict[str, float] = {}  # game -> when its posted plays were last re-checked

    async def resolve(self, games: list[Game], updates: list[Update]) -> list[Update]:
        resolved: list[Update] = []
        others: list[Update] = []
        for u in updates:
            if u.kind == SCORE and not u.score_decreased:
                pending = self._pending.get(u.game.id)
                if pending is None:
                    self._pending[u.game.id] = _Pending(u.prev_total, self._clock())
                else:
                    pending.base_total = min(pending.base_total, u.prev_total)
            else:
                others.append(u)

        # Anything else about a game with a score still pending (like the final
        # whistle after a walk-off) waits so it's posted after the scoring play.
        ready = []
        for u in others:
            pending = self._pending.get(u.game.id)
            (pending.held if pending else ready).append(u)

        by_id = {g.id: g for g in games}
        edits = await self._recheck(by_id)
        for game_id in list(self._pending):
            game = by_id.get(game_id)
            if game is None:
                resolved.extend(self._pending.pop(game_id).held)
                continue
            resolved.extend(await self._check(game, self._pending[game_id]))

        # Forget games that dropped off the scoreboard.
        for game_id in list(self._posted):
            if game_id not in by_id:
                del self._posted[game_id]
                self._watching.pop(game_id, None)
        return edits + resolved + ready

    async def _recheck(self, by_id: dict[str, Game]) -> list[Update]:
        """Edits for posted plays that ESPN has since filled in or corrected."""
        now, edits = self._clock(), []
        for game_id, watched in list(self._watching.items()):
            for pid, (_, at) in list(watched.items()):
                if now - at > WATCH_SECONDS:
                    del watched[pid]
            game = by_id.get(game_id)
            if not watched or game is None:
                self._watching.pop(game_id, None)
                continue
            if game_id in self._pending or now - self._checked.get(game_id, 0) < WATCH_CHECK_SECONDS:
                continue  # a new play is being looked up anyway; or checked a moment ago
            self._checked[game_id] = now
            try:
                plays = {p.id: p for p in await self._fetch(game_id)}
            except Exception:
                log.warning("Failed to re-check scoring plays for game %s", game_id, exc_info=True)
                continue
            for pid, (old, at) in list(watched.items()):
                new = plays.get(pid)
                if new is not None and (new.text, new.away_score, new.home_score) != (old.text, old.away_score, old.home_score):
                    watched[pid] = (new, at)
                    edits.append(Update(SCORE, game, play=new, edit=True))
        return edits

    async def _check(self, game: Game, pending: _Pending) -> list[Update]:
        try:
            plays = await self._fetch(game.id)
        except Exception:
            log.exception("Failed to fetch scoring plays for game %s", game.id)
            plays = []
        posted = self._posted.setdefault(game.id, set())
        current_total = game.home.score + game.away.score
        new = [p for p in plays if p.total > pending.base_total and p.id not in posted]
        if new:
            # Posted straight away, even if ESPN is still filling it in: the post is edited as it does.
            posted.update(p.id for p in new)
            now = self._clock()
            self._watching.setdefault(game.id, {}).update((p.id, (p, now)) for p in new)
            self._checked[game.id] = now
            del self._pending[game.id]
            return [Update(SCORE, game, play=p) for p in new] + pending.held
        if any(p.total == current_total and p.id in posted for p in plays):
            # The change was e.g. an extra point added to a touchdown already posted.
            del self._pending[game.id]
            return pending.held
        final_held = any(u.kind == FINAL for u in pending.held)
        waited = self._clock() - pending.since
        if waited >= PLAY_WAIT_SECONDS or (final_held and waited >= FINAL_HOLD_SECONDS):
            del self._pending[game.id]
            # The final already shows the score, so a bare score update would only repeat it.
            return pending.held if final_held else [Update(SCORE, game)] + pending.held
        return []


FetchGoalDetails = Callable[[str], Awaitable[list[GoalDetail]]]


def _waiting(goal: Goal) -> bool:
    """Whether a goal's assist is still to come (an own goal has none)."""
    return goal.assist is None and not goal.own_goal


@dataclass
class _Watched:
    update: Update  # as last posted
    since: float
    checked: float


class AssistResolver:
    """Adds the assister to soccer goal updates.

    The scoreboard only names the scorer, so each new goal is looked up in the
    match details. The goal posts straight away with whatever is known; if the
    assist isn't out yet, the post says it's being checked and is edited when
    ESPN publishes it (or FanDuel's extra assist, e.g. who won the penalty),
    for up to WATCH_SECONDS.
    """

    def __init__(self, fetch: FetchGoalDetails, clock: Clock = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._watching: list[_Watched] = []

    async def resolve(self, games: list[Game], updates: list[Update]) -> list[Update]:
        now, out = self._clock(), []
        edits = await self._recheck(now)
        for u in updates:
            if u.kind == SCORE and u.new_goals:
                goals = await self._with_assists(u.game.id, u.new_goals)
                u = replace(u, new_goals=goals)
                if any(_waiting(g) for g in goals):
                    self._watching.append(_Watched(u, now, now))
            out.append(u)
        return edits + out

    async def _recheck(self, now: float) -> list[Update]:
        edits = []
        for w in list(self._watching):
            expired = now - w.since >= WATCH_SECONDS
            if not expired and now - w.checked < WATCH_CHECK_SECONDS:
                continue
            w.checked = now
            goals = await self._with_assists(w.update.game.id, w.update.new_goals)
            if expired:  # no assist found in time: stop saying it's being checked
                goals = tuple(replace(g, assist="") if _waiting(g) else g for g in goals)
            if goals != w.update.new_goals:
                w.update = replace(w.update, new_goals=goals)
                edits.append(replace(w.update, edit=True))
            if expired or not any(_waiting(g) for g in goals):
                self._watching.remove(w)
        return edits

    async def _with_assists(self, game_id: str, goals: tuple[Goal, ...]) -> tuple[Goal, ...]:
        try:
            details = await self._fetch(game_id)
        except Exception:
            log.exception("Failed to fetch goal details for game %s", game_id)
            return goals
        found = {(d.minute, d.scorer): d for d in details}
        # assist "" means the goal was found and was unassisted; None means not found yet. An unassisted
        # goal also waits for the commentary, which shows FanDuel's extra assists (e.g. who won the penalty).
        out = []
        for g in goals:
            d = found.get((g.minute, g.scorer))
            if g.assist is not None or d is None or (not d.assist and not d.in_commentary):
                out.append(g)
            else:
                out.append(replace(g, assist=d.assist or "", fanduel=d.fanduel, fanduel_how=d.fanduel_how))
        return tuple(out)
