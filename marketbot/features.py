"""Turns price history into model inputs (one row per day, using only what was known that day) and the
outcomes the models learn to predict."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import indicators as ind
from .yahoo import Bars

FEATURES = [
    "ret_1d", "ret_5d", "ret_20d", "ret_60d", "ret_120d", "ret_250d",
    "vs_sma20", "vs_sma50", "vs_sma200", "sma50_slope", "sma200_slope",
    "rsi14", "rsi2", "macd_hist", "bb_width_rank", "vol_ratio", "atr_rank",
    "volume_z", "vs_high250", "vs_low250", "days_since_high", "drawdown",
    "up_days20", "range_tightness", "adx", "stoch", "close_location", "gap", "donchian_pos",
]
FEATURE_LABELS = {
    "ret_1d": "yesterday's move", "ret_5d": "1-week momentum", "ret_20d": "1-month momentum",
    "ret_60d": "3-month momentum", "ret_120d": "6-month momentum", "ret_250d": "12-month momentum",
    "vs_sma20": "distance from 20-day average", "vs_sma50": "distance from 50-day average",
    "vs_sma200": "distance from 200-day average", "sma50_slope": "50-day trend slope",
    "sma200_slope": "200-day trend slope", "rsi14": "RSI(14)", "rsi2": "short-term overbought/oversold (RSI 2)",
    "macd_hist": "MACD histogram", "bb_width_rank": "Bollinger width (squeeze)", "vol_ratio": "volatility vs normal",
    "atr_rank": "daily range vs the past year", "volume_z": "volume vs normal", "vs_high250": "distance from 52-week high",
    "vs_low250": "distance from 52-week low", "days_since_high": "time since 52-week high",
    "drawdown": "drop from all-time high", "up_days20": "share of up days (20d)", "range_tightness": "10-day range tightness",
    "adx": "trend strength (ADX)", "stoch": "stochastic", "close_location": "close within today's range",
    "gap": "opening gap", "donchian_pos": "position in 20-day range",
}
HORIZONS = (5, 20, 60)
BREAKOUT_DAYS = 10
BREAKOUT_LOOKBACK = 20
WARMUP = 260


@dataclass
class FeatureSet:
    X: np.ndarray  # rows x FEATURES (NaN where unknown)
    valid: np.ndarray  # rows with every feature known
    vol20: np.ndarray  # daily volatility, for scaling moves
    close: np.ndarray


def _safe(x):
    return np.where(np.isfinite(x), x, np.nan)


def build(bars: Bars) -> FeatureSet:
    c, h, l, o, v = bars.close, bars.high, bars.low, bars.open, bars.volume
    n = len(c)
    lc = np.log(c)
    r = ind.log_returns(c)
    vol20 = ind.rolling_std(r, 20)
    vol60 = ind.rolling_std(r, 60)
    vol250 = ind.rolling_std(r, 250)
    with np.errstate(divide="ignore", invalid="ignore"):
        def ret(k, vol):
            return (lc - ind.shift(lc, k)) / (vol * np.sqrt(k))

        def dist(avg, k):
            return np.log(c / avg) / (vol20 * np.sqrt(k / 2))

        sma20, sma50, sma200 = ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
        lower, mid, upper = ind.bollinger(c, 20)
        bbw = (upper - lower) / mid
        a14 = ind.atr(h, l, c, 14)
        has_volume = (v > 0).mean() > 0.8 if n else False
        if has_volume:
            lv = np.log(np.maximum(v, 1))
            volume_z = (lv - ind.sma(lv, 50)) / ind.rolling_std(lv, 50)
        else:
            volume_z = np.zeros(n)
        hi250, lo250 = ind.rolling_max(h, 250), ind.rolling_min(l, 250)
        idx = np.arange(n)
        # Days since the 52-week high: index of the last bar that set a running 250-day high.
        at_high = h >= hi250
        last_high = np.maximum.accumulate(np.where(at_high, idx, 0))
        hi20, lo20 = ind.rolling_max(h, 20), ind.rolling_min(l, 20)
        rng10 = ind.rolling_max(h, 10) - ind.rolling_min(l, 10)
        up = (r > 0).astype(float)  # the first bar (no return) counts as down
        _, _, hist = ind.macd(c)
        cols = {
            "ret_1d": r / vol20,
            "ret_5d": ret(5, vol20),
            "ret_20d": ret(20, vol20),
            "ret_60d": ret(60, vol60),
            "ret_120d": ret(120, vol250),
            "ret_250d": ret(250, vol250),
            "vs_sma20": dist(sma20, 20),
            "vs_sma50": dist(sma50, 50),
            "vs_sma200": dist(sma200, 200),
            "sma50_slope": np.log(sma50 / ind.shift(sma50, 10)) / (vol20 * np.sqrt(10)),
            "sma200_slope": np.log(sma200 / ind.shift(sma200, 20)) / (vol60 * np.sqrt(20)),
            "rsi14": (ind.rsi(c, 14) - 50) / 50,
            "rsi2": (ind.rsi(c, 2) - 50) / 50,
            "macd_hist": hist / (c * vol20),
            "bb_width_rank": ind.percentile_rank(bbw, 250) - 0.5,
            "vol_ratio": np.log(vol20 / vol250),
            "atr_rank": ind.percentile_rank(a14 / c, 250) - 0.5,
            "volume_z": volume_z,
            "vs_high250": np.log(c / hi250) / (vol20 * np.sqrt(20)),
            "vs_low250": np.log(c / lo250) / (vol20 * np.sqrt(20)),
            "days_since_high": np.minimum(idx - last_high, 250) / 250,
            "drawdown": np.log(c / np.maximum.accumulate(c)) / (vol250 * np.sqrt(250)),
            "up_days20": ind.sma(up, 20) - 0.5,
            "range_tightness": np.log(rng10 / (a14 * np.sqrt(10))),
            "adx": ind.adx(h, l, c, 14) / 50 - 0.5,
            "stoch": ind.stochastic(h, l, c, 14) - 0.5,
            "close_location": np.where(h > l, (c - l) / (h - l), 0.5) - 0.5,
            "gap": np.log(o / ind.shift(c, 1)) / vol20,
            "donchian_pos": np.where(hi20 > lo20, (c - lo20) / (hi20 - lo20), 0.5) - 0.5,
        }
    X = np.column_stack([_safe(cols[f]) for f in FEATURES])
    X = np.clip(X, -6, 6)
    valid = np.isfinite(X).all(axis=1)
    valid[:WARMUP] = False
    return FeatureSet(X, valid, vol20, c)


def targets(bars: Bars) -> dict[str, np.ndarray]:
    """What happened next, for every day (NaN when the future isn't known yet):

    up_5d/up_20d/up_60d: 1 if the price was higher that many trading days later;
    breakout_up/breakout_down: 1 if within the next 10 days a close cleared the prior 20-day high (or low).
    """
    c, h, l = bars.close, bars.high, bars.low
    n = len(c)
    out = {}
    for k in HORIZONS:
        future = ind.shift(c, -k)
        out[f"up_{k}d"] = np.where(np.isfinite(future), (future > c).astype(float), np.nan)
        out[f"ret_{k}d"] = np.log(future / c)
    hi = ind.rolling_max(h, BREAKOUT_LOOKBACK)
    lo = ind.rolling_min(l, BREAKOUT_LOOKBACK)
    best = np.full(n, np.nan)
    worst = np.full(n, np.nan)
    if n > BREAKOUT_DAYS:
        w = np.lib.stride_tricks.sliding_window_view(c[1:], BREAKOUT_DAYS)
        best[: len(w)] = w.max(axis=1)
        worst[: len(w)] = w.min(axis=1)
    known = np.isfinite(best) & np.isfinite(hi)
    out["breakout_up"] = np.where(known, (best > hi).astype(float), np.nan)
    out["breakout_down"] = np.where(known, (worst < lo).astype(float), np.nan)
    return out


TARGETS = ("up_5d", "up_20d", "up_60d", "breakout_up", "breakout_down")
