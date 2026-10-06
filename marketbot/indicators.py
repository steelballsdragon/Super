"""Technical indicators over numpy arrays. Every function returns an array as long as its input, with NaN
where there isn't enough history yet, and uses only past and current values (no look-ahead)."""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

TRADING_DAYS = 252


def _pad(values: np.ndarray, n: int) -> np.ndarray:
    return np.concatenate([np.full(n, np.nan), values])


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if len(x) < n:
        return np.full(len(x), np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    return _pad((c[n:] - c[:-n]) / n, n - 1)


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if len(x) < n:
        return np.full(len(x), np.nan)
    return _pad(sliding_window_view(x, n).std(axis=1, ddof=1), n - 1)


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if len(x) < n:
        return np.full(len(x), np.nan)
    return _pad(sliding_window_view(x, n).max(axis=1), n - 1)


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if len(x) < n:
        return np.full(len(x), np.nan)
    return _pad(sliding_window_view(x, n).min(axis=1), n - 1)


def shift(x: np.ndarray, n: int = 1) -> np.ndarray:
    """x moved n bars later (positive n: yesterday's value at today's index)."""
    x = np.asarray(x, dtype=float)
    if n == 0:
        return x.copy()
    out = np.full(len(x), np.nan)
    if n > 0:
        out[n:] = x[:-n]
    else:
        out[:n] = x[-n:]
    return out


def ema(x: np.ndarray, n: int, wilder: bool = False) -> np.ndarray:
    """Exponential moving average seeded with the first n-bar simple average (Wilder's smoothing if asked)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    start = np.argmax(np.isfinite(x)) if np.isfinite(x).any() else len(x)
    if len(x) - start < n:
        return out
    k = 1.0 / n if wilder else 2.0 / (n + 1)
    value = float(np.mean(x[start:start + n]))
    out[start + n - 1] = value
    keep = 1.0 - k
    for i in range(start + n, len(x)):
        v = x[i]
        if v == v:  # skip NaNs
            value = value * keep + v * k
        out[i] = value
    return out


def log_returns(close: np.ndarray) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), np.nan)
    out[1:] = np.log(close[1:] / close[:-1])
    return out


def realized_vol(close: np.ndarray, n: int) -> np.ndarray:
    """Daily standard deviation of log returns over the last n bars."""
    return rolling_std(log_returns(close), n)


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(np.asarray(close, dtype=float), prepend=np.nan)
    gain = ema(np.where(d > 0, d, 0.0)[1:], n, wilder=True)
    loss = ema(np.where(d < 0, -d, 0.0)[1:], n, wilder=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
        out = 100 - 100 / (1 + rs)
    out = np.where((loss == 0) & (gain > 0), 100.0, out)
    out = np.where((loss == 0) & (gain == 0), 50.0, out)
    return np.concatenate([[np.nan], out])


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def true_range(high, low, close) -> np.ndarray:
    prev = shift(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return ema(true_range(high, low, close), n, wilder=True)


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = rolling_std(close, n)
    return mid - k * sd, mid, mid + k * sd


def keltner(high, low, close, n: int = 20, k: float = 1.5):
    mid = ema(close, n)
    a = atr(high, low, close, n)
    return mid - k * a, mid, mid + k * a


def adx(high, low, close, n: int = 14) -> np.ndarray:
    up = np.diff(high, prepend=np.nan)
    down = -np.diff(low, prepend=np.nan)
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)
    tr = ema(true_range(high, low, close), n, wilder=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * ema(plus, n, wilder=True) / tr
        mdi = 100 * ema(minus, n, wilder=True) / tr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where(np.isfinite(dx), dx, np.nan)
    return ema(dx, n, wilder=True)


def stochastic(high, low, close, n: int = 14) -> np.ndarray:
    hh, ll = rolling_max(high, n), rolling_min(low, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        k = (close - ll) / (hh - ll)
    return np.where(np.isfinite(k), k, 0.5)


def obv(close, volume) -> np.ndarray:
    direction = np.sign(np.diff(close, prepend=close[0]))
    return np.cumsum(direction * volume)


def percentile_rank(x: np.ndarray, n: int) -> np.ndarray:
    """Where today's value sits among the last n values (0 = lowest, 1 = highest)."""
    x = np.asarray(x, dtype=float)
    if len(x) < n:
        return np.full(len(x), np.nan)
    w = sliding_window_view(x, n)
    last = w[:, -1:]
    valid = np.isfinite(w).sum(axis=1)
    rank = ((w < last).sum(axis=1) + 0.5 * ((w == last).sum(axis=1) - 1)) / np.maximum(valid - 1, 1)
    rank = np.where(np.isfinite(last[:, 0]) & (valid > n // 2), rank, np.nan)
    return _pad(rank, n - 1)


def drawdown(close: np.ndarray) -> np.ndarray:
    """Fall from the highest close so far (0 at a new high, -0.25 when 25% below it)."""
    peak = np.maximum.accumulate(close)
    return close / peak - 1


def swing_points(high: np.ndarray, low: np.ndarray, width: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Indexes of swing highs and lows: bars higher (lower) than `width` bars on each side."""
    if len(high) < 2 * width + 1:
        return np.array([], dtype=int), np.array([], dtype=int)
    wh = sliding_window_view(high, 2 * width + 1)
    wl = sliding_window_view(low, 2 * width + 1)
    centre_h, centre_l = wh[:, width], wl[:, width]
    highs = np.where((centre_h >= wh.max(axis=1)) & (centre_h > wh[:, :width].max(axis=1)))[0] + width
    lows = np.where((centre_l <= wl.min(axis=1)) & (centre_l < wl[:, :width].min(axis=1)))[0] + width
    return highs, lows
