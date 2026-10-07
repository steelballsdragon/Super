"""Yahoo Finance's public endpoints: daily history back to the 1920s, live quotes, fundamentals, options and news.

No API key is needed. Quotes, fundamentals and options need a session "crumb", which is fetched the same
way a browser gets it and refreshed when Yahoo rejects it. Requests go through http.Http, which looks like Chrome to
Yahoo (it refuses plain Python clients on cloud hosts).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import numpy as np

from .http import BROWSER_UA, Http, HttpError, Response

log = logging.getLogger(__name__)

BASE = "https://query1.finance.yahoo.com"
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json,text/plain,*/*"}  # for the other aiohttp clients
EARLIEST = -2208988800  # 1900-01-01: Yahoo returns from the first bar it has
CRUMB_TTL = 6 * 3600
QUOTE_BATCH = 40


class YahooError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Bars:
    """Price bars, oldest first. Daily bars are adjusted for splits and dividends."""
    symbol: str
    t: np.ndarray  # unix seconds of each bar's start
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.close)

    def tail(self, n: int) -> "Bars":
        return self.slice(max(len(self) - n, 0), len(self))

    def slice(self, start: int, stop: int) -> "Bars":
        return Bars(self.symbol, self.t[start:stop], self.open[start:stop], self.high[start:stop],
                    self.low[start:stop], self.close[start:stop], self.volume[start:stop], self.meta)

    def with_last(self, price: float, at: float, day_high: float | None = None, day_low: float | None = None,
                  volume: float | None = None, new_bar: bool = False) -> "Bars":
        """The bars with today's live price as the latest bar (a new one, or today's still forming)."""
        if not len(self):
            return self
        if new_bar:
            add = lambda a, v: np.append(a, v)
            return Bars(self.symbol, add(self.t, int(at)), add(self.open, price), add(self.high, day_high or price),
                        add(self.low, day_low or price), add(self.close, price), add(self.volume, volume or 0.0),
                        self.meta)
        b = Bars(self.symbol, self.t.copy(), self.open.copy(), self.high.copy(), self.low.copy(),
                 self.close.copy(), self.volume.copy(), self.meta)
        b.close[-1] = price
        b.high[-1] = max(b.high[-1], day_high or price, price)
        b.low[-1] = min(b.low[-1], day_low or price, price)
        if volume:
            b.volume[-1] = volume
        return b

    def to_arrays(self) -> dict:
        return {"t": self.t, "open": self.open, "high": self.high, "low": self.low, "close": self.close,
                "volume": self.volume}


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    prev_close: float | None
    change_pct: float | None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    high52: float | None = None
    low52: float | None = None
    currency: str = "USD"
    market_state: str = ""  # REGULAR, PRE, POST, CLOSED... (empty when unknown)
    time: float = 0.0
    quote_type: str = ""
    ext_price: float | None = None  # pre-market or after-hours price
    ext_change_pct: float | None = None
    extra: dict = field(default_factory=dict)  # market cap, P/E, earnings date, analyst rating...
    source: str = "Yahoo"

    @property
    def change(self) -> float | None:
        return None if self.prev_close in (None, 0) else self.price - self.prev_close


def _num(v):
    if isinstance(v, dict):
        v = v.get("raw")
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def parse_chart(result: dict, adjust: bool = True) -> Bars:
    """Bars from a chart response, dropping empty bars and filling missing open/high/low from the close."""
    meta = result.get("meta", {})
    ts = result.get("timestamp") or []
    ind = result.get("indicators", {})
    q = (ind.get("quote") or [{}])[0]
    n = len(ts)

    def col(values):
        return np.array([np.nan if v is None else v for v in (values or [None] * n)], dtype=float)[:n]

    close = col(q.get("close"))
    adj = col((ind.get("adjclose") or [{}])[0].get("adjclose")) if adjust and ind.get("adjclose") else None
    open_, high, low, vol = col(q.get("open")), col(q.get("high")), col(q.get("low")), col(q.get("volume"))
    t = np.array(ts, dtype=np.int64)
    keep = np.isfinite(close) & (close > 0)
    if adj is not None and len(adj) == n:
        keep &= np.isfinite(adj) & (adj > 0)
    t, close, open_, high, low, vol = t[keep], close[keep], open_[keep], high[keep], low[keep], vol[keep]
    if adj is not None and len(adj) == n:
        adj = adj[keep]
        factor = adj / close
        open_, high, low, close = open_ * factor, high * factor, low * factor, adj
    open_ = np.where(np.isfinite(open_) & (open_ > 0), open_, close)
    high = np.where(np.isfinite(high) & (high > 0), high, close)
    low = np.where(np.isfinite(low) & (low > 0), low, close)
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])
    vol = np.where(np.isfinite(vol), vol, 0.0)
    # Yahoo sometimes repeats the latest bar; keep the last copy of each timestamp.
    if len(t) > 1:
        last = np.r_[t[1:] != t[:-1], True]
        t, open_, high, low, close, vol = t[last], open_[last], high[last], low[last], close[last], vol[last]
    return Bars(meta.get("symbol", ""), t, open_, high, low, close, vol, meta)


def quote_from_meta(meta: dict) -> Quote | None:
    price = _num(meta.get("regularMarketPrice"))
    if price is None:
        return None
    prev = _num(meta.get("previousClose")) or _num(meta.get("chartPreviousClose"))
    pct = _num(meta.get("regularMarketChangePercent"))
    if pct is None and prev:
        pct = (price / prev - 1) * 100
    return Quote(
        symbol=meta.get("symbol", ""), name=meta.get("shortName") or meta.get("longName") or meta.get("symbol", ""),
        price=price, prev_close=prev, change_pct=pct, day_high=_num(meta.get("regularMarketDayHigh")),
        day_low=_num(meta.get("regularMarketDayLow")), volume=_num(meta.get("regularMarketVolume")),
        high52=_num(meta.get("fiftyTwoWeekHigh")), low52=_num(meta.get("fiftyTwoWeekLow")),
        currency=meta.get("currency") or "USD", time=_num(meta.get("regularMarketTime")) or 0.0,
        quote_type=meta.get("instrumentType", ""),
    )


EXTRA_FIELDS = ("marketCap", "trailingPE", "forwardPE", "priceToBook", "dividendYield", "epsTrailingTwelveMonths",
                "epsForward", "fiftyDayAverage", "twoHundredDayAverage", "averageAnalystRating", "earningsTimestamp",
                "earningsTimestampStart", "isEarningsDateEstimate", "sharesOutstanding", "fiftyTwoWeekChangePercent",
                "trailingAnnualDividendYield", "circulatingSupply", "volume24Hr", "exchange", "fullExchangeName")


def quote_from_v7(d: dict) -> Quote | None:
    price = _num(d.get("regularMarketPrice"))
    if price is None:
        return None
    state = d.get("marketState", "")
    ext_price = ext_pct = None
    if state in ("PRE", "PREPRE") and _num(d.get("preMarketPrice")):
        ext_price, ext_pct = _num(d.get("preMarketPrice")), _num(d.get("preMarketChangePercent"))
    elif state in ("POST", "POSTPOST", "CLOSED") and _num(d.get("postMarketPrice")):
        ext_price, ext_pct = _num(d.get("postMarketPrice")), _num(d.get("postMarketChangePercent"))
    return Quote(
        symbol=d.get("symbol", ""), name=d.get("shortName") or d.get("longName") or d.get("symbol", ""), price=price,
        prev_close=_num(d.get("regularMarketPreviousClose")), change_pct=_num(d.get("regularMarketChangePercent")),
        day_high=_num(d.get("regularMarketDayHigh")), day_low=_num(d.get("regularMarketDayLow")),
        volume=_num(d.get("regularMarketVolume")), avg_volume=_num(d.get("averageDailyVolume3Month")),
        high52=_num(d.get("fiftyTwoWeekHigh")), low52=_num(d.get("fiftyTwoWeekLow")),
        currency=d.get("currency") or "USD", market_state=state, time=_num(d.get("regularMarketTime")) or 0.0,
        quote_type=d.get("quoteType", ""), ext_price=ext_price, ext_change_pct=ext_pct,
        extra={k: d[k] for k in EXTRA_FIELDS if d.get(k) is not None},
    )


class YahooClient:
    SOURCE = "Yahoo"

    def __init__(self, http: Http | None = None, concurrency: int = 6):
        self.http = http or Http(limits={self.SOURCE: concurrency})
        self._own_http = http is None
        self._crumb: str | None = None
        self._crumb_at = 0.0
        self._crumb_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._own_http:
            await self.http.close()

    @property
    def resting(self) -> bool:
        return self.http.resting(self.SOURCE)

    async def _get(self, url: str, params: dict | None = None, answered: tuple[int, ...] = ()) -> Response:
        try:
            return await self.http.get(url, params=params, source=self.SOURCE, answered=answered)
        except HttpError as exc:
            raise YahooError(f"Yahoo: {exc}", exc.status) from exc

    async def get_json(self, url: str, params: dict | None = None, crumb: bool = False):
        """GET (the transport retries rate limits and server errors); with crumb=True a stale crumb is refreshed
        once."""
        for refreshed in (False, True):
            p = dict(params or {})
            used = None
            if crumb:
                used = p["crumb"] = await self.crumb()
            # An expired crumb is routine: its first rejection isn't counted against Yahoo's health (several
            # requests rejected at once would otherwise rest Yahoo before the crumb could be renewed).
            resp = await self._get(url, p, answered=(401, 403) if crumb and not refreshed else ())
            if resp.status in (401, 403) and crumb and not refreshed:
                if self._crumb == used:
                    self._crumb = None  # another request may have renewed it already
                continue
            if resp.status == 404:
                raise YahooError(f"not found: {url}", 404)
            if resp.status >= 400:
                raise YahooError(f"Yahoo: HTTP {resp.status}", resp.status)
            try:
                return resp.json()
            except HttpError as exc:
                raise YahooError(f"Yahoo: {exc}", resp.status) from exc
        raise YahooError("Yahoo rejected the crumb twice", 401)

    async def crumb(self) -> str:
        async with self._crumb_lock:
            if self._crumb and time.monotonic() - self._crumb_at < CRUMB_TTL:
                return self._crumb
            # Visiting fc.yahoo.com sets the session cookie (the page itself is a 404). It's optional, so it's
            # counted as its own source: its 404 mustn't make Yahoo look healthy, nor its failure rest Yahoo.
            try:
                await self.http.get("https://fc.yahoo.com", source=f"{self.SOURCE} cookie", retries=0, timeout=10)
            except HttpError:
                log.debug("fc.yahoo.com didn't answer; asking for a crumb anyway")
            resp = await self._get(f"{BASE}/v1/test/getcrumb")
            text = resp.text.strip()
            if resp.status != 200 or not text or "<" in text or " " in text or len(text) > 40:
                raise YahooError(f"couldn't get a Yahoo crumb (HTTP {resp.status})", resp.status)
            self._crumb, self._crumb_at = text, time.monotonic()
            return text

    # ----- prices -----

    async def chart(self, symbol: str, *, range_: str | None = None, interval: str = "1d",
                    start: int | None = None, end: int | None = None, prepost: bool = False) -> dict:
        params = {"interval": interval, "includeAdjustedClose": "true", "events": "div,splits",
                  "includePrePost": str(prepost).lower()}
        if range_:
            params["range"] = range_
        else:
            params["period1"] = str(EARLIEST if start is None else int(start))
            params["period2"] = str(int(end or time.time() + 86400))
        data = await self.get_json(f"{BASE}/v8/finance/chart/{symbol}", params)
        chart = (data or {}).get("chart") or {}
        if chart.get("error") or not chart.get("result"):
            raise YahooError(f"no chart for {symbol}: {chart.get('error')}")
        return chart["result"][0]

    async def daily(self, symbol: str, start: int | None = None) -> Bars:
        bars = parse_chart(await self.chart(symbol, start=start))
        bars.symbol = bars.symbol or symbol
        bars.meta = {**bars.meta, "source": self.SOURCE}
        return bars

    async def intraday(self, symbol: str, range_: str = "1d", interval: str = "5m", prepost: bool = True) -> Bars:
        bars = parse_chart(await self.chart(symbol, range_=range_, interval=interval, prepost=prepost), adjust=False)
        bars.symbol = bars.symbol or symbol
        return bars

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Live quotes (with market state, extended hours and fundamentals); falls back to chart data."""
        symbols = list(dict.fromkeys(symbols))
        out: dict[str, Quote] = {}
        try:
            for i in range(0, len(symbols), QUOTE_BATCH):
                batch = symbols[i:i + QUOTE_BATCH]
                data = await self.get_json(f"{BASE}/v7/finance/quote", {"symbols": ",".join(batch)}, crumb=True)
                for d in ((data or {}).get("quoteResponse") or {}).get("result") or []:
                    q = quote_from_v7(d)
                    if q:
                        out[q.symbol] = q
        except YahooError as exc:
            log.warning("Quote endpoint failed (%s); using chart data", exc)
        missing = [s for s in symbols if s not in out]
        if missing:
            out.update(await self.spark_quotes(missing))
        return out

    async def spark_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for i in range(0, len(symbols), 20):
            batch = symbols[i:i + 20]
            try:
                data = await self.get_json(f"{BASE}/v7/finance/spark",
                                           {"symbols": ",".join(batch), "range": "1d", "interval": "1d"})
            except YahooError as exc:
                log.debug("Spark quotes failed: %s", exc)  # e.g. a mistyped symbol
                continue
            for r in ((data or {}).get("spark") or {}).get("result") or []:
                for resp in r.get("response") or []:
                    q = quote_from_meta(resp.get("meta") or {})
                    if q:
                        out[q.symbol] = q
        return out

    async def spark_bars(self, symbols: list[str], range_: str = "1y", interval: str = "1d") -> dict[str, Bars]:
        """Closes only (open/high/low equal the close) for up to 20 symbols in one call. Symbols Yahoo doesn't
        know are left out."""
        if len(symbols) > 20:
            raise ValueError("spark takes at most 20 symbols")
        data = await self.get_json(f"{BASE}/v7/finance/spark",
                                   {"symbols": ",".join(symbols), "range": range_, "interval": interval})
        out: dict[str, Bars] = {}
        for r in ((data or {}).get("spark") or {}).get("result") or []:
            for resp in r.get("response") or []:
                bars = parse_chart(resp, adjust=False)
                bars.symbol = r.get("symbol") or bars.symbol
                if bars.symbol and len(bars):
                    out[bars.symbol] = bars
        return out

    # ----- research -----

    async def summary(self, symbol: str, modules: tuple[str, ...]) -> dict:
        data = await self.get_json(f"{BASE}/v10/finance/quoteSummary/{symbol}", {"modules": ",".join(modules)},
                                   crumb=True)
        result = ((data or {}).get("quoteSummary") or {}).get("result") or [{}]
        return result[0] or {}

    async def options(self, symbol: str, date: int | None = None) -> dict:
        params = {"date": str(date)} if date else {}
        data = await self.get_json(f"{BASE}/v7/finance/options/{symbol}", params, crumb=True)
        result = ((data or {}).get("optionChain") or {}).get("result") or [{}]
        return result[0] or {}

    async def search(self, query: str, news: int = 0, quotes: int = 8) -> tuple[list[dict], list[dict]]:
        data = await self.get_json(f"{BASE}/v1/finance/search",
                                   {"q": query, "newsCount": str(news), "quotesCount": str(quotes),
                                    "enableFuzzyQuery": "false"})
        return (data or {}).get("quotes") or [], (data or {}).get("news") or []

    async def screener(self, scr_id: str, count: int = 25) -> list[dict]:
        data = await self.get_json(f"{BASE}/v1/finance/screener/predefined/saved",
                                   {"scrIds": scr_id, "count": str(count)})
        result = ((data or {}).get("finance") or {}).get("result") or [{}]
        return (result[0] or {}).get("quotes") or []
