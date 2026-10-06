"""Chart setups (squeezes, breakouts, flags, crosses, divergences...) found across a symbol's whole history, so
each one comes with how it actually played out on that chart before."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import indicators as ind
from .yahoo import Bars

EVENT_GAP = 5  # signals within this many days of the last one are the same event
STAT_HORIZON = 20


@dataclass(frozen=True)
class SetupDef:
    key: str
    name: str
    direction: int  # +1 bullish, -1 bearish
    emoji: str
    kind: str  # "breakout" (happening now), "pre-breakout" (coiling), "reversal", "trend", "momentum"
    alert: bool = True  # worth posting as an alert (MACD crosses and RSI exits are too frequent to be news)


DEFS = {d.key: d for d in [
    SetupDef("breakout_20d", "20-day breakout", 1, "🚀", "breakout"),
    SetupDef("breakdown_20d", "20-day breakdown", -1, "🪂", "breakout"),
    SetupDef("high_52w", "New 52-week high", 1, "🏔️", "breakout"),
    SetupDef("low_52w", "New 52-week low", -1, "🕳️", "breakout"),
    SetupDef("ath", "New all-time high", 1, "👑", "breakout"),
    SetupDef("squeeze_on", "Volatility squeeze (coiling)", 0, "🗜️", "pre-breakout"),
    SetupDef("squeeze_fire_up", "Squeeze fired upward", 1, "💥", "breakout"),
    SetupDef("squeeze_fire_down", "Squeeze fired downward", -1, "💥", "breakout"),
    SetupDef("coil_resistance", "Coiling just under resistance", 1, "⚡", "pre-breakout"),
    SetupDef("coil_support", "Coiling just above support", -1, "⚠️", "pre-breakout"),
    SetupDef("bull_flag", "Bull flag", 1, "🚩", "pre-breakout"),
    SetupDef("bear_flag", "Bear flag", -1, "🏴", "pre-breakout"),
    SetupDef("golden_cross", "Golden cross (50 over 200-day)", 1, "✨", "trend"),
    SetupDef("death_cross", "Death cross (50 under 200-day)", -1, "☠️", "trend"),
    SetupDef("reclaim_200", "Reclaimed the 200-day average", 1, "📈", "trend"),
    SetupDef("lost_200", "Lost the 200-day average", -1, "📉", "trend"),
    SetupDef("dip_in_uptrend", "Sharp dip in an uptrend", 1, "🛒", "reversal"),
    SetupDef("pop_in_downtrend", "Sharp bounce in a downtrend", -1, "🎈", "reversal"),
    SetupDef("rsi_oversold_exit", "RSI back above 30 (oversold exit)", 1, "🔄", "reversal", False),
    SetupDef("rsi_overbought_exit", "RSI back below 70 (overbought exit)", -1, "🔄", "reversal", False),
    SetupDef("bullish_divergence", "Bullish RSI divergence", 1, "🧲", "reversal"),
    SetupDef("bearish_divergence", "Bearish RSI divergence", -1, "🧲", "reversal"),
    SetupDef("volume_surge_up", "Volume surge on a big up day", 1, "🔊", "momentum"),
    SetupDef("volume_surge_down", "Volume surge on a big down day", -1, "🔊", "momentum"),
    SetupDef("gap_up", "Gap up above yesterday's high", 1, "⬆️", "momentum"),
    SetupDef("gap_down", "Gap down below yesterday's low", -1, "⬇️", "momentum"),
    SetupDef("macd_cross_up", "MACD bullish cross", 1, "〽️", "momentum", False),
    SetupDef("macd_cross_down", "MACD bearish cross", -1, "〽️", "momentum", False),
]}


@dataclass
class Context:
    """Indicator arrays shared by the detectors."""
    bars: Bars
    c: np.ndarray
    h: np.ndarray
    l: np.ndarray
    o: np.ndarray
    v: np.ndarray
    atr: np.ndarray
    vol20: np.ndarray
    sma20: np.ndarray
    sma50: np.ndarray
    sma200: np.ndarray
    rsi14: np.ndarray
    rsi2: np.ndarray
    hi20: np.ndarray  # highest high of the 20 bars before today
    lo20: np.ndarray
    hi250: np.ndarray
    lo250: np.ndarray
    has_volume: bool
    vol_avg50: np.ndarray


def context(bars: Bars) -> Context:
    c, h, l, o, v = bars.close, bars.high, bars.low, bars.open, bars.volume
    has_volume = bool(len(v)) and (v[-min(len(v), 250):] > 0).mean() > 0.8
    return Context(bars, c, h, l, o, v, ind.atr(h, l, c, 14), ind.realized_vol(c, 20), ind.sma(c, 20),
                   ind.sma(c, 50), ind.sma(c, 200), ind.rsi(c, 14), ind.rsi(c, 2),
                   ind.shift(ind.rolling_max(h, 20), 1), ind.shift(ind.rolling_min(l, 20), 1),
                   ind.shift(ind.rolling_max(h, 250), 1), ind.shift(ind.rolling_min(l, 250), 1),
                   has_volume, ind.shift(ind.sma(v, 50), 1))


def _cross_up(a, b):
    return (a > b) & (ind.shift(a, 1) <= ind.shift(b, 1))


def detect(ctx: Context) -> dict[str, np.ndarray]:
    """Boolean arrays (one per setup) marking the days each setup appeared."""
    c, h, l, o = ctx.c, ctx.h, ctx.l, ctx.o
    n = len(c)
    with np.errstate(invalid="ignore", divide="ignore"):
        # Breakouts need above-normal volume, where there's volume data (old index history has none).
        loud = (ctx.v > 1.5 * ctx.vol_avg50) | ~(ctx.vol_avg50 > 0) if ctx.has_volume else np.ones(n, bool)
        sig = {}
        sig["breakout_20d"] = (c > ctx.hi20) & (ind.shift(c, 1) <= ctx.hi20) & loud
        sig["breakdown_20d"] = (c < ctx.lo20) & (ind.shift(c, 1) >= ctx.lo20) & loud
        sig["high_52w"] = (c > ctx.hi250) & ~(ind.shift(c, 1) > ind.shift(ctx.hi250, 1))
        sig["low_52w"] = (c < ctx.lo250) & ~(ind.shift(c, 1) < ind.shift(ctx.lo250, 1))
        prior_ath = ind.shift(np.maximum.accumulate(h), 1)
        sig["ath"] = (c > prior_ath) & (ind.shift(c, 1) <= ind.shift(prior_ath, 1)) & (np.arange(n) > 250)

        bb_lo, _, bb_hi = ind.bollinger(c, 20, 2.0)
        kc_lo, _, kc_hi = ind.keltner(h, l, c, 20, 1.5)
        squeeze = (bb_hi < kc_hi) & (bb_lo > kc_lo)
        mom = c - (ind.rolling_max(h, 20) + ind.rolling_min(l, 20) + 2 * ctx.sma20) / 4
        fired = ind.shift(squeeze.astype(float), 1).astype(bool) & ~squeeze
        sig["squeeze_on"] = squeeze & ~ind.shift(squeeze.astype(float), 1).astype(bool)
        sig["squeeze_fire_up"] = fired & (mom > 0)
        sig["squeeze_fire_down"] = fired & (mom < 0)

        rng10 = ind.rolling_max(h, 10) - ind.rolling_min(l, 10)
        tight = rng10 < 2.5 * ctx.atr
        sig["coil_resistance"] = tight & (ctx.hi20 - c < 0.75 * ctx.atr) & (c <= ctx.hi20) & (c > ctx.sma50)
        sig["coil_support"] = tight & (c - ctx.lo20 < 0.75 * ctx.atr) & (c >= ctx.lo20) & (c < ctx.sma50)

        pole = np.log(ind.shift(c, 5) / ind.shift(c, 15))
        pole_need = 2.5 * ctx.vol20 * np.sqrt(10)
        flag_rng = ind.rolling_max(h, 5) - ind.rolling_min(l, 5)
        pole_height = np.abs(ind.shift(c, 5) - ind.shift(c, 15))
        drift5 = c - ind.shift(c, 5)
        sig["bull_flag"] = ((pole > pole_need) & (flag_rng < 0.5 * pole_height) & (drift5 <= 0)
                            & (c > ind.shift(c, 15) + 0.5 * pole_height))
        sig["bear_flag"] = ((-pole > pole_need) & (flag_rng < 0.5 * pole_height) & (drift5 >= 0)
                            & (c < ind.shift(c, 15) - 0.5 * pole_height))

        sig["golden_cross"] = _cross_up(ctx.sma50, ctx.sma200)
        sig["death_cross"] = _cross_up(ctx.sma200, ctx.sma50)
        sig["reclaim_200"] = _cross_up(c, ctx.sma200) & (ind.rolling_min(ind.shift(c - ctx.sma200, 1), 10) < 0)
        sig["lost_200"] = _cross_up(ctx.sma200, c) & (ind.rolling_max(ind.shift(c - ctx.sma200, 1), 10) > 0)
        sig["dip_in_uptrend"] = (c > ctx.sma200) & (ctx.rsi2 < 5) & (ind.shift(ctx.rsi2, 1) >= 5)
        sig["pop_in_downtrend"] = (c < ctx.sma200) & (ctx.rsi2 > 95) & (ind.shift(ctx.rsi2, 1) <= 95)
        sig["rsi_oversold_exit"] = _cross_up(ctx.rsi14, np.full(n, 30.0))
        sig["rsi_overbought_exit"] = _cross_up(np.full(n, 70.0), ctx.rsi14)

        r = ind.log_returns(c)
        big = 1.5 * ind.shift(ctx.vol20, 1)
        surge = (ctx.v > 2.5 * ctx.vol_avg50) if ctx.has_volume else np.zeros(n, bool)
        sig["volume_surge_up"] = surge & (r > big)
        sig["volume_surge_down"] = surge & (r < -big)
        gap = np.log(o / ind.shift(c, 1))
        sig["gap_up"] = (o > ind.shift(h, 1)) & (gap > big) & (c > o)
        sig["gap_down"] = (o < ind.shift(l, 1)) & (gap < -big) & (c < o)
        _, _, hist = ind.macd(c)
        sig["macd_cross_up"] = _cross_up(hist, np.zeros(n))
        sig["macd_cross_down"] = _cross_up(np.zeros(n), hist)

    bull_div, bear_div = divergences(ctx)
    sig["bullish_divergence"] = bull_div
    sig["bearish_divergence"] = bear_div
    for k in sig:
        sig[k] = np.nan_to_num(sig[k], nan=0).astype(bool)
        sig[k][: min(n, 60)] = False
    return sig


def divergences(ctx: Context) -> tuple[np.ndarray, np.ndarray]:
    """Price at a new 20-day low while RSI holds above its reading at the previous low (and the reverse)."""
    c, r = ctx.c, ctx.rsi14
    n = len(c)
    bull = np.zeros(n, bool)
    bear = np.zeros(n, bool)
    lows = np.where(c <= ind.rolling_min(c, 20))[0]
    highs = np.where(c >= ind.rolling_max(c, 20))[0]
    for marks, out, sign in ((lows, bull, 1), (highs, bear, -1)):
        beyond = (lambda a, b: c[a] < c[b]) if sign == 1 else (lambda a, b: c[a] > c[b])
        ref = extreme = None  # the previous run of lows' lowest day, and the current run's
        last = -10**9
        for i in marks:
            if i - last > 1:  # a new run of lows starts
                ref, extreme = extreme, i
            elif beyond(i, extreme):
                extreme = i
            last = i
            if ref is None or not 5 <= i - ref <= 40 or not (np.isfinite(r[i]) and np.isfinite(r[ref])):
                continue
            rsi_holds = r[i] > r[ref] + 3 if sign == 1 else r[i] < r[ref] - 3
            stretched = r[ref] < 35 if sign == 1 else r[ref] > 65
            if beyond(i, ref) and rsi_holds and stretched:
                out[i] = True
    return bull, bear


@dataclass
class SetupStats:
    events: int
    up_rate: float  # share higher STAT_HORIZON days later
    median: float
    mean: float
    base_up: float  # same for every day in the history
    base_mean: float

    @property
    def edge(self) -> float:
        return self.up_rate - self.base_up


def events_of(signal: np.ndarray) -> np.ndarray:
    idx = np.where(signal)[0]
    keep, last = [], -10**9
    for i in idx:
        if i - last > EVENT_GAP:
            keep.append(i)
        last = i
    return np.array(keep, dtype=int)


def stats(ctx: Context, signal: np.ndarray, horizon: int = STAT_HORIZON) -> SetupStats | None:
    c = ctx.c
    fwd = ind.shift(c, -horizon) / c - 1
    known = np.isfinite(fwd)
    ev = events_of(signal)
    ev = ev[known[ev]] if len(ev) else ev
    if len(ev) < 5:
        return None
    base = fwd[known & (np.arange(len(c)) > 60)]
    return SetupStats(len(ev), float((fwd[ev] > 0).mean()), float(np.median(fwd[ev])), float(fwd[ev].mean()),
                      float((base > 0).mean()), float(base.mean()))


@dataclass
class Plan:
    trigger: float | None  # price that confirms it
    stop: float | None  # price that says it failed
    target: float | None  # measured-move target


@dataclass
class ActiveSetup:
    key: str
    name: str
    emoji: str
    direction: int
    kind: str
    days_ago: int
    detail: str
    plan: Plan
    stats: SetupStats | None
    bench_stats: SetupStats | None = None  # the same setup on the benchmark's longer history


def plan_for(key: str, ctx: Context, i: int) -> tuple[Plan, str]:
    c, a = float(ctx.c[i]), float(ctx.atr[i]) if np.isfinite(ctx.atr[i]) else float(ctx.c[i]) * 0.02
    hi20, lo20 = float(ctx.hi20[i]), float(ctx.lo20[i])
    rng = hi20 - lo20
    f = fmt_price
    if key in ("breakout_20d", "high_52w", "ath", "squeeze_fire_up", "coil_resistance"):
        hi250 = float(ctx.hi250[i])
        trig, detail = {
            "breakout_20d": (hi20, f"closed above the 20-day high {f(hi20)}"),
            "high_52w": (hi250, f"closed above the 52-week high {f(hi250)}"),
            "ath": (c, "closed at a record high"),
            "squeeze_fire_up": (c, "volatility is expanding after a squeeze, momentum up"),
            "coil_resistance": (hi20, f"tight range just under the 20-day high {f(hi20)} ({(hi20 / c - 1) * 100:.1f}% away)"),
        }[key]
        # Measured move from the breakout line, but never a target the price has already passed.
        return Plan(trig, max(trig - 1.5 * a, lo20), max(trig + max(rng, 2 * a), c + 1.5 * a)), detail
    if key in ("breakdown_20d", "low_52w", "squeeze_fire_down", "coil_support"):
        lo250 = float(ctx.lo250[i])
        trig, detail = {
            "breakdown_20d": (lo20, f"closed below the 20-day low {f(lo20)}"),
            "low_52w": (lo250, f"closed below the 52-week low {f(lo250)}"),
            "squeeze_fire_down": (c, "volatility is expanding after a squeeze, momentum down"),
            "coil_support": (lo20, f"tight range just above the 20-day low {f(lo20)} ({(1 - lo20 / c) * 100:.1f}% away)"),
        }[key]
        return Plan(trig, min(trig + 1.5 * a, hi20), min(trig - max(rng, 2 * a), c - 1.5 * a)), detail
    if key == "squeeze_on":
        return Plan(hi20, lo20, None), f"Bollinger Bands inside the Keltner Channel: a big move often follows. Break {f(hi20)} up or {f(lo20)} down"
    if key in ("bull_flag", "bear_flag"):
        flag_hi = float(ctx.h[max(i - 4, 0): i + 1].max())
        flag_lo = float(ctx.l[max(i - 4, 0): i + 1].min())
        pole = abs(float(ctx.c[i - 5] - ctx.c[i - 15])) if i >= 15 else rng
        if key == "bull_flag":
            return Plan(flag_hi, flag_lo, flag_hi + pole), f"strong run-up then a tight pause; breaks out above {f(flag_hi)}"
        return Plan(flag_lo, flag_hi, flag_lo - pole), f"sharp drop then a tight pause; breaks down below {f(flag_lo)}"
    if key in ("golden_cross", "death_cross", "reclaim_200", "lost_200"):
        s200 = float(ctx.sma200[i])
        up = DEFS[key].direction > 0
        return Plan(None, s200 * (0.97 if up else 1.03), None), f"200-day average at {f(s200)}"
    if key in ("dip_in_uptrend", "pop_in_downtrend"):
        return Plan(None, None, float(ctx.sma20[i])), f"RSI(2) at {ctx.rsi2[i]:.0f}; snap-back target is the 20-day average {f(float(ctx.sma20[i]))}"
    if key in ("rsi_oversold_exit", "rsi_overbought_exit", "bullish_divergence", "bearish_divergence"):
        return Plan(None, None, None), f"RSI(14) {ctx.rsi14[i]:.0f}"
    if key in ("volume_surge_up", "volume_surge_down"):
        ratio = ctx.v[i] / ctx.vol_avg50[i] if ctx.vol_avg50[i] else 0
        return Plan(None, float(ctx.l[i]) if key.endswith("up") else float(ctx.h[i]), None), f"volume {ratio:.1f}× normal"
    if key in ("gap_up", "gap_down"):
        prev = float(ctx.c[i - 1])
        return Plan(None, prev, None), f"gapped {(ctx.o[i] / prev - 1) * 100:+.1f}% and held"
    return Plan(None, None, None), ""


def fmt_price(p: float | None) -> str:
    if p is None or not np.isfinite(p):
        return "—"
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0")


def active(bars: Bars, lookback: int = 3, bench: Bars | None = None) -> list[ActiveSetup]:
    """Setups that appeared in the last `lookback` bars, each with its track record on this chart."""
    ctx = context(bars)
    if len(ctx.c) < 80:
        return []
    sig = detect(ctx)
    bctx = bsig = None
    if bench is not None and bench.symbol != bars.symbol and len(bench) > 500:
        bctx = context(bench)
        bsig = detect(bctx)
    n = len(ctx.c)
    out = []
    for key, s in sig.items():
        recent = np.where(s[n - lookback:])[0]
        if not len(recent):
            continue
        i = n - lookback + int(recent[-1])
        d = DEFS[key]
        plan, detail = plan_for(key, ctx, i)
        out.append(ActiveSetup(key, d.name, d.emoji, d.direction, d.kind, n - 1 - i, detail, plan,
                               stats(ctx, s), stats(bctx, bsig[key]) if bsig is not None else None))
    kind_order = {"breakout": 0, "pre-breakout": 1, "momentum": 2, "trend": 3, "reversal": 4}
    out.sort(key=lambda a: (a.days_ago, kind_order[a.kind]))
    return out
