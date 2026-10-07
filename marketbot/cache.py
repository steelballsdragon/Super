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

from .yahoo import Bars

log = logging.getLogger(__name__)

MEMORY_TTL = 10 * 60  # in-memory copies are re-checked against Yahoo after this
DISK_TTL = 6 * 3600  # a saved history is topped up when it's older than this
FULL_REFRESH = 7 * 86400  # and fully re-downloaded weekly (dividends re-adjust old prices)
OVERLAP_DAYS = 15
MEMORY_SYMBOLS = 60
DAY = 86400
PRIMARY = "Yahoo"  # its prices are adjusted for dividends; the backups' aren't


def _filename(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", symbol) + ".npz"


def merge(old: Bars, new: Bars, tolerance: float = 0.002) -> Bars | None:
    """Old bars followed by the new ones; None when the overlapping closes disagree (prices were re-adjusted).
    Bars are matched by day, since sources stamp the same day differently."""
    if not len(new):
        return old
    if not len(old):
        return new
    d_old, d_new = old.t // DAY, new.t // DAY
    # The old last bar may have been saved mid-day, so it isn't compared.
    common, i_old, i_new = np.intersect1d(d_old[:-1], d_new, return_indices=True)
    if len(common):
        ratio = new.close[i_new] / old.close[i_old]
        if np.nanmax(np.abs(ratio - 1)) > tolerance:
            return None
    return _join(old, d_old < d_new[0], 1.0, new)


def splice(old: Bars, new: Bars) -> Bars:
    """The new bars, with the older history in front of them rescaled to meet them where they overlap. Used for a
    backup source, whose prices aren't adjusted for dividends like Yahoo's, so its shorter history never replaces
    the long one already saved."""
    if not len(new) or not len(old):
        return new if len(new) else old
    d_old, d_new = old.t // DAY, new.t // DAY
    keep = d_old < d_new[0]
    common, i_old, i_new = np.intersect1d(d_old, d_new, return_indices=True)
    factor = 1.0
    if len(common):
        f = new.close[i_new[0]] / old.close[i_old[0]]
        if np.isfinite(f) and 0.01 < f < 100:
            factor = float(f)
    return _join(old, keep, factor, new)


def _join(old: Bars, keep: np.ndarray, factor: float, new: Bars) -> Bars:
    cat = lambda a, b, f=1.0: np.concatenate([a[keep] * f, b])
    meta = {**old.meta, **new.meta}
    return Bars(new.symbol or old.symbol, np.concatenate([old.t[keep], new.t]), cat(old.open, new.open, factor),
                cat(old.high, new.high, factor), cat(old.low, new.low, factor), cat(old.close, new.close, factor),
                cat(old.volume, new.volume), meta)


def source_of(bars: Bars) -> str:
    return str(bars.meta.get("source") or PRIMARY)


class HistoryCache:
    def __init__(self, data, folder: str | Path):
        self.data = data  # anything with daily(symbol, start) -> Bars: the MarketData hub
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
        have = saved is not None and len(saved) > 0
        if have:
            age = now - info.get("fetched_at", 0)
            if age < min(DISK_TTL, max(fresh, 60)):
                return saved
            if now - info.get("full_at", 0) < FULL_REFRESH:
                try:
                    recent = await self.data.daily(symbol, start=int(saved.t[-1]) - OVERLAP_DAYS * DAY)
                    merged = merge(saved, recent)
                    if merged is None and source_of(recent) != PRIMARY:
                        merged = splice(saved, recent)
                    if merged is not None:
                        self._write(symbol, merged, {"fetched_at": now, "full_at": info.get("full_at", now)})
                        return merged
                except Exception:
                    log.warning("Couldn't top up %s; re-downloading it", symbol, exc_info=True)
        try:
            bars = await self.data.daily(symbol)
        except Exception:
            if have:
                log.warning("Using saved %s history; no source answered", symbol, exc_info=True)
                return saved
            raise
        full_at = now
        if source_of(bars) != PRIMARY:
            # A backup's history is shorter and unadjusted: keep any long saved one in front of it, and try for a
            # full Yahoo download again next time.
            if have:
                bars = splice(saved, bars)
            full_at = info.get("full_at", 0)
        if len(bars):
            self._write(symbol, bars, {"fetched_at": now, "full_at": full_at})
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
