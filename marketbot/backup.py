"""Backup price sources for when Yahoo Finance doesn't answer: Nasdaq for US stocks, ETFs and the Nasdaq indices
(live quotes, ten years of daily history, today's intraday prices, the whole-market screener) and Coinbase for crypto
(24-hour stats and candles back to each coin's listing). None of them needs a key."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from .hours import NEW_YORK
from .http import Http, HttpError
from .universe import CRYPTO, coin_base, market_of
from .yahoo import Bars, Quote

log = logging.getLogger(__name__)

NASDAQ = "https://api.nasdaq.com/api"
NASDAQ_HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                  "Referer": "https://www.nasdaq.com/"}
NASDAQ_BATCH = 20  # the watchlist endpoint answers at most 20 symbols per call
NASDAQ_INDICES = {"^IXIC": "COMP", "^NDX": "NDX"}  # the only indices Nasdaq's API has
NASDAQ_STATES = {"market open": "REGULAR", "pre market": "PRE", "pre-market": "PRE", "after hours": "POST",
                 "after-hours": "POST", "closed": "CLOSED", "market closed": "CLOSED"}
COINBASE = "https://api.exchange.coinbase.com"
DAY = 86400
OPEN_UTC = 14 * 3600 + 30 * 60  # a daily bar's timestamp: the New York open, roughly (the date is what matters)
UNSUPPORTED_TTL = 6 * 3600


def number(text) -> float | None:
    """Nasdaq's numbers ("$1,234.50", "-0.09%", "+1.73", "--", "N/A") as floats."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if np.isfinite(text) else None
    s = re.sub(r"[$,%+\s]", "", str(text))
    if not s or s in ("-", "--", "N/A", "NA"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if np.isfinite(v) else None


def nasdaq_symbol(symbol: str) -> str | None:
    """Yahoo symbol -> Nasdaq's (BRK-B -> BRK.B, ^IXIC -> COMP); None when Nasdaq doesn't carry it."""
    if symbol in NASDAQ_INDICES:
        return NASDAQ_INDICES[symbol]
    if symbol.startswith("^") or "=" in symbol or market_of(symbol) == CRYPTO or not symbol:
        return None
    return symbol.replace("-", ".")


def from_nasdaq_symbol(symbol: str) -> str:
    """Nasdaq's stock or ETF symbol -> Yahoo's (BRK.B or BRK/B -> BRK-B). Not for indices: the stock COMP is not the
    Nasdaq Composite."""
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def _ny_time(text: str | None) -> float:
    """Unix time from Nasdaq's timestamps ("2026-10-06T12:47:45.05-04:00", or New York time without a zone)."""
    if not text:
        return 0.0
    try:
        t = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=NEW_YORK)
    return t.timestamp()


def _day_t(text: str) -> int | None:
    """A daily bar's timestamp from "MM/DD/YYYY"."""
    try:
        d = datetime.strptime(text.strip(), "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    return int(d.timestamp()) + OPEN_UTC


_SHARE_WORDS = re.compile(r"\s+-?\s*\b(Common Stock|Common Shares|Ordinary Shares?|Class [A-Z] Ordinary Shares?|"
                          r"American Depositary Shares?|ADS|Depositary Shares?)\b.*$", re.I)


def clean_name(name: str) -> str:
    """'Apple Inc. Common Stock' -> 'Apple Inc.'; 'Alphabet Inc. Class A Common Stock' -> 'Alphabet Inc. Class A'.
    Only a trailing description is cut (Teads and ADS-TEC keep their names)."""
    original = re.sub(r"\s+", " ", (name or "").strip())
    cleaned = _SHARE_WORDS.sub("", original).strip(" -,")
    return cleaned or original


def bars_from_rows(symbol: str, rows: list[tuple[int, float, float, float, float, float]], source: str,
                   daily: bool = True) -> Bars:
    """Bars from (t, open, high, low, close, volume) rows in any order, dropping bad rows and repeated days (or
    repeated times, for intraday bars)."""
    clean = {}
    for t, o, h, l, c, v in rows:
        if t is None or c is None or not c > 0:
            continue
        o = o if o and o > 0 else c
        h = max(x for x in (h, o, c) if x and x > 0)
        l = min(x for x in (l, o, c) if x and x > 0)
        clean[int(t) // DAY if daily else int(t)] = (int(t), o, h, l, c, v or 0.0)
    ordered = [clean[k] for k in sorted(clean)]
    cols = list(zip(*ordered)) if ordered else [[], [], [], [], [], []]
    t, o, h, l, c, v = (np.array(x, dtype=float) for x in cols)
    return Bars(symbol, t.astype(np.int64), o, h, l, c, v, {"symbol": symbol, "source": source})


class Nasdaq:
    SOURCE = "Nasdaq"

    def __init__(self, http: Http, etfs=None):
        self.http = http
        self.etfs = etfs or (lambda symbol: False)  # does the symbol directory say it's an ETF?
        self._unknown: dict[str, float] = {}

    async def _get(self, path: str, params: list | dict | None = None) -> dict:
        resp = await self.http.get(f"{NASDAQ}{path}", params=params, headers=NASDAQ_HEADERS, source=self.SOURCE)
        if resp.status != 200:
            raise HttpError(self.SOURCE, f"Nasdaq: HTTP {resp.status}", resp.status)
        data = resp.json()
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise HttpError(self.SOURCE, "Nasdaq sent an unexpected answer", resp.status)
        return data

    def supports(self, symbol: str) -> bool:
        return nasdaq_symbol(symbol) is not None and time.monotonic() - self._unknown.get(symbol, -1e9) > UNSUPPORTED_TTL

    def asset_class(self, symbol: str) -> str:
        if symbol in NASDAQ_INDICES:
            return "index"
        return "etf" if self.etfs(symbol) else "stocks"

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        wanted = [s for s in dict.fromkeys(symbols) if self.supports(s)]
        out: dict[str, Quote] = {}
        first = {s: self.asset_class(s) for s in wanted}
        failed = await self._quote_batches(first, out)
        # Nasdaq needs the right asset class: try the other one for whatever it didn't know.
        retry = {s: ("stocks" if c == "etf" else "etf") for s, c in first.items()
                 if s not in out and s not in failed and c != "index"}
        if retry:
            failed |= await self._quote_batches(retry, out)
        now = time.monotonic()
        for s in wanted:
            if s not in out and s not in failed:
                self._unknown[s] = now
        return out

    async def _quote_batches(self, classes: dict[str, str], out: dict[str, Quote]) -> set[str]:
        """Fills `out`; returns the symbols whose batch failed (nothing was learned about them)."""
        items = list(classes.items())
        failed: set[str] = set()
        for i in range(0, len(items), NASDAQ_BATCH):
            batch = items[i:i + NASDAQ_BATCH]
            params = [("symbol", f"{nasdaq_symbol(s).lower()}|{c}") for s, c in batch]
            try:
                data = await self._get("/quote/watchlist", params)
            except HttpError as exc:
                log.info("Nasdaq quotes failed for %d symbols: %s", len(batch), exc)
                failed.update(s for s, _ in batch)
                continue
            # Matched by symbol and asset class: the stock COMP and the Nasdaq Composite (COMP|index) can share a
            # batch.
            by_key = {(nasdaq_symbol(s), c.upper()): s for s, c in batch}
            rows = data.get("data") if isinstance(data.get("data"), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = (str(row.get("symbol", "")).upper(), str(row.get("assetClass") or "").upper())
                sym = by_key.get(key)
                if sym is None:  # no asset class in the answer: match by symbol when that's unambiguous
                    matches = [s for (ns, _), s in by_key.items() if ns == key[0]]
                    sym = matches[0] if len(matches) == 1 else None
                try:
                    q = self.quote_from_row(row, sym) if sym else None
                except (TypeError, ValueError, AttributeError):
                    log.info("Odd Nasdaq quote row for %s skipped", sym)
                    q = None
                if q:
                    out[sym] = q
        return failed

    @staticmethod
    def quote_from_row(row: dict, symbol: str) -> Quote | None:
        price = number(row.get("lastSalePrice"))
        if price is None or price <= 0:
            return None
        prev = number(row.get("previousClosePrice"))
        pct = number(row.get("percentageChange"))
        if prev and prev > 0:
            pct = (price / prev - 1) * 100
        state = NASDAQ_STATES.get(str(row.get("marketStatus") or "").strip().lower(), "")
        kind = str(row.get("assetClass") or "").upper()
        at = _ny_time(row.get("lastTradeTimestampDateTime"))
        ext_price = ext_pct = None
        if state == "PRE" and prev:
            # Like Yahoo: before the open the price is yesterday's close and the early trades are "pre-market".
            ext_price, ext_pct = price, pct
            price, pct, at = prev, 0.0, 0.0
        elif state == "POST":
            # After hours Nasdaq's last sale is the after-hours trade and its change is from today's close: the
            # regular close is the last sale minus that change. Report it like Yahoo (close + after-hours price).
            change = number(row.get("netChange"))
            close = price - change if change is not None else None
            if close and close > 0:
                ext_price, ext_pct = price, (price / close - 1) * 100
                price = close
                pct = (close / prev - 1) * 100 if prev and prev > 0 else None
                session = datetime.fromtimestamp(at or time.time(), NEW_YORK)
                at = session.replace(hour=16, minute=0, second=0, microsecond=0).timestamp()
        return Quote(symbol=symbol, name=clean_name(str(row.get("companyName") or symbol)), price=price,
                     prev_close=prev, change_pct=pct, volume=number(row.get("volume")), time=at, market_state=state,
                     quote_type={"ETF": "ETF", "INDEX": "INDEX"}.get(kind, "EQUITY"), ext_price=ext_price,
                     ext_change_pct=ext_pct, source="Nasdaq")

    async def daily(self, symbol: str, start: int | None = None) -> Bars:
        """Up to ten years of daily bars (Nasdaq's limit). Prices are split-adjusted, not dividend-adjusted."""
        ns = nasdaq_symbol(symbol)
        if ns is None:
            raise HttpError(self.SOURCE, f"Nasdaq doesn't carry {symbol}")
        earliest = datetime.now(timezone.utc) - timedelta(days=3653)
        since = max(datetime.fromtimestamp(start, timezone.utc), earliest) if start else earliest
        rows = []
        for cls in dict.fromkeys([self.asset_class(symbol), "etf" if self.asset_class(symbol) == "stocks" else "stocks"]):
            data = await self._get(f"/quote/{ns}/historical", {"assetclass": cls, "fromdate": since.strftime("%Y-%m-%d"),
                                                               "limit": "9999"})
            table = ((data.get("data") or {}).get("tradesTable") or {})
            rows = table.get("rows") or []
            if rows or cls == "index":
                break
        if not rows:
            raise HttpError(self.SOURCE, f"Nasdaq has no history for {symbol}", 404)
        parsed = [(_day_t(r.get("date", "")), number(r.get("open")), number(r.get("high")), number(r.get("low")),
                   number(r.get("close")), number(r.get("volume"))) for r in rows]
        return bars_from_rows(symbol, parsed, "Nasdaq")

    async def intraday(self, symbol: str) -> tuple[Bars, float | None]:
        """Today's prices minute by minute (from 4 AM ET) and the previous close."""
        ns = nasdaq_symbol(symbol)
        if ns is None:
            raise HttpError(self.SOURCE, f"Nasdaq doesn't carry {symbol}")
        data = await self._get(f"/quote/{ns}/chart", {"assetclass": self.asset_class(symbol)})
        d = data.get("data") or {}
        points = d.get("chart") or []
        rows = []
        for p in points:
            x, y = p.get("x"), number(p.get("y"))
            if x is None or y is None:
                continue
            # x is New York wall-clock time written as if it were UTC.
            wall = datetime.fromtimestamp(x / 1000, timezone.utc).replace(tzinfo=NEW_YORK)
            rows.append((int(wall.timestamp()), y, y, y, y, 0.0))
        return bars_from_rows(symbol, rows, self.SOURCE, daily=False), number(d.get("previousClose"))

    async def screener(self) -> list[dict]:
        """Every US-listed stock with its last close, change, volume, market cap and sector (end of day)."""
        data = await self._get("/screener/stocks", {"tableonly": "true", "download": "true"})
        return ((data.get("data") or {}).get("rows")) or []

    async def nasdaq100(self) -> list[str]:
        data = await self._get("/quote/list-type/nasdaq100")
        rows = (((data.get("data") or {}).get("data") or {}).get("rows")) or []
        return [from_nasdaq_symbol(r["symbol"]) for r in rows if r.get("symbol")]


def coinbase_product(symbol: str) -> str | None:
    """Yahoo's BTC-USD or SUI20947-USD -> Coinbase's BTC-USD or SUI-USD."""
    if not symbol.endswith("-USD") or symbol.startswith("^"):
        return None
    base = coin_base(symbol)
    return f"{base}-USD" if base and not base.isdigit() else None  # an all-digit base is an id, not a ticker


class Coinbase:
    SOURCE = "Coinbase"
    CANDLES = 300  # per call

    def __init__(self, http: Http):
        self.http = http
        self._unknown: dict[str, float] = {}

    def supports(self, symbol: str) -> bool:
        p = coinbase_product(symbol)
        return p is not None and time.monotonic() - self._unknown.get(p, -1e9) > UNSUPPORTED_TTL

    async def _get(self, path: str, params: dict | None = None, product: str | None = None):
        resp = await self.http.get(f"{COINBASE}{path}", params=params, source=self.SOURCE)
        if resp.status in (400, 404) and product:
            self._unknown[product] = time.monotonic()
            raise HttpError(self.SOURCE, f"Coinbase doesn't list {product}", 404)
        if resp.status != 200:
            raise HttpError(self.SOURCE, f"Coinbase: HTTP {resp.status}", resp.status)
        return resp.json()

    async def quote(self, symbol: str) -> Quote | None:
        """Today's UTC-day candle as a quote, like Yahoo's crypto quotes: the change is since midnight UTC, and the
        day's high, low and volume are the UTC day's."""
        p = coinbase_product(symbol)
        if not p:
            return None
        now = int(time.time())
        midnight = now - now % DAY
        rows = await self._get(f"/products/{p}/candles", {
            "granularity": str(DAY), "start": datetime.fromtimestamp(midnight, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(now, timezone.utc).isoformat()}, product=p) or []
        today = next((r for r in rows if isinstance(r, list) and len(r) >= 6 and int(r[0]) == midnight), None)
        if today is None:
            return None  # just after midnight, before the day's first trade
        low, high, open_, close, volume = (number(x) for x in today[1:6])
        if not close or close <= 0:
            return None
        return Quote(symbol=symbol, name=p[:-4], price=close, prev_close=open_,
                     change_pct=(close / open_ - 1) * 100 if open_ else None, day_high=high, day_low=low,
                     volume=(volume or 0) * close, time=float(now), quote_type="CRYPTOCURRENCY", source="Coinbase")

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        wanted = [s for s in dict.fromkeys(symbols) if self.supports(s)]
        found = await asyncio.gather(*(self.quote(s) for s in wanted), return_exceptions=True)
        out = {}
        for s, q in zip(wanted, found):
            if isinstance(q, Quote):
                out[s] = q
            elif isinstance(q, HttpError) and q.status != 404:
                log.debug("Coinbase quote for %s failed: %s", s, q)
        return out

    async def candles(self, symbol: str, granularity: int, start: int, end: int) -> Bars:
        """Candles between two times, paging through Coinbase's 300-per-call limit (newest first)."""
        p = coinbase_product(symbol)
        if not p:
            raise HttpError(self.SOURCE, f"Coinbase doesn't list {symbol}", 404)
        rows, stop = [], end
        while stop > start:
            begin = max(start, stop - granularity * self.CANDLES)
            data = await self._get(f"/products/{p}/candles", {
                "granularity": str(granularity),
                "start": datetime.fromtimestamp(begin, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(stop, timezone.utc).isoformat()}, product=p)
            if not data:
                break  # before the coin was listed
            rows += [(int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5]) * float(r[4]))
                     for r in data if len(r) >= 6]
            stop = begin
        if not rows:
            raise HttpError(self.SOURCE, f"Coinbase has no candles for {symbol}", 404)
        return bars_from_rows(symbol, rows, self.SOURCE, daily=granularity >= DAY)

    async def daily(self, symbol: str, start: int | None = None) -> Bars:
        now = int(time.time())
        first = max(start or 0, 1420070400)  # Coinbase's history starts in 2015
        return await self.candles(symbol, DAY, first, now + DAY)

    async def intraday(self, symbol: str, days: float = 1, granularity: int = 300) -> Bars:
        now = int(time.time())
        return await self.candles(symbol, granularity, now - int(days * DAY), now)
