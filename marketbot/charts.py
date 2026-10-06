"""PNG charts for Discord, drawn with matplotlib's object API (safe in worker threads)."""

from __future__ import annotations

import functools
import io
import threading
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator, MaxNLocator, NullFormatter  # noqa: E402

from . import indicators as ind  # noqa: E402
from .forecast import Cone, Level  # noqa: E402
from .yahoo import Bars  # noqa: E402

# Dark chart chrome and a colour-blind-checked series order (blue, orange, aqua).
SURFACE = "#1a1a19"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"
SERIES = ["#3987e5", "#d95926", "#199e70"]
UP = "#0ca30c"
DOWN = "#d03b3b"
CONE = "#3987e5"
DPI = 110
_LOCK = threading.Lock()  # matplotlib isn't guaranteed thread-safe: draw one chart at a time


def serialized(draw):
    @functools.wraps(draw)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return draw(*args, **kwargs)
    return wrapper


def _fig(height_ratios, size=(10, 6.2)):
    fig = Figure(figsize=size, dpi=DPI, facecolor=SURFACE)
    axes = fig.subplots(len(height_ratios), 1, sharex=True, gridspec_kw={"height_ratios": height_ratios,
                                                                         "hspace": 0.06})
    axes = np.atleast_1d(axes)
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.tick_params(colors=MUTED, labelsize=8, length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.yaxis.tick_right()
    return fig, axes


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE, bbox_inches="tight", pad_inches=0.25)
    return buf.getvalue()


def _price_fmt(v, _=None) -> str:
    a = abs(v)
    if a >= 10000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:,.1f}"
    if a >= 1:
        return f"{v:,.2f}"
    return f"{v:.4g}"


def _big(v, _=None) -> str:
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.0f}{unit}"
    return f"{v:.0f}"


def _log_axis(ax, fmt) -> None:
    """Log scale with plain labels at 1-2-5 steps (no scientific notation)."""
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.yaxis.set_major_formatter(FuncFormatter(fmt))
    ax.yaxis.set_minor_formatter(NullFormatter())


def _date_ticks(ax, t: np.ndarray, extra: int = 0, span_days: float | None = None):
    """Label integer x positions with dates (future positions get estimated dates)."""
    step = float(np.median(np.diff(t[-30:]))) if len(t) > 2 else 86400.0
    span = span_days or (t[-1] - t[0]) / 86400

    def label(x, _=None):
        i = int(round(x))
        if i < 0:
            return ""
        ts = t[i] if i < len(t) else t[-1] + (i - len(t) + 1) * step * (7 / 5 if step < 2 * 86400 and span > 3 else 1)
        d = datetime.fromtimestamp(float(ts), timezone.utc)
        if span <= 3:
            return d.strftime("%H:%M")
        if span <= 200:
            return d.strftime("%b %d")
        if span <= 800:
            return d.strftime("%b '%y")
        return d.strftime("%Y")

    ax.xaxis.set_major_locator(MaxNLocator(8, integer=True))
    ax.xaxis.set_major_formatter(FuncFormatter(label))


def _title(fig, title: str, subtitle: str):
    fig.text(0.01, 0.995, title, color=INK, fontsize=13, fontweight="bold", va="top", ha="left")
    fig.text(0.01, 0.955, subtitle, color=INK_2, fontsize=8.5, va="top", ha="left")


@serialized
def price_chart(bars: Bars, title: str, cone: Cone | None = None, resistance: list[Level] | None = None,
                support: list[Level] | None = None, trigger: float | None = None, days: int = 126,
                cone_days: int = 42) -> bytes:
    """Candles with 20/50/200-day averages, Bollinger Bands, levels, a breakout line and the forecast cone,
    plus volume and RSI panels."""
    n = len(bars)
    start = max(n - days, 0)
    if n - start > 300:
        return long_price_chart(bars, title, start)
    c, o, h, l, v = bars.close, bars.open, bars.high, bars.low, bars.volume
    x = np.arange(n - start)
    has_volume = (v[start:] > 0).mean() > 0.8
    fig, axes = _fig([6, 1.2, 1.3] if has_volume else [6, 1.3])
    ax, ax_rsi = axes[0], axes[-1]
    ax_vol = axes[1] if has_volume else None

    lo, mid, hi = ind.bollinger(c, 20)
    ax.fill_between(x, lo[start:], hi[start:], color=INK_2, alpha=0.07, linewidth=0, label="Bollinger Bands")
    if days <= 200:
        up = c[start:] >= o[start:]
        colors = np.where(up, UP, DOWN)
        ax.vlines(x, l[start:], h[start:], colors=colors, linewidth=0.8)
        body_lo = np.minimum(o[start:], c[start:])
        body_h = np.maximum(np.abs(c[start:] - o[start:]), (h[start:].max() - l[start:].min()) * 0.002)
        ax.bar(x, body_h, bottom=body_lo, width=0.62, color=colors, linewidth=0)
    else:
        ax.plot(x, c[start:], color=INK, linewidth=1.4)
    for (period, color) in zip((20, 50, 200), SERIES):
        s = ind.sma(c, period)[start:]
        if np.isfinite(s).any():
            ax.plot(x, s, color=color, linewidth=1.6, label=f"{period}-day avg")
            last = s[np.isfinite(s)][-1]
            ax.annotate(f"{period}d", (x[-1], last), xytext=(4, 0), textcoords="offset points", color=INK_2,
                        fontsize=7, va="center")
    end_x = x[-1]
    if cone is not None and cone.bands is not None:
        k = min(cone_days, cone.bands.shape[1])
        fx = np.arange(end_x, end_x + k + 1)
        band = np.hstack([np.full((cone.bands.shape[0], 1), c[-1]), cone.bands[:, :k]])
        ax.fill_between(fx, band[0], band[4], color=CONE, alpha=0.13, linewidth=0, label="Forecast 5–95%")
        ax.fill_between(fx, band[1], band[3], color=CONE, alpha=0.28, linewidth=0, label="Forecast 25–75%")
        ax.plot(fx, band[2], color=CONE, linewidth=1.4, linestyle=(0, (4, 3)))
        for row, name in ((4, "95%"), (2, "median"), (0, "5%")):
            ax.annotate(f"{name} {_price_fmt(band[row][-1])}", (fx[-1], band[row][-1]), xytext=(4, 0),
                        textcoords="offset points", color=INK_2, fontsize=7, va="center")
        end_x = fx[-1]
    span_lo = float(np.nanmin(l[start:]))
    span_hi = float(np.nanmax(h[start:]))
    drawn = [trigger] if trigger is not None else []
    for lvl in (resistance or [])[:2] + (support or [])[:2]:
        if any(abs(lvl.price / d - 1) < 0.012 for d in drawn):
            continue  # too close to a line already drawn to label legibly
        drawn.append(lvl.price)
        if span_lo * 0.9 <= lvl.price <= span_hi * 1.1:
            ax.axhline(lvl.price, color=MUTED, linewidth=0.9, linestyle=(0, (2, 3)))
            tag = "R" if lvl.kind == "resistance" else "S"
            ax.annotate(f"{tag} {_price_fmt(lvl.price)}", (0, lvl.price), xytext=(2, 2), textcoords="offset points",
                        color=INK_2, fontsize=7, va="bottom")
    if trigger is not None and span_lo * 0.8 <= trigger <= span_hi * 1.2:
        ax.axhline(trigger, color=INK, linewidth=1.0, linestyle=(0, (6, 3)))
        ax.annotate(f"breakout line {_price_fmt(trigger)}", (x[len(x) // 3], trigger), xytext=(0, 3),
                    textcoords="offset points", color=INK, fontsize=7.5, va="bottom")
    ax.annotate(_price_fmt(c[-1]), (x[-1], c[-1]), xytext=(-2, 10), textcoords="offset points", color=INK,
                fontsize=8.5, fontweight="bold", ha="right",
                bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": AXIS, "lw": 0.8})
    ax.yaxis.set_major_formatter(FuncFormatter(_price_fmt))
    ax.set_xlim(-1, end_x + (8 if cone is not None else 3))
    leg = ax.legend(loc="upper left", fontsize=7.5, frameon=False, labelcolor=INK_2, ncol=3)
    for text in leg.get_texts():
        text.set_color(INK_2)

    if ax_vol is not None:
        up = c[start:] >= o[start:]
        ax_vol.bar(x, v[start:], width=0.7, color=np.where(up, UP, DOWN), alpha=0.45, linewidth=0)
        avg = ind.sma(v, 50)[start:]
        ax_vol.plot(x, avg, color=INK_2, linewidth=1.0)
        ax_vol.yaxis.set_major_formatter(FuncFormatter(_big))
        ax_vol.yaxis.set_major_locator(MaxNLocator(2))
        ax_vol.set_ylabel("Volume", color=MUTED, fontsize=7.5)
    r = ind.rsi(c, 14)[start:]
    ax_rsi.plot(x, r, color=INK_2, linewidth=1.3)
    for level in (30, 70):
        ax_rsi.axhline(level, color=MUTED, linewidth=0.8, linestyle=(0, (2, 3)))
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_yticks([30, 70])
    ax_rsi.set_ylabel("RSI 14", color=MUTED, fontsize=7.5)
    _date_ticks(ax_rsi, bars.t[start:], span_days=(bars.t[-1] - bars.t[start]) / 86400)
    sub = f"Daily · last {len(x)} sessions · candles, 20/50/200-day averages, Bollinger Bands"
    if cone is not None:
        sub += f" · shaded: Monte Carlo range for the next {min(cone_days, cone.bands.shape[1])} sessions"
    _title(fig, title, sub)
    return _png(fig)


def long_price_chart(bars: Bars, title: str, start: int) -> bytes:
    """Years of history: a log-scale line with the 200-day average (candles and RSI would be a blur)."""
    fig, axes = _fig([1], size=(10, 5))
    ax = axes[0]
    c = bars.close[start:]
    x = np.arange(len(c))
    ax.plot(x, c, color=INK, linewidth=1.1)
    s200 = ind.sma(bars.close, 200)[start:]
    ax.plot(x, s200, color=SERIES[0], linewidth=1.4, label="200-day avg")
    if c.max() / max(c.min(), 1e-12) > 4:
        _log_axis(ax, _price_fmt)
    else:
        ax.yaxis.set_major_formatter(FuncFormatter(_price_fmt))
    ax.annotate(_price_fmt(c[-1]), (x[-1], c[-1]), xytext=(-2, 10), textcoords="offset points", color=INK,
                fontsize=8.5, fontweight="bold", ha="right",
                bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": AXIS, "lw": 0.8})
    leg = ax.legend(loc="upper left", fontsize=7.5, frameon=False)
    for text in leg.get_texts():
        text.set_color(INK_2)
    ax.set_xlim(-1, len(x) + 3)
    _date_ticks(ax, bars.t[start:], span_days=(bars.t[-1] - bars.t[start]) / 86400)
    first = datetime.fromtimestamp(float(bars.t[start]), timezone.utc)
    _title(fig, title, f"Daily closes since {first:%b %Y}" + (" · log scale" if c.max() / max(c.min(), 1e-12) > 4 else ""))
    return _png(fig)


@serialized
def intraday_chart(bars: Bars, title: str, prev_close: float | None) -> bytes:
    fig, axes = _fig([1], size=(10, 4.2))
    ax = axes[0]
    x = np.arange(len(bars))
    c = bars.close
    base = prev_close or float(c[0])
    color = UP if c[-1] >= base else DOWN
    ax.plot(x, c, color=color, linewidth=1.8)
    ax.fill_between(x, c, base, color=color, alpha=0.12, linewidth=0)
    ax.axhline(base, color=MUTED, linewidth=0.9, linestyle=(0, (2, 3)))
    ax.annotate(f"prev close {_price_fmt(base)}", (0, base), xytext=(2, 3), textcoords="offset points",
                color=INK_2, fontsize=7.5)
    ax.annotate(_price_fmt(c[-1]), (x[-1], c[-1]), xytext=(-2, 10), textcoords="offset points", color=INK,
                fontsize=8.5, fontweight="bold", ha="right",
                bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": AXIS, "lw": 0.8})
    ax.yaxis.set_major_formatter(FuncFormatter(_price_fmt))
    ax.set_xlim(-1, len(x) + 2)
    _date_ticks(ax, bars.t, span_days=(bars.t[-1] - bars.t[0]) / 86400)
    _title(fig, title, "Intraday · 5-minute bars (times in UTC)")
    return _png(fig)


@serialized
def long_run_chart(t: np.ndarray, p: np.ndarray, title: str, subtitle: str,
                   marks: list[tuple[float, str]] | None = None) -> bytes:
    """Log-scale history (fractional years on x), with the deepest falls shaded."""
    fig, axes = _fig([1], size=(10, 5))
    ax = axes[0]
    ax.plot(t, p, color=SERIES[0], linewidth=1.4)
    _log_axis(ax, lambda v, _: f"{v:,.0f}" if v >= 1 else f"{v:.2g}")
    peak = np.maximum.accumulate(p)
    dd = p / peak - 1
    ax.fill_between(t, p, peak, where=dd < -0.2, color=DOWN, alpha=0.18, linewidth=0, label="More than 20% below a high")
    for when, text in marks or []:
        i = int(np.clip(np.searchsorted(t, when), 0, len(t) - 1))
        ax.annotate(text, (t[i], p[i]), xytext=(0, -18), textcoords="offset points", color=INK_2, fontsize=7,
                    ha="center", arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.6})
    ax.xaxis.set_major_locator(MaxNLocator(10, integer=True))
    leg = ax.legend(loc="upper left", fontsize=7.5, frameon=False)
    for text in leg.get_texts():
        text.set_color(INK_2)
    _title(fig, title, subtitle)
    return _png(fig)


@serialized
def equity_chart(t: np.ndarray, strategy: np.ndarray, hold: np.ndarray, title: str, subtitle: str,
                 labels: tuple[str, str] = ("Model timing", "Buy and hold"),
                 short_labels: tuple[str, str] = ("Model", "Hold")) -> bytes:
    """Two growth-of-$1 curves on a log scale (also used to compare two symbols)."""
    fig, axes = _fig([1], size=(10, 4.6))
    ax = axes[0]
    x = np.arange(len(t))
    ax.plot(x, hold, color=SERIES[1], linewidth=1.6, label=labels[1])
    ax.plot(x, strategy, color=SERIES[0], linewidth=1.8, label=labels[0])
    for curve, name in ((strategy, short_labels[0]), (hold, short_labels[1])):
        ax.annotate(f"{name} ×{curve[-1]:.2f}", (x[-1], curve[-1]), xytext=(4, 0), textcoords="offset points",
                    color=INK_2, fontsize=7.5, va="center")
    _log_axis(ax, lambda v, _: f"×{v:.2g}")
    ax.set_xlim(-1, len(x) * 1.08)
    leg = ax.legend(loc="upper left", fontsize=8, frameon=False)
    for text in leg.get_texts():
        text.set_color(INK_2)
    _date_ticks(ax, t, span_days=(t[-1] - t[0]) / 86400)
    _title(fig, title, subtitle)
    return _png(fig)
