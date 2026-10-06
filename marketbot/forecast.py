"""Forward-looking tools that need no fitted model: historical look-alikes (analogs), Monte Carlo price cones,
and support/resistance levels."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import indicators as ind
from .features import FEATURES, FeatureSet
from .yahoo import Bars

ANALOG_FEATURES = ["ret_5d", "ret_20d", "ret_60d", "ret_250d", "vs_sma50", "vs_sma200", "rsi14", "bb_width_rank",
                   "vol_ratio", "vs_high250", "drawdown", "range_tightness"]
SHAPE_DAYS = 30
SHAPE_POINTS = 6
SHAPE_WEIGHT = 0.7
ANALOG_HORIZONS = (5, 20, 60)
MIN_GAP = 15  # look-alikes from the same stretch of history count once


@dataclass
class Analog:
    symbol: str
    t: int  # unix time of the matching day
    distance: float
    forward: dict[int, float]  # horizon -> return (fraction)


@dataclass
class AnalogSummary:
    matches: list[Analog]
    horizons: dict[int, dict]  # horizon -> {"n", "up", "median", "p25", "p75", "mean"}
    similarity: float  # 0-100: how close the best matches are

    def up_chance(self, horizon: int, base: float, prior: float = 25) -> float:
        """Share of look-alikes that were higher, shrunk toward the base rate (few matches = weak evidence)."""
        h = self.horizons.get(horizon)
        if not h or not h["n"]:
            return base
        return (h["up"] * h["n"] + base * prior) / (h["n"] + prior)


def _shape(close: np.ndarray, vol20: np.ndarray) -> np.ndarray:
    """The last 30 days' path (relative to today, in units of normal volatility), sampled at 6 points."""
    lc = np.log(close)
    steps = np.linspace(SHAPE_DAYS, SHAPE_DAYS // SHAPE_POINTS, SHAPE_POINTS).astype(int)
    with np.errstate(invalid="ignore", divide="ignore"):
        cols = [(ind.shift(lc, k) - lc) / (vol20 * np.sqrt(SHAPE_DAYS)) for k in steps]
    return np.column_stack(cols)


def analog_matrix(fs: FeatureSet) -> np.ndarray:
    idx = [FEATURES.index(f) for f in ANALOG_FEATURES]
    return np.hstack([fs.X[:, idx], SHAPE_WEIGHT * _shape(fs.close, fs.vol20)])


def find_analogs(target: FeatureSet, references: list[tuple[Bars, FeatureSet]], k: int = 30,
                 exclude_recent: int = 60) -> AnalogSummary:
    """Days in all the reference histories whose setup looked most like today's, and what happened next."""
    query = analog_matrix(target)[-1]
    if not np.isfinite(query).all():
        return AnalogSummary([], {}, 0.0)
    pools = []
    for bars, fs in references:
        M = analog_matrix(fs)
        n = len(bars)
        rows = np.where(fs.valid & np.isfinite(M).all(axis=1))[0]
        rows = rows[rows < n - max(ANALOG_HORIZONS) - 1]
        if fs is target:
            rows = rows[rows < n - exclude_recent]
        if len(rows):
            pools.append((bars, rows, M[rows]))
    if not pools:
        return AnalogSummary([], {}, 0.0)
    allM = np.vstack([p[2] for p in pools])
    scale = allM.std(axis=0)
    scale[scale < 1e-9] = 1.0
    cands = []
    for bars, rows, M in pools:
        d = np.sqrt((((M - query) / scale) ** 2).mean(axis=1))
        best = np.argsort(d)[: k * 20]
        cands.extend((float(d[i]), bars, int(rows[i])) for i in best)
    cands.sort(key=lambda c: c[0])
    chosen: list[tuple[float, Bars, int]] = []
    for d, bars, row in cands:
        if any(b is bars and abs(row - r) < MIN_GAP for _, b, r in chosen):
            continue
        chosen.append((d, bars, row))
        if len(chosen) >= k:
            break
    matches = []
    for d, bars, row in chosen:
        fwd = {h: float(bars.close[row + h] / bars.close[row] - 1) for h in ANALOG_HORIZONS if row + h < len(bars)}
        matches.append(Analog(bars.symbol, int(bars.t[row]), d, fwd))
    summary = {}
    for h in ANALOG_HORIZONS:
        r = np.array([m.forward[h] for m in matches if h in m.forward])
        if len(r):
            summary[h] = {"n": len(r), "up": float((r > 0).mean()), "median": float(np.median(r)),
                          "p25": float(np.percentile(r, 25)), "p75": float(np.percentile(r, 75)),
                          "mean": float(r.mean())}
    top = np.mean([m.distance for m in matches[:5]]) if matches else 9
    similarity = float(np.clip(100 * np.exp(-top), 0, 100))
    return AnalogSummary(matches, summary, similarity)


# ----- Monte Carlo -----

CONE_HORIZONS = (5, 21, 63, 126, 252)
PERCENTILES = (5, 25, 50, 75, 95)


@dataclass
class Cone:
    price: float
    daily_vol: float  # today's estimated daily volatility
    long_vol: float
    drift: float  # assumed daily log drift
    horizons: dict[int, dict]  # days -> {"p5".."p95", "up", "touch_up10", "touch_down10", "max_dd"}
    bands: np.ndarray = field(repr=False, default=None)  # (len(PERCENTILES), days) price percentiles per day

    def range_for(self, days: int) -> tuple[float, float]:
        h = self.horizons[days]
        return h["p5"], h["p95"]


def ewma_vol(r: np.ndarray, lam: float = 0.94) -> np.ndarray:
    r = np.nan_to_num(r)
    var = np.empty(len(r))
    v = float(np.var(r[1:31])) if len(r) > 31 else float(np.var(r) or 1e-4)
    for i, x in enumerate(r):
        var[i] = v
        v = lam * v + (1 - lam) * x * x
    return np.sqrt(var)  # var[i] is the forecast made before day i


def monte_carlo(bars: Bars, market: str, days: int = 252, paths: int = 2000, seed: int | None = 7,
                block: int = 5) -> Cone | None:
    """Filtered historical simulation: history's shocks (scaled to the volatility of their day) replayed in
    random 5-day blocks, with volatility that clusters and drifts back to its long-run level like a GARCH."""
    c = bars.close
    if len(c) < 300:
        return None
    r = np.diff(np.log(c))[-252 * 20:]
    sig = ewma_vol(r)
    z = np.clip(r / np.maximum(sig, 1e-8), -8, 8)
    z = (z - z.mean()) / z.std()
    long_var = float(np.var(r[-252 * 10:]))
    now_var = float(sig[-1] ** 2 * 0.94 + 0.06 * r[-1] ** 2)
    hist_mu = float(np.mean(r))
    cap = (0.30 if market == "crypto" else 0.10) / 252
    mu = float(np.clip(0.5 * hist_mu, -0.10 / 252, cap))
    rng = np.random.default_rng(seed)
    alpha, beta = 0.06, 0.92
    omega = long_var * (1 - alpha - beta)
    n_blocks = days // block + 1
    starts = rng.integers(0, len(z) - block, size=(paths, n_blocks))
    shocks = z[(starts[:, :, None] + np.arange(block)).reshape(paths, -1)[:, :days]]
    logp = np.zeros((paths, days))
    var = np.full(paths, now_var)
    level = np.zeros(paths)
    for d in range(days):
        eps = np.sqrt(var) * shocks[:, d]
        level = level + mu - 0.5 * var + eps
        logp[:, d] = level
        var = omega + alpha * eps ** 2 + beta * var
    prices = c[-1] * np.exp(logp)
    bands = np.percentile(prices, PERCENTILES, axis=0)
    horizons = {}
    for h in CONE_HORIZONS:
        if h > days:
            continue
        end = prices[:, h - 1]
        path = prices[:, :h]
        peak = np.maximum.accumulate(np.concatenate([np.full((paths, 1), c[-1]), path], axis=1), axis=1)
        dd = (np.concatenate([np.full((paths, 1), c[-1]), path], axis=1) / peak - 1).min(axis=1)
        horizons[h] = {**{f"p{p}": float(v) for p, v in zip(PERCENTILES, np.percentile(end, PERCENTILES))},
                       "up": float((end > c[-1]).mean()),
                       "touch_up10": float((path.max(axis=1) >= c[-1] * 1.1).mean()),
                       "touch_down10": float((path.min(axis=1) <= c[-1] * 0.9).mean()),
                       "max_dd": float(np.median(dd))}
    return Cone(float(c[-1]), float(np.sqrt(now_var)), float(np.sqrt(long_var)), mu, horizons, bands)


# ----- support and resistance -----

@dataclass
class Level:
    price: float
    kind: str  # "support" or "resistance"
    touches: int
    label: str  # e.g. "swing high ×3", "52-week high", "200-day average"


def levels(bars: Bars, lookback: int = 300, per_side: int = 3) -> tuple[list[Level], list[Level]]:
    """Nearest resistance levels above the price and supports below it, strongest clusters first."""
    b = bars.tail(lookback)
    if len(b) < 30:
        return [], []
    price = float(b.close[-1])
    a = ind.atr(b.high, b.low, b.close, 14)
    tol = 0.6 * float(np.nanmean(a[-20:])) if np.isfinite(a[-20:]).any() else price * 0.01
    hi_idx, lo_idx = ind.swing_points(b.high, b.low, 5)
    points = [(float(b.high[i]), i) for i in hi_idx] + [(float(b.low[i]), i) for i in lo_idx]
    points.sort()
    clusters: list[list[tuple[float, int]]] = []
    for p, i in points:
        if clusters and p - clusters[-1][-1][0] <= tol:
            clusters[-1].append((p, i))
        else:
            clusters.append([(p, i)])
    found = []
    for cl in clusters:
        prices = np.array([p for p, _ in cl])
        recency = np.array([i for _, i in cl]) / len(b)
        level = float(np.average(prices, weights=0.5 + recency))
        n = len(cl)
        found.append((level, n, f"swing {'level' if n > 1 else 'point'}" + (f" ×{n}" if n > 1 else "")))
    full = bars.tail(252)
    found.append((float(full.high.max()), 2, "52-week high"))
    found.append((float(full.low.min()), 2, "52-week low"))
    ath = float(bars.high.max())
    if ath <= price * 1.3:
        found.append((ath, 3, "all-time high"))
    for n in (50, 200):
        s = ind.sma(bars.close, n)
        if np.isfinite(s[-1]):
            found.append((float(s[-1]), 1, f"{n}-day average"))
    above = sorted((f for f in found if f[0] > price * 1.001), key=lambda f: f[0])
    below = sorted((f for f in found if f[0] < price * 0.999), key=lambda f: -f[0])

    def pick(side, kind):
        out: list[Level] = []
        for level, touches, label in side:
            near = next((o for o in out if abs(o.price - level) <= tol), None)
            if near:
                if touches > near.touches:
                    near.touches, near.label = touches, f"{label} + {near.label}"
                continue
            out.append(Level(level, kind, touches, label))
            if len(out) >= per_side:
                break
        return out

    return pick(above, "resistance"), pick(below, "support")
