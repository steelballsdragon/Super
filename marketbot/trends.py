"""Market trends: the day's biggest gainers, losers and most traded stocks across the whole US market, the week's,
month's, quarter's and year's movers among the S&P 500, the Nasdaq-100 and the major ETFs, the sectors, and the top
250 coins over 24 hours, 7 days, 30 days and a year.

Today's lists come from Yahoo's screeners. The longer periods are worked out from a year of daily closes (Yahoo's
spark endpoint, 20 symbols a call). The closes are saved, so if Yahoo stops answering the periods still work from the
saved closes and Nasdaq's live prices.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .hours import NEW_YORK
from .universe import SECTORS

log = logging.getLogger(__name__)

PERIODS = {"1D": "today", "WTD": "this week", "MTD": "this month", "1W": "over 5 sessions",
           "1M": "over 21 sessions", "3M": "over 3 months", "YTD": "year to date", "1Y": "over a year"}
BARS_BACK = {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "1Y": 252}
# CoinGecko has rolling 24-hour, 7-day, 30-day and 1-year changes; calendar periods use the nearest.
CRYPTO_PERIODS = {"1D": "change_24h", "1W": "change_7d", "1M": "change_30d", "1Y": "change_1y"}
CRYPTO_ALIASES = {"WTD": "1W", "MTD": "1M"}
CRYPTO_LABELS = {"1D": "in 24 hours", "1W": "over 7 days", "1M": "over 30 days", "1Y": "over a year"}
BAR_HOUR = 14 * 3600 + 30 * 60  # a day number's bar time (UTC): the New York morning, so the date is right
MAJOR_ETFS = ["SPY", "QQQ", "DIA", "IWM", "VTI", "TLT", "IEF", "HYG", "LQD", "GLD", "SLV", "USO", "UNG", "EEM",
              "EFA", "FXI", "EWJ", "ARKK", "SOXX", "XBI", "KRE", "IBB", "ITB", "XHB", "IBIT", "ETHA", "BITO", "VNQ",
              "TAN", "URA", "JETS"]
SPARK_BATCH = 20
JUMP = float(np.log(1.75))  # a one-day move beyond x1.75 or /1.75 is taken for a split or spin-off
MIN_COIN_VOLUME = 1e6  # dollars a day: thinner coins stay off the movers lists
KEEP_DAYS = 300
DAY = 86400
STABLES = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "PYUSD", "USDS", "TUSD", "USD1", "USDD", "BUIDL", "USDTB", "RLUSD",
           "USD0", "USDF", "GHO", "FRAX", "LUSD", "CRVUSD", "SUSDE", "USDX", "USDG", "EURC", "XAUT", "PAXG"}
DERIVED = re.compile(r"tokeni[sz]ed|xstock|bstock|wrapped|bridged|staked|restaked|liquid staking", re.I)


@dataclass
class Mover:
    symbol: str
    name: str
    price: float
    change: float  # percent
    volume: float | None = None
    cap: float | None = None


@dataclass
class Snapshot:
    """One refresh of everything the trends channel shows."""
    at: float
    day_gainers: list[Mover] = field(default_factory=list)  # the whole US market, companies worth $2B+
    day_losers: list[Mover] = field(default_factory=list)
    most_active: list[Mover] = field(default_factory=list)
    day_source: str = ""
    changes: dict[str, dict[str, float]] = field(default_factory=dict)  # symbol -> period -> percent
    prices: dict[str, float] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    periods_source: str = ""
    coins: list = field(default_factory=list)  # sources.Coin, biggest first
    errors: list[str] = field(default_factory=list)

    def movers(self, period: str, symbols: list[str] | None = None) -> list[Mover]:
        """Everyone with a change over the period, best first."""
        pool = symbols if symbols is not None else list(self.changes)
        out = [Mover(s, self.names.get(s, s), self.prices.get(s, 0.0), self.changes[s][period])
               for s in pool if s in self.changes and period in self.changes[s]]
        out.sort(key=lambda m: -m.change)
        return out

    def breadth(self, symbols: list[str], period: str = "1D") -> tuple[int, int]:
        """(up, down) among the symbols over the period."""
        moves = [self.changes[s][period] for s in symbols if period in self.changes.get(s, {})]
        return sum(1 for m in moves if m > 0), sum(1 for m in moves if m < 0)

    def coin_movers(self, period: str) -> list[Mover]:
        attr = CRYPTO_PERIODS.get(CRYPTO_ALIASES.get(period, period))
        if not attr:
            return []
        out = [Mover(f"{c.symbol}-USD", c.name, c.price, getattr(c, attr), c.volume, c.cap)
               for c in self.coins if real_coin(c) and getattr(c, attr, None) is not None
               and (c.volume or 0) >= MIN_COIN_VOLUME]
        out.sort(key=lambda m: -m.change)
        return out


def real_coin(c) -> bool:
    """Not a stablecoin, a wrapped or staked copy of another coin, or a tokenized stock."""
    if c.symbol.upper() in STABLES or DERIVED.search(c.name or ""):
        return False
    return not (0.97 < (c.price or 0) < 1.03 and abs(c.change_7d or 0) < 1.5)


def period_changes(t: np.ndarray, close: np.ndarray, now: float | None = None) -> dict[str, float]:
    """Percent changes over each period from daily closes (oldest first). The latest close may be today's, still
    moving. Year to date is measured from the last close of the previous year.

    These closes aren't adjusted for splits and spin-offs, which show up as one-day jumps no big company or ETF
    really makes; a period that contains one is left out rather than reported as a huge move."""
    ok = np.isfinite(close) & (close > 0)
    t, close = t[ok], close[ok]
    out: dict[str, float] = {}
    if len(close) < 2:
        return out
    jumps = np.abs(np.diff(np.log(close))) > JUMP

    def clean_since(i: int) -> bool:  # no jump between close i and the last one
        return not jumps[i:].any()

    last = close[-1]
    for period, back in BARS_BACK.items():
        start = len(close) - 1 - back
        if start < 0 and period == "1Y" and len(close) > 200:
            start = 0  # a newer listing: since its first close
        if start >= 0 and clean_since(start):
            out[period] = float((last / close[start] - 1) * 100)
    # Calendar periods: from the last close before this week, month or year began (New York time).
    today = datetime.fromtimestamp(now or t[-1], NEW_YORK).date()
    starts = {"WTD": today - timedelta(days=today.weekday()), "MTD": today.replace(day=1),
              "YTD": today.replace(month=1, day=1)}
    for period, first in starts.items():
        begin = datetime(first.year, first.month, first.day, tzinfo=NEW_YORK).timestamp()
        before = np.nonzero(t < begin)[0]
        if len(before) and clean_since(int(before[-1])):
            out[period] = float((last / close[before[-1]] - 1) * 100)
    return out


class ClosesStore:
    """A year or so of daily closes for the trends universe, saved, so the periods survive a Yahoo outage."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.days: dict[str, dict[int, float]] = {}  # symbol -> day number -> close
        self._load()

    def update(self, symbol: str, t: np.ndarray, close: np.ndarray) -> None:
        series = self.days.setdefault(symbol, {})
        for day, c in zip((t // DAY).tolist(), close.tolist()):
            if c and np.isfinite(c) and c > 0:
                series[int(day)] = float(c)
        if len(series) > KEEP_DAYS:
            for day in sorted(series)[:-KEEP_DAYS]:
                del series[day]

    def series(self, symbol: str, extra: dict[int, float] | None = None, before: int | None = None
               ) -> tuple[np.ndarray, np.ndarray]:
        """Saved closes (only days before `before`, if given) plus `extra` {day number: close}, oldest first."""
        s = {d: c for d, c in self.days.get(symbol, {}).items() if before is None or d < before}
        s.update(extra or {})
        days = sorted(s)
        return np.array(days, dtype=np.int64) * DAY + BAR_HOUR, np.array([s[d] for d in days], dtype=float)

    def save(self) -> None:
        if not self.path:
            return
        symbols = sorted(self.days)
        all_days = sorted({d for s in symbols for d in self.days[s]})[-KEEP_DAYS:]
        index = {d: i for i, d in enumerate(all_days)}
        grid = np.full((len(symbols), len(all_days)), np.nan, dtype=np.float32)
        for row, s in enumerate(symbols):
            for d, c in self.days[s].items():
                if d in index:
                    grid[row, index[d]] = c
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".npz")
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez_compressed(f, symbols=np.array(symbols), days=np.array(all_days, dtype=np.int64), grid=grid)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with np.load(self.path, allow_pickle=False) as z:
                symbols, days, grid = z["symbols"], z["days"], z["grid"]
            if grid.ndim != 2 or grid.shape != (len(symbols), len(days)):
                raise ValueError(f"shapes don't match: {grid.shape} vs {len(symbols)} x {len(days)}")
            loaded = {}
            for row, s in enumerate(symbols.tolist()):
                vals = grid[row]
                ok = np.isfinite(vals) & (vals > 0)
                loaded[str(s)] = {int(d): float(c) for d, c in zip(days[ok].tolist(), vals[ok].tolist())}
        except FileNotFoundError:
            return
        except Exception:
            log.warning("Saved trend closes are unreadable; starting over", exc_info=True)
            return
        self.days = loaded


def movers_from_screener(rows: list[dict]) -> list[Mover]:
    """Screener quotes as movers; rows with missing or unreadable numbers are skipped."""
    from .backup import number
    out = []
    for q in rows or []:
        if not isinstance(q, dict):
            continue
        sym, price, chg = q.get("symbol"), number(q.get("regularMarketPrice")), number(q.get("regularMarketChangePercent"))
        if not sym or not isinstance(sym, str) or price is None or chg is None:
            continue
        out.append(Mover(sym, str(q.get("shortName") or q.get("longName") or sym), price, chg,
                         number(q.get("regularMarketVolume")), number(q.get("marketCap"))))
    return out


class TrendsDesk:
    def __init__(self, data, sources, directory, folder: str | Path | None = None):
        self.data = data  # MarketData
        self.sources = sources
        self.directory = directory
        self.closes = ClosesStore(Path(folder) / "trend-closes.npz" if folder else None)
        self.last: Snapshot | None = None
        self._lock = asyncio.Lock()

    def universe(self) -> list[str]:
        """S&P 500, Nasdaq-100, sector ETFs and the major ETFs."""
        d = self.directory
        members = d.members("sp500") + d.members("ndx100") if d else []
        return list(dict.fromkeys(members + list(SECTORS) + MAJOR_ETFS))

    def index_members(self, tag: str) -> list[str]:
        return self.directory.members(tag) if self.directory else []

    async def refresh(self, max_age: float = 0) -> Snapshot:
        """A fresh snapshot (or the last one if it's younger than max_age seconds)."""
        if self.last and time.time() - self.last.at < max_age:
            return self.last
        async with self._lock:
            if self.last and time.time() - self.last.at < max_age:
                return self.last
            snap = Snapshot(time.time())
            await asyncio.gather(self._day_lists(snap), self._periods(snap), self._coins(snap))
            self.last = snap
            return snap

    async def _day_lists(self, snap: Snapshot) -> None:
        for attr, scr in (("day_gainers", "day_gainers"), ("day_losers", "day_losers"),
                          ("most_active", "most_actives")):
            try:
                rows = await self.data.screener(scr, 25)
                setattr(snap, attr, movers_from_screener(rows))
                if rows and isinstance(rows[0], dict):
                    snap.day_source = "Nasdaq (last close)" if rows[0].get("source") else "Yahoo Finance"
            except Exception as exc:
                snap.errors.append(f"{scr}: {exc}"[:160])

    async def _periods(self, snap: Snapshot) -> None:
        symbols = self.universe()
        got = await self._spark(symbols, snap)
        missing = [s for s in symbols if s not in got]
        live = {}
        if missing:
            # Yahoo didn't answer for these: today's price from a backup, the history from the saved closes.
            try:
                live = await self.data.quotes(missing)
            except Exception as exc:
                snap.errors.append(f"backup quotes: {exc}"[:160])
        now = time.time()
        for s in symbols:
            if s in got:
                t, c = got[s]
                ch = period_changes(t, c, now)
            elif s in live and live[s].price:
                ch, c = self.backup_changes(s, live[s], now)
                snap.names.setdefault(s, live[s].name)
            else:
                continue
            if ch:
                snap.changes[s] = ch
                snap.prices[s] = float(c[-1])
        for s in snap.changes:
            if s not in snap.names:
                item = self.directory.get(s) if self.directory else None
                snap.names[s] = item.name if item else s
        snap.periods_source = ("Yahoo Finance" if got and len(got) >= len(live) else
                               "saved closes + Nasdaq" if live else "")
        if got:
            try:
                self.closes.save()
            except OSError:
                log.warning("Couldn't save the trend closes", exc_info=True)

    def backup_changes(self, symbol: str, q, now: float) -> tuple[dict[str, float], np.ndarray]:
        """Period changes from a backup quote and the saved closes. Today's change is the quote's own; the longer
        periods only when the saved closes run up to the session before the quote's previous close (no gap)."""
        from .hours import is_trading_day
        session = datetime.fromtimestamp(q.time or now, NEW_YORK).date()
        if (q.market_state or "").startswith("PRE") and not q.time:
            session = _trading_day_before(session, is_trading_day)  # before the open: yesterday's close
        elif not is_trading_day(session):
            session = _trading_day_before(session + timedelta(days=1), is_trading_day)  # the last trading day
        prev = _trading_day_before(session, is_trading_day)
        day_no = lambda d: (d - EPOCH).days
        extra = {day_no(session): q.price}
        if q.prev_close and q.prev_close > 0:
            extra[day_no(prev)] = q.prev_close
        t, c = self.closes.series(symbol, extra, before=day_no(prev))
        ch: dict[str, float] = {}
        saved_days = [d for d in self.closes.days.get(symbol, {}) if d < day_no(prev)]
        if q.prev_close and saved_days and max(saved_days) == day_no(_trading_day_before(prev, is_trading_day)):
            ch = period_changes(t, c, now)
        ch.pop("1D", None)
        if q.change_pct is not None:
            ch["1D"] = float(q.change_pct)
        return ch, c

    async def _spark(self, symbols: list[str], snap: Snapshot) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """A year of daily closes per symbol from Yahoo, 20 symbols a call."""
        if not self.data.yahoo_ok:
            return {}
        out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        batches = [symbols[i:i + SPARK_BATCH] for i in range(0, len(symbols), SPARK_BATCH)]
        results = await asyncio.gather(*(self.data.yahoo.spark_bars(b, "1y", "1d") for b in batches),
                                       return_exceptions=True)
        failed = 0
        for batch, res in zip(batches, results):
            if isinstance(res, BaseException):
                failed += 1
                continue
            for s, bars in res.items():
                if len(bars) >= 2:
                    out[s] = (bars.t, bars.close)
                    self.closes.update(s, bars.t, bars.close)
                    name = bars.meta.get("shortName") or bars.meta.get("longName")
                    if name:
                        snap.names[s] = name
        if failed:
            snap.errors.append(f"Yahoo spark: {failed} of {len(batches)} batches failed")
        return out

    async def _coins(self, snap: Snapshot) -> None:
        try:
            snap.coins = await self.sources.top_coins(250)
        except Exception as exc:
            snap.errors.append(f"CoinGecko: {exc}"[:160])


EPOCH = datetime(1970, 1, 1).date()


def _trading_day_before(day, is_trading_day):
    d = day - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


# ----- recaps: when they're due -----

def last_trading_day_of_week(day, is_trading_day) -> bool:
    """Whether `day` is the week's last trading day (usually Friday; Thursday before a Good Friday)."""
    if not is_trading_day(day):
        return False
    d = day + timedelta(days=1)
    while d.weekday() < 5:
        if is_trading_day(d):
            return False
        d += timedelta(days=1)
    return True


def last_trading_day_of_month(day, is_trading_day) -> bool:
    if not is_trading_day(day):
        return False
    d = day + timedelta(days=1)
    while d.month == day.month:
        if is_trading_day(d):
            return False
        d += timedelta(days=1)
    return True
