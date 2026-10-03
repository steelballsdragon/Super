"""Follows ESPN's ball-by-ball cricket commentary for live matches."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .espn import Ball, Game

log = logging.getLogger(__name__)

FetchBalls = Callable[[str, str, "int | None"], Awaitable[tuple[list[Ball], int]]]


class BallFeed:
    """Returns the deliveries bowled since the last check, per match.

    The commentary is paged oldest-first, so we remember the last page read
    and continue from there. A match's first check only records where the
    commentary is up to, so following mid-match doesn't replay every ball.
    """

    def __init__(self, fetch: FetchBalls) -> None:
        self._fetch = fetch
        self._last_seq: dict[str, int] = {}
        self._page: dict[str, int] = {}

    async def new_balls(self, game: Game) -> list[Ball]:
        try:
            if game.id not in self._last_seq:
                _, pages = await self._fetch(game.path, game.id, None)
                latest, _ = await self._fetch(game.path, game.id, pages)
                self._page[game.id] = pages
                self._last_seq[game.id] = max((b.sequence for b in latest), default=0)
                return []
            page = self._page[game.id]
            balls, pages = await self._fetch(game.path, game.id, page)
            for extra in range(page + 1, pages + 1):
                more, _ = await self._fetch(game.path, game.id, extra)
                balls += more
            self._page[game.id] = pages
        except Exception:
            log.exception("Failed to fetch ball-by-ball commentary for match %s", game.id)
            return []
        new = sorted((b for b in balls if b.sequence > self._last_seq[game.id]), key=lambda b: b.sequence)
        if new:
            self._last_seq[game.id] = new[-1].sequence
        return new

    def forget_except(self, game_ids: set[str]) -> None:
        for gid in list(self._last_seq):
            if gid not in game_ids:
                self._last_seq.pop(gid, None)
                self._page.pop(gid, None)
