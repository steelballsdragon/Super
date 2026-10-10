"""Follows ESPN's ball-by-ball cricket commentary for live matches."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .espn import Ball, Game
from .settings import StateStore

log = logging.getLogger(__name__)

FetchBalls = Callable[[str, str, "int | None"], Awaitable[tuple[list[Ball], int]]]


# More "new" balls than this on the first check after starting (about two overs)
# means ESPN's first answer was out of date, or the bot was off for a while:
# start from the latest ball instead of posting a backlog.
CATCH_UP_BALLS = 12


class BallFeed:
    """Returns the deliveries bowled since the last check, per match.

    The commentary is paged oldest-first, so we remember the last page read
    and continue from there. A match's first check only records where the
    commentary is up to, so following mid-match doesn't replay every ball.
    With a state store, the position survives restarts, so an update or
    reboot neither repeats balls nor skips them.
    """

    def __init__(self, fetch: FetchBalls, state: StateStore | None = None, section: str = "balls") -> None:
        self._fetch = fetch
        self._state, self._section = state, section
        self._last_seq: dict[str, int] = {}
        self._page: dict[str, int] = {}
        for game_id, (seq, page) in state.items(section) if state else ():
            self._last_seq[game_id], self._page[game_id] = seq, page
        self._starting: set[str] = set(self._last_seq)  # first check since we started on these

    async def new_balls(self, game: Game) -> list[Ball]:
        try:
            if game.id not in self._last_seq:
                _, pages = await self._fetch(game.path, game.id, None)
                latest, _ = await self._fetch(game.path, game.id, pages)
                self._page[game.id] = pages
                self._last_seq[game.id] = max((b.sequence for b in latest), default=0)
                self._starting.add(game.id)
                self._save(game.id)
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
        if game.id in self._starting:
            self._starting.discard(game.id)
            if len(new) > CATCH_UP_BALLS:
                log.info("Skipping %d older balls in match %s to start from the latest", len(new), game.id)
                self._last_seq[game.id] = new[-1].sequence
                self._save(game.id)
                return []
        if new:
            self._last_seq[game.id] = new[-1].sequence
        if new or page != pages:
            self._save(game.id)
        return new

    def forget_except(self, game_ids: set[str]) -> None:
        for gid in list(self._last_seq):
            if gid not in game_ids:
                self._last_seq.pop(gid, None)
                self._page.pop(gid, None)
                self._starting.discard(gid)
                if self._state:
                    self._state.delete(self._section, gid)

    def _save(self, game_id: str) -> None:
        if self._state:
            self._state.set(self._section, game_id, [self._last_seq[game_id], self._page[game_id]])
