"""Daily price history kept on disk, so the bot downloads each symbol's century of bars once and then only
the latest days."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .yahoo import Bars, YahooClient

log = logging.getLogger(__name__)

MEMORY_TTL = 10 * 60  # in-memory copies are re-checked against Yahoo after this
DISK_TTL = 6 * 3600  # a saved history is topped up when it's older than this
FULL_REFRESH = 7 * 86400  # and fully re-downloaded weekly (dividends re-adjust old prices)
OVERLAP_DAYS = 15
MEMORY_SYMBOLS = 60


def _filename(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", symbol) + ".npz"


def merge(old: Bars, new: Bars, tolerance: float = 0.002) -> Bars | None:
    """Old bars followed by the new ones; None when the overlapping closes disagree (prices were re-adjusted)."""
    if not len(new):
        return old
    if not len(old):
        return new
    # The old last bar may have been saved mid-day, so it isn't compared.
    common, i_old, i_new = np.intersect1d(old.t[:-1], new.t, return_indices=True)
    if len(common):
        ratio = new.close[i_new] / old.close[i_old]
        if np.nanmax(np.abs(ratio - 1)) > tolerance:
            return None
    keep = old.t < new.t[0]
    cat = lambda a, b: np.concatenate([a[keep], b])
    return Bars(new.symbol or old.symbol, cat(old.t, new.t), cat(old.open, new.open), cat(old.high, new.high),
                cat(old.low, new.low), cat(old.close, new.close), cat(old.volume, new.volume), new.meta)


class HistoryCache:
    def __init__(self, yahoo: YahooClient, folder: str | Path):
        self.yahoo = yahoo
        self.folder = Path(folder)
        self._memory: OrderedDict[str, tuple[float, Bars]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    async def daily(self, symbol: str, fresh: float = MEMORY_TTL) -> Bars:
        """The symbol's whole daily history (oldest first), at most `fresh` seconds old."""
        hit = self._memory.get(symbol)
        if hit and time.monotonic() - hit[0] < fresh:
            self._memory.move_to_end(symbol)
            return hit[1]
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            hit = self._memory.get(symbol)
            if hit and time.monotonic() - hit[0] < fresh:
                return hit[1]
            bars = await self._load(symbol, fresh)
            self._memory[symbol] = (time.monotonic(), bars)
            self._memory.move_to_end(symbol)
            while len(self._memory) > MEMORY_SYMBOLS:
                self._memory.popitem(last=False)
            return bars

    def cached(self, symbol: str) -> Bars | None:
        hit = self._memory.get(symbol)
        return hit[1] if hit else None

    async def _load(self, symbol: str, fresh: float) -> Bars:
        saved, info = self._read(symbol)
        now = time.time()
        if saved is not None and len(saved):
            age = now - info.get("fetched_at", 0)
            if age < min(DISK_TTL, max(fresh, 60)):
                return saved
            if now - info.get("full_at", 0) < FULL_REFRESH:
                try:
                    recent = await self.yahoo.daily(symbol, start=int(saved.t[-1]) - OVERLAP_DAYS * 86400)
                    merged = merge(saved, recent)
                    if merged is not None:
                        self._write(symbol, merged, {"fetched_at": now, "full_at": info.get("full_at", now)})
                        return merged
                except Exception:
                    log.warning("Couldn't top up %s; re-downloading it", symbol, exc_info=True)
        try:
            bars = await self.yahoo.daily(symbol)
        except Exception:
            if saved is not None and len(saved):
                log.warning("Using saved %s history; Yahoo didn't answer", symbol, exc_info=True)
                return saved
            raise
        if len(bars):
            self._write(symbol, bars, {"fetched_at": now, "full_at": now})
        return bars

    def _read(self, symbol: str) -> tuple[Bars | None, dict]:
        path = self.folder / _filename(symbol)
        try:
            with np.load(path, allow_pickle=False) as z:
                info = json.loads(str(z["info"]))
                bars = Bars(symbol, z["t"], z["open"], z["high"], z["low"], z["close"], z["volume"],
                            info.get("meta", {}))
            return bars, info
        except FileNotFoundError:
            return None, {}
        except Exception:
            log.warning("Saved history for %s is unreadable; downloading it again", symbol)
            path.unlink(missing_ok=True)
            return None, {}

    def _write(self, symbol: str, bars: Bars, info: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        keep_meta = {k: v for k, v in bars.meta.items() if isinstance(v, (str, int, float, bool))}
        payload = dict(bars.to_arrays(), info=np.array(json.dumps({**info, "meta": keep_meta})))
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".npz")
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez_compressed(f, **payload)
            os.replace(tmp, self.folder / _filename(symbol))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
