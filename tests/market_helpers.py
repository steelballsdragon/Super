"""Synthetic price histories for the market bot's tests (no network)."""

import numpy as np

from marketbot.yahoo import Bars, Quote

DAY = 86400
START = 315532800  # 1980-01-01


def walk(n=3000, seed=1, drift=0.0003, vol=0.012, symbol="TEST", start=START, volume=True, step=DAY):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(r))
    open_ = close * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    vol_ = rng.lognormal(15, 0.3, n) if volume else np.zeros(n)
    t = start + np.arange(n, dtype=np.int64) * step
    return Bars(symbol, t, open_, high, low, close, vol_)


def from_closes(closes, symbol="TEST", start=START, volume=1e6):
    c = np.asarray(closes, dtype=float)
    t = start + np.arange(len(c), dtype=np.int64) * DAY
    return Bars(symbol, t, c.copy(), c * 1.002, c * 0.998, c.copy(), np.full(len(c), float(volume)))


def quote(symbol="TEST", price=100.0, change=1.0, state="REGULAR", t=None, quote_type="EQUITY", **kw):
    return Quote(symbol=symbol, name=symbol, price=price, prev_close=price / (1 + change / 100), change_pct=change,
                 market_state=state, time=t or 1_791_000_000, quote_type=quote_type, **kw)
