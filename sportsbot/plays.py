"""Turns NFL score changes into posts about the actual scoring plays."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .espn import Game, ScoringPlay
from .tracker import SCORE, Update

log = logging.getLogger(__name__)

# How many polls to wait for ESPN to publish a scoring play before falling
# back to a plain score update (at the default 30s interval, about 2 minutes).
MAX_ATTEMPTS = 4

FetchPlays = Callable[[str], Awaitable[list[ScoringPlay]]]


@dataclass
class _Pending:
    base_total: int
    attempts: int = 0


class PlayResolver:
    """Replaces NFL score updates with one update per new scoring play.

    ESPN's play-by-play can lag the scoreboard, so a score change stays
    pending until its play shows up (or we give up and post the bare score).
    Plays are remembered by ID so a touchdown isn't posted again when its
    extra point is added a moment later.
    """

    def __init__(self, fetch: FetchPlays) -> None:
        self._fetch = fetch
        self._posted: dict[str, set[str]] = {}
        self._pending: dict[str, _Pending] = {}

    async def resolve(self, games: list[Game], updates: list[Update]) -> list[Update]:
        resolved: list[Update] = []
        others: list[Update] = []
        for u in updates:
            if u.kind == SCORE and not u.score_decreased:
                pending = self._pending.get(u.game.id)
                base = u.prev_total if pending is None else min(pending.base_total, u.prev_total)
                self._pending[u.game.id] = _Pending(base)
            else:
                others.append(u)

        by_id = {g.id: g for g in games}
        for game_id in list(self._pending):
            game = by_id.get(game_id)
            if game is None:
                del self._pending[game_id]
                continue
            resolved.extend(await self._check(game, self._pending[game_id]))

        # Forget games that dropped off the scoreboard.
        for game_id in list(self._posted):
            if game_id not in by_id:
                del self._posted[game_id]
        return resolved + others

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
            posted.update(p.id for p in new)
            del self._pending[game.id]
            return [Update(SCORE, game, play=p) for p in new]
        if any(p.total == current_total and p.id in posted for p in plays):
            # The change was e.g. an extra point added to a touchdown already posted.
            del self._pending[game.id]
            return []
        pending.attempts += 1
        if pending.attempts >= MAX_ATTEMPTS:
            del self._pending[game.id]
            return [Update(SCORE, game)]
        return []
