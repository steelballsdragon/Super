"""History in numbers: returns over every period, risk, drawdowns, seasonality, decades, the US presidential
cycle, Bitcoin's halving cycle and Shiller's CAPE (S&P 500 valuation since 1881)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import numpy as np

from . import indicators as ind
from .sources import LongRun
from .yahoo import Bars

DAY = 86400
YEAR = 365.2425 * DAY
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
HALVINGS = [date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20)]
NEXT_HALVING_EST = date(2028, 4, 15)


def to_date(t: float) -> date:
    return datetime.fromtimestamp(float(t), timezone.utc).date()


def days(bars: Bars) -> np.ndarray:
    return bars.t.astype("datetime64[s]").astype("datetime64[D]")


@dataclass
class Drawdown:
    depth: float
    peak: date
    trough: date
    recovered: date | None

    @property
    def months_down(self) -> float:
        return (self.trough - self.peak).days / 30.44

    @property
    def months_to_recover(self) -> float | None:
        return (self.recovered - self.trough).days / 30.44 if self.recovered else None


@dataclass
class Performance:
    first: date
    years: float
    returns: dict[str, float | None]  # "1D", "1W", "1M", "3M", "YTD", "1Y", "3Y", "5Y", "10Y", "Max"
    cagr: float
    vol: float  # annualised
    sharpe: float  # excess over nothing (no risk-free rate)
    sortino: float
    max_dd: Drawdown
    current_dd: float
    best_day: tuple[float, date]
    worst_day: tuple[float, date]
    up_days: float


def performance(bars: Bars, periods_per_year: int = 252) -> Performance:
    c = bars.close
    t = bars.t
    now = t[-1]

    def back(seconds):
        i = np.searchsorted(t, now - seconds, side="right") - 1
        return float(c[-1] / c[i] - 1) if i >= 0 and t[0] <= now - seconds + 5 * DAY else None

    year_start = datetime(to_date(now).year, 1, 1, tzinfo=timezone.utc).timestamp()
    i_ytd = np.searchsorted(t, year_start) - 1
    rets = {
        "1D": float(c[-1] / c[-2] - 1) if len(c) > 1 else None,
        "1W": back(7 * DAY), "1M": back(30.44 * DAY), "3M": back(91.3 * DAY),
        "YTD": float(c[-1] / c[i_ytd] - 1) if i_ytd >= 0 else None,
        "1Y": back(YEAR), "3Y": back(3 * YEAR), "5Y": back(5 * YEAR), "10Y": back(10 * YEAR),
        "Max": float(c[-1] / c[0] - 1),
    }
    r = np.diff(np.log(c))
    years = (t[-1] - t[0]) / YEAR
    cagr = float((c[-1] / c[0]) ** (1 / years) - 1) if years > 0 else 0.0
    recent = r[-periods_per_year * 10:]
    vol = float(recent.std() * np.sqrt(periods_per_year))
    mean = float(recent.mean() * periods_per_year)
    down = recent[recent < 0]
    sortino = mean / float(np.sqrt((down ** 2).mean()) * np.sqrt(periods_per_year)) if len(down) else 0.0
    dd = drawdowns(bars, 0.0, top=1)
    best, worst = int(np.argmax(r)), int(np.argmin(r))
    return Performance(to_date(t[0]), years, rets, cagr, vol, mean / vol if vol else 0.0, sortino,
                       dd[0] if dd else Drawdown(0.0, to_date(t[0]), to_date(t[0]), None),
                       float(c[-1] / c.max() - 1), (float(np.expm1(r[best])), to_date(t[best + 1])),
                       (float(np.expm1(r[worst])), to_date(t[worst + 1])), float((r > 0).mean()))


def drawdowns(bars: Bars, min_depth: float = 0.2, top: int = 10) -> list[Drawdown]:
    """The deepest peak-to-trough falls (each counted once, until the old high was regained)."""
    c, t = bars.close, bars.t
    peak_val, peak_i = c[0], 0
    trough_val, trough_i = c[0], 0
    found = []
    for i in range(1, len(c)):
        if c[i] >= peak_val:
            if trough_val / peak_val - 1 <= -min_depth and trough_i > peak_i:
                found.append(Drawdown(float(trough_val / peak_val - 1), to_date(t[peak_i]), to_date(t[trough_i]),
                                      to_date(t[i])))
            peak_val, peak_i = c[i], i
            trough_val, trough_i = c[i], i
        elif c[i] < trough_val:
            trough_val, trough_i = c[i], i
    if trough_val / peak_val - 1 <= -min_depth and trough_i > peak_i:
        found.append(Drawdown(float(trough_val / peak_val - 1), to_date(t[peak_i]), to_date(t[trough_i]), None))
    found.sort(key=lambda d: d.depth)
    return found[:top]


def month_ends(bars: Bars) -> tuple[np.ndarray, np.ndarray]:
    """(month as numpy datetime64[M], last close of that month), dropping the current unfinished month."""
    m = days(bars).astype("datetime64[M]")
    last = np.r_[m[1:] != m[:-1], False]
    return m[last], bars.close[last]


def year_ends(bars: Bars) -> tuple[np.ndarray, np.ndarray]:
    y = days(bars).astype("datetime64[Y]")
    last = np.r_[y[1:] != y[:-1], True]
    return y[last].astype(int) + 1970, bars.close[last]


def calendar_years(bars: Bars) -> list[tuple[int, float]]:
    """Each calendar year's return (the current year so far, last)."""
    years, closes = year_ends(bars)
    out = [(int(years[i]), float(closes[i] / closes[i - 1] - 1)) for i in range(1, len(years))]
    return out


@dataclass
class Season:
    month: int  # 1-12
    avg: float
    median: float
    up: float
    n: int


def seasonality(bars: Bars) -> list[Season]:
    months, closes = month_ends(bars)
    rets = closes[1:] / closes[:-1] - 1
    mnum = months[1:].astype(int) % 12 + 1
    out = []
    for m in range(1, 13):
        r = rets[mnum == m]
        if len(r):
            out.append(Season(m, float(r.mean()), float(np.median(r)), float((r > 0).mean()), len(r)))
    return out


def weekday_stats(bars: Bars) -> list[tuple[str, float, float]]:
    d = days(bars)
    wd = (d.astype(int) + 3) % 7  # 1970-01-01 was a Thursday; 0 = Monday
    r = np.diff(np.log(bars.close))
    wd = wd[1:]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return [(names[k], float(np.expm1(r[wd == k]).mean()), float((r[wd == k] > 0).mean()))
            for k in range(7) if (wd == k).sum() > 50]


def decades(bars: Bars) -> list[tuple[str, float, float]]:
    """(decade, total return, annualised) from the first full decade on; the current one so far."""
    years, closes = year_ends(bars)
    out = []
    for start in range(int(years[0]) // 10 * 10 + 10, int(years[-1]) + 1, 10):
        i0 = np.searchsorted(years, start - 1)
        i1 = min(np.searchsorted(years, start + 9), len(years) - 1)
        if i0 >= len(years) or years[i0] != start - 1:
            continue
        total = float(closes[i1] / closes[i0] - 1)
        span = max(int(years[i1]) - start + 1, 1)
        out.append((f"{start}s", total, (1 + total) ** (1 / span) - 1))
    return out


def beta_corr(bars: Bars, bench: Bars, window: int = 252) -> tuple[float, float] | None:
    common, ia, ib = np.intersect1d(days(bars), days(bench), return_indices=True)
    if len(common) < 60:
        return None
    a = np.diff(np.log(bars.close[ia]))[-window:]
    b = np.diff(np.log(bench.close[ib]))[-window:]
    var = b.var()
    if not var:
        return None
    return float(np.cov(a, b)[0, 1] / var), float(np.corrcoef(a, b)[0, 1])


# ----- cycles -----

def presidential_year(year: int) -> int:
    """1 = the year after a US election (2025, 2029...), 4 = election year (2024, 2028...)."""
    return (year - 2024 - 1) % 4 + 1


PRESIDENTIAL_NAMES = {1: "Post-election year", 2: "Midterm year", 3: "Pre-election year", 4: "Election year"}


def presidential_cycle(years: list[tuple[int, float]]) -> dict[int, dict]:
    out = {}
    for k in range(1, 5):
        r = np.array([ret for y, ret in years if presidential_year(y) == k])
        if len(r):
            out[k] = {"avg": float(r.mean()), "median": float(np.median(r)), "up": float((r > 0).mean()), "n": len(r)}
    return out


def halving_cycle(btc: Bars, today: date | None = None) -> dict:
    """Where Bitcoin is in its 4-year halving cycle, and how it did from the same point in earlier cycles."""
    today = today or to_date(btc.t[-1])
    last = max(h for h in HALVINGS if h <= today)
    since = (today - last).days
    d = days(btc)
    out = {"last": last, "days_since": since, "next_est": NEXT_HALVING_EST,
           "days_to_next": (NEXT_HALVING_EST - today).days, "earlier": []}
    for h in HALVINGS:
        if h >= last:
            continue
        at = np.datetime64(h) + np.timedelta64(since, "D")
        i = np.searchsorted(d, at)
        if i >= len(d) or i == 0:
            continue
        row = {"halving": h, "price_then": float(btc.close[i])}
        for label, ahead in (("3M", 91), ("6M", 182), ("1Y", 365)):
            j = np.searchsorted(d, at + np.timedelta64(ahead, "D"))
            row[label] = float(btc.close[j] / btc.close[i] - 1) if j < len(d) else None
        k = np.searchsorted(d, np.datetime64(h))
        row["since_halving"] = float(btc.close[i] / btc.close[k] - 1) if k < len(d) else None
        out["earlier"].append(row)
    k = np.searchsorted(d, np.datetime64(last))
    out["since_halving_now"] = float(btc.close[-1] / btc.close[k] - 1) if k < len(d) else None
    return out


# ----- the long run (1871 onward) -----

@dataclass
class CapeView:
    cape_now: float  # estimated
    as_of_data: str  # month of the last reported earnings
    percentile: float  # share of months since 1881 with a lower CAPE
    median: float
    expected_10y_real: float  # annualised, from the historical CAPE regression
    band: tuple[float, float]  # middle 80% of what followed similar valuations
    similar_years: list[int]


def cape_view(lr: LongRun, price_now: float, years_now: float) -> CapeView | None:
    """Shiller's CAPE: price over the last 10 years' average inflation-adjusted earnings. Shiller's earnings
    stop a few quarters back, so today's value is estimated by carrying the 10-year earnings average forward
    at its recent growth rate. Historically, high CAPE has meant lower returns over the next decade."""
    cape = lr.cape
    known = np.where(np.isfinite(cape) & np.isfinite(lr.real_price))[0]
    if len(known) < 600:
        return None
    last = known[-1]
    e10 = lr.real_price[known] / cape[known]
    growth = (e10[-1] / e10[-61]) ** (1 / 5) - 1 if len(e10) > 61 else 0.02
    gap_years = max(years_now - lr.t[last], 0)
    e10_now = e10[-1] * (1 + growth) ** gap_years
    # Real price now: today's price in the dollars of the last CPI reading (inflation since then ≈ 3%/yr).
    real_now = price_now / (1.03 ** gap_years) * (lr.real_price[last] / lr.price[last])
    cape_now = float(real_now / e10_now)
    # Forward 10-year real annualised return for every month with a CAPE and a known future.
    rp = lr.real_price
    fwd = np.full(len(rp), np.nan)
    ahead = 120
    div_yield = np.where(np.isfinite(lr.dividend), lr.dividend / lr.price, 0.0)
    for i in known:
        j = i + ahead
        if j < len(rp) and np.isfinite(rp[j]):
            fwd[i] = (rp[j] / rp[i]) ** (1 / 10) - 1 + float(np.nanmean(div_yield[i:j]))
    ok = np.isfinite(fwd) & np.isfinite(cape)
    x, y = np.log(cape[ok]), fwd[ok]
    slope, intercept = np.polyfit(x, y, 1)
    expected = float(intercept + slope * np.log(cape_now))
    similar = ok & (np.abs(np.log(cape) - np.log(cape_now)) < 0.12)
    if similar.sum() >= 12:
        band = (float(np.percentile(fwd[similar], 10)), float(np.percentile(fwd[similar], 90)))
        sim_years = sorted({int(lr.year[i]) for i in np.where(similar)[0]})
    else:
        resid = y - (intercept + slope * x)
        band = (expected + float(np.percentile(resid, 10)), expected + float(np.percentile(resid, 90)))
        sim_years = []
    return CapeView(cape_now, f"{MONTHS[lr.month[last] - 1]} {lr.year[last]}",
                    float((cape[known] < cape_now).mean()), float(np.median(cape[known])), expected, band,
                    sim_years)


def long_run_monthly(lr: LongRun, sp500: Bars | None) -> tuple[np.ndarray, np.ndarray]:
    """S&P 500 monthly level since 1871: Shiller's monthly averages, then Yahoo month-end closes from 1928."""
    t = lr.t
    p = lr.price
    keep = np.isfinite(p)
    t, p = t[keep], p[keep]
    if sp500 is not None and len(sp500):
        m, closes = month_ends(sp500)
        ty = 1970 + m.astype(int) / 12
        before = t < ty[0]
        t = np.concatenate([t[before], ty])
        p = np.concatenate([p[before], closes])
    return t, p


def long_run_years(t: np.ndarray, p: np.ndarray) -> list[tuple[int, float]]:
    """Calendar-year returns from a monthly series (December to December)."""
    years = np.floor(t + 1e-6).astype(int)
    out = []
    for y in range(int(years[0]) + 1, int(years[-1]) + 1):
        a = np.where(years == y - 1)[0]
        b = np.where(years == y)[0]
        if len(a) and len(b):
            out.append((y, float(p[b[-1]] / p[a[-1]] - 1)))
    return out


def trend_label(bars: Bars) -> tuple[str, int]:
    """Plain-words trend and a -2..+2 score from the 50/200-day averages and their slopes."""
    c = bars.close
    s50, s200 = ind.sma(c, 50), ind.sma(c, 200)
    if not np.isfinite(s200[-1]):
        if not np.isfinite(s50[-1]):
            return "Not enough history", 0
        return ("Uptrend (short history)", 1) if c[-1] > s50[-1] else ("Downtrend (short history)", -1)
    above200 = c[-1] > s200[-1]
    above50 = c[-1] > s50[-1]
    rising200 = s200[-1] > s200[-21]
    if above200 and above50 and s50[-1] > s200[-1] and rising200:
        return "Strong uptrend", 2
    if above200 and (above50 or rising200):
        return "Uptrend", 1
    if not above200 and not above50 and s50[-1] < s200[-1] and not rising200:
        return "Strong downtrend", -2
    if not above200 and (not above50 or not rising200):
        return "Downtrend", -1
    return "Sideways", 0


def volatility_regime(bars: Bars) -> tuple[str, float]:
    """Today's 20-day volatility against the symbol's own history (percentile)."""
    v = ind.realized_vol(bars.close, 20)
    known = v[np.isfinite(v)]
    if len(known) < 300:
        return "unknown", 0.5
    pct = float((known < known[-1]).mean())
    label = ("very calm" if pct < 0.15 else "calm" if pct < 0.4 else "normal" if pct < 0.7
             else "elevated" if pct < 0.9 else "extreme")
    return label, pct
