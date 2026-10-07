"""Fear & Greed, 0 (extreme fear) to 100 (extreme greed): CNN's index for the US stock market and alternative.me's
for crypto.

CNN publishes its index only on its website (cnn.com/markets/fear-and-greed); the bot reads the data that page
draws from, over the same Chrome-like connection it uses for Yahoo (CNN turns other clients away). When CNN
doesn't answer, the bot's own estimate (momentum, price strength, VIX and junk-bond demand against the last two
years) stands in, labelled as an estimate.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from .http import FAILING, Http, HttpError
from .sources import fear_greed_label

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_PAGE = "https://www.cnn.com/markets/fear-and-greed"
CNN_HEADERS = {"Referer": CNN_PAGE, "Origin": "https://www.cnn.com", "Accept": "application/json"}
# CNN's seven gauges: (its key, name, what it measures)
CNN_PARTS = (
    ("market_momentum_sp500", "Market momentum", "S&P 500 vs its 125-day average"),
    ("stock_price_strength", "Stock price strength", "NYSE 52-week highs vs lows"),
    ("stock_price_breadth", "Stock price breadth", "volume in rising vs falling stocks"),
    ("put_call_options", "Put and call options", "5-day put/call ratio"),
    ("market_volatility_vix", "Market volatility", "VIX vs its 50-day average"),
    ("safe_haven_demand", "Safe haven demand", "stocks vs Treasuries over 20 days"),
    ("junk_bond_demand", "Junk bond demand", "junk vs investment-grade bond yields"),
)
ZONES = ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")
ZONE_EMOJI = {"Extreme Fear": "😱", "Fear": "😟", "Neutral": "😐", "Greed": "😀", "Extreme Greed": "🤑"}
HYSTERESIS = 2.0  # points past a zone's edge before a move into it counts (no alerts while it wobbles on an edge)
CNN_TTL = 600.0  # CNN updates its index every few minutes while the market is open
CNN_RETRY = 120.0  # after a failure, wait this long before asking CNN again
DAY = 86400.0


@dataclass
class Gauge:
    market: str  # "Stocks" or "Crypto"
    source: str  # "CNN", "alternative.me" or "bot's estimate"
    score: float
    at: float  # when it was read (epoch seconds)
    close: float | None = None  # the previous close (stocks) or yesterday (crypto)
    week: float | None = None
    month: float | None = None
    year: float | None = None
    history: list[tuple[float, float]] = field(default_factory=list)  # (epoch seconds, score), oldest first
    parts: list[tuple[str, str, float]] = field(default_factory=list)  # (name, what it measures, 0-100)

    @property
    def label(self) -> str:
        return fear_greed_label(self.score)

    @property
    def emoji(self) -> str:
        return ZONE_EMOJI[self.label]

    @property
    def official(self) -> bool:
        """A published index (CNN, alternative.me), not the bot's stand-in."""
        return self.source != "bot's estimate"


def zone(value: float) -> int:
    """0 (Extreme Fear) to 4 (Extreme Greed), on the same edges as the labels."""
    return ZONES.index(fear_greed_label(value))


def score(value) -> float | None:
    """A 0-100 reading, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def _epoch(value) -> float | None:
    """Epoch seconds from CNN's ISO timestamps or millisecond numbers."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return value / 1000 if value > 1e11 else float(value)


def parse_cnn(data, now: float | None = None) -> Gauge:
    """CNN's graph data as a Gauge. Raises ValueError when it has no usable current reading."""
    now = time.time() if now is None else now
    if not isinstance(data, dict) or not isinstance(data.get("fear_and_greed"), dict):
        raise ValueError("no fear_and_greed reading")
    fg = data["fear_and_greed"]
    current = score(fg.get("score"))
    if current is None:
        raise ValueError(f"unusable score {fg.get('score')!r}")
    at = _epoch(fg.get("timestamp"))
    history: dict[float, float] = {}
    hist = data.get("fear_and_greed_historical")
    for point in (hist.get("data") if isinstance(hist, dict) and isinstance(hist.get("data"), list) else []):
        if not isinstance(point, dict):
            continue
        t, y = _epoch(point.get("x")), score(point.get("y"))
        if t is not None and y is not None and t <= now + DAY:
            history[t] = y
    parts = []
    for key, name, what in CNN_PARTS:
        part = data.get(key)
        value = score(part.get("score")) if isinstance(part, dict) else None
        if value is not None:
            parts.append((name, what, value))
    return Gauge("Stocks", "CNN", current, min(at or now, now), score(fg.get("previous_close")),
                 score(fg.get("previous_1_week")), score(fg.get("previous_1_month")), score(fg.get("previous_1_year")),
                 sorted(history.items()), parts)


def crypto_gauge(times, values) -> Gauge | None:
    """alternative.me's daily readings (epoch seconds and values, oldest first) as a Gauge."""
    pairs = sorted((float(t), v) for t, v in ((t, score(float(x)) if x is not None else None)
                                               for t, x in zip(times, values)) if v is not None)
    if not pairs:
        return None
    last_t, last = pairs[-1]

    def back(days: float) -> float | None:
        """The reading from `days` before the latest (within half a day), if there is one."""
        target = last_t - days * DAY
        best = min(pairs, key=lambda p: abs(p[0] - target))
        return best[1] if abs(best[0] - target) <= DAY / 2 else None

    return Gauge("Crypto", "alternative.me", last, last_t, back(1), back(7), back(30), back(365),
                 [p for p in pairs if p[0] >= last_t - 366 * DAY])


def estimate_gauge(mood: float | None, parts: dict[str, float], at: float) -> Gauge | None:
    """The bot's own stock market estimate, for when CNN isn't answering."""
    value = score(mood)
    if value is None:
        return None
    return Gauge("Stocks", "bot's estimate", value, at,
                 parts=[(name, "", 100 * v) for name, v in parts.items() if score(100 * v) is not None])


class CNNFearGreed:
    """CNN's index, cached for a few minutes and not asked again for a couple of minutes after a failure."""

    def __init__(self, http: Http, clock=time.monotonic):
        self.http = http
        self.clock = clock
        self._gauge: Gauge | None = None
        self._at = -math.inf
        self._failed_at = -math.inf

    async def get(self) -> Gauge | None:
        now = self.clock()
        if self._gauge is not None and now - self._at < CNN_TTL:
            return self._gauge
        if now - self._failed_at < CNN_RETRY:
            return self._gauge if self._gauge is not None and now - self._at < 6 * 3600 else None
        try:
            resp = await self.http.get(CNN_URL, headers=CNN_HEADERS, source="CNN", retries=1, timeout=20)
            if resp.status != 200:
                error = HttpError("CNN", f"HTTP {resp.status}", resp.status)
                if resp.status not in FAILING:  # those are already counted
                    self.http.record_failure("CNN", error)
                raise error
            try:
                gauge = parse_cnn(resp.json())
            except (HttpError, ValueError) as exc:
                self.http.record_failure("CNN", HttpError("CNN", f"unreadable answer ({exc})"[:100]))
                raise
        except (HttpError, ValueError):
            self._failed_at = self.clock()
            # A reading from the last few hours beats none while CNN is down.
            return self._gauge if self._gauge is not None and now - self._at < 6 * 3600 else None
        self._gauge, self._at = gauge, self.clock()
        return gauge


def moved_zone(previous: int | None, value: float) -> int | None:
    """The new zone when `value` has clearly moved out of zone `previous` (by HYSTERESIS points past the edge),
    else None. With no previous zone there's nothing to compare: None."""
    if previous is None:
        return None
    now = zone(value)
    if now > previous and zone(max(0.0, value - HYSTERESIS)) > previous:
        return now
    if now < previous and zone(min(100.0, value + HYSTERESIS)) < previous:
        return now
    return None
