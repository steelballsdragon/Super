"""Where every price in the bot comes from: Yahoo Finance first, and when Yahoo doesn't answer, Nasdaq (US stocks,
ETFs and the Nasdaq indices), Coinbase and CoinGecko (crypto) and the symbol directory (lookups). Commands and jobs
call this instead of a single source, so one source going down doesn't take the bot down with it."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .backup import Coinbase, Nasdaq
from .directory import Directory
from .http import Http, HttpError
from .universe import CRYPTO, coin_base, market_of
from .yahoo import Bars, Quote, YahooClient, YahooError

log = logging.getLogger(__name__)

INTRADAY_DAYS = {"1d": 1, "5d": 5}
SCREENS = {"day_gainers", "day_losers", "most_actives"}
MIN_CAP = 2e9  # movers lists skip companies smaller than this (as Yahoo's do)
COINGECKO_WAIT = 8.0  # seconds the last-resort crypto prices may take
COINGECKO_MAX_AGE = 900.0  # older CoinGecko prices (a cached answer while it's down) aren't passed off as live


class DataUnavailable(YahooError):
    """No source could provide the data. Subclasses YahooError so existing handlers keep working."""

    def __init__(self, what: str, errors: list[Exception]):
        reasons = "; ".join(dict.fromkeys(str(e) for e in errors)) or "no source has it"
        super().__init__(f"{what}: {reasons}"[:400], next((getattr(e, "status", None) for e in errors
                                                          if getattr(e, "status", None)), None))
        self.errors = errors

    @property
    def unknown(self) -> bool:
        """Every source answered that it doesn't have it (rather than failing)."""
        return bool(self.errors) and all(getattr(e, "status", None) == 404 for e in self.errors)




class MarketData:
    def __init__(self, http: Http | None = None, directory: Directory | None = None, yahoo: YahooClient | None = None,
                 nasdaq: Nasdaq | None = None, coinbase: Coinbase | None = None, coins=None):
        self.http = http or Http(limits={"Yahoo": 6, "Nasdaq": 4, "Coinbase": 4},
                                 proxies={"Yahoo": os.environ.get("YAHOO_PROXY", "")})
        self.directory = directory if directory is not None else Directory([])
        self.yahoo = yahoo or YahooClient(self.http)
        self.nasdaq = nasdaq or Nasdaq(self.http, etfs=lambda s: self.directory.is_etf(s))
        self.coinbase = coinbase or Coinbase(self.http)
        self.coins = coins  # async () -> CoinGecko's top coins, the last resort for crypto prices

    async def close(self) -> None:
        await self.http.close()

    @property
    def health(self):
        return self.http.health

    @property
    def yahoo_ok(self) -> bool:
        return not self.yahoo.resting

    def outage(self) -> str | None:
        """Why prices can't be had right now (Yahoo and its backups all failing), or None."""
        h = self.http.health
        yahoo = h.get("Yahoo")
        if not yahoo or not yahoo.failing:
            return None
        backups = [h[s] for s in ("Nasdaq", "Coinbase") if s in h]
        if backups and not all(b.failing for b in backups):
            return None
        return f"Yahoo Finance: {yahoo.last_error}"

    def _canonical_coin(self, symbol: str) -> bool:
        """Whether the backups' ticker for this coin means this coin. They know a coin only by its ticker (Coinbase's
        SUI-USD, CoinGecko's "sui"), so a smaller coin sharing a ticker (Yahoo's HYPE-USD vs Hyperliquid's
        HYPE32196-USD) must not borrow the bigger one's prices."""
        biggest = self.directory.coin(coin_base(symbol))
        return biggest is None or biggest.symbol == symbol

    def _is_crypto(self, symbol: str) -> bool:
        listing = self.directory.get(symbol)
        return (listing.market == CRYPTO) if listing else market_of(symbol) == CRYPTO

    # ----- quotes -----

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Live quotes for whatever any source has; symbols nobody has are left out."""
        symbols = [s for s in dict.fromkeys(symbols) if s]
        out: dict[str, Quote] = {}
        if symbols and self.yahoo_ok:
            try:
                out.update(await self.yahoo.quotes(symbols))
            except Exception:
                log.warning("Yahoo quotes failed", exc_info=True)
        missing = [s for s in symbols if s not in out]
        if missing:
            out.update(await self._backup_quotes(missing))
        return out

    async def _backup_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        crypto = [s for s in symbols if self._is_crypto(s)]
        coins = [s for s in crypto if self._canonical_coin(s)]
        stocks = [s for s in symbols if s not in crypto and self.nasdaq.supports(s)]
        jobs = []
        if stocks:
            jobs.append(self.nasdaq.quotes(stocks))
        if coins:
            jobs.append(self.coinbase.quotes(coins))
        for found in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(found, dict):
                out.update(found)
            else:
                log.info("Backup quotes failed: %s", found)
        left = [s for s in coins if s not in out]
        if left and self.coins:
            try:
                # The last resort mustn't hold up the live boards: a few seconds, then go without.
                top = await asyncio.wait_for(self.coins(), COINGECKO_WAIT)
            except Exception as exc:
                log.info("CoinGecko prices failed: %s", exc)
                top = []
            now = time.time()
            by_ticker = {}
            for c in top:  # largest first, so a ticker means the biggest coin using it
                by_ticker.setdefault(c.symbol.upper(), c)
            for s in left:
                c = by_ticker.get(coin_base(s))
                age = now - (getattr(c, "at", 0) or 0) if c else None
                if c and c.price and age is not None and age < COINGECKO_MAX_AGE:
                    pct = c.change_24h
                    out[s] = Quote(symbol=s, name=c.name, price=c.price,
                                   prev_close=c.price / (1 + pct / 100) if pct not in (None, -100) else None,
                                   change_pct=pct, time=c.at, quote_type="CRYPTOCURRENCY", source="CoinGecko",
                                   extra={"change_window": "24h", "volume24h": c.volume})
        for s, q in out.items():
            listing = self.directory.get(s)
            if listing:
                q.name = listing.name  # the backups' names are terse ("HYPE32196") or formal
        return out

    # ----- history -----

    def _backups(self, symbol: str):
        if self._is_crypto(symbol):
            return [self.coinbase] if self.coinbase.supports(symbol) and self._canonical_coin(symbol) else []
        return [self.nasdaq] if self.nasdaq.supports(symbol) else []

    async def daily(self, symbol: str, start: int | None = None) -> Bars:
        errors: list[Exception] = []
        if self.yahoo_ok:
            try:
                return await self.yahoo.daily(symbol, start)
            except YahooError as exc:
                errors.append(exc)
        else:
            errors.append(YahooError("Yahoo is resting after repeated failures"))
        for source in self._backups(symbol):
            try:
                bars = await source.daily(symbol, start)
                if len(bars):
                    return bars
            except HttpError as exc:
                errors.append(exc)
        raise DataUnavailable(f"No price history for {symbol}", errors)

    async def intraday(self, symbol: str, range_: str = "1d", interval: str = "5m", prepost: bool = True) -> Bars:
        errors: list[Exception] = []
        if self.yahoo_ok:
            try:
                return await self.yahoo.intraday(symbol, range_, interval, prepost)
            except YahooError as exc:
                errors.append(exc)
        else:
            errors.append(YahooError("Yahoo is resting after repeated failures"))
        days = INTRADAY_DAYS.get(range_, 1)
        try:
            if self._is_crypto(symbol) and self.coinbase.supports(symbol) and self._canonical_coin(symbol):
                return await self.coinbase.intraday(symbol, days, 300 if days <= 1 else 900)
            if self.nasdaq.supports(symbol):
                bars, _ = await self.nasdaq.intraday(symbol)  # today only
                if len(bars):
                    return bars
        except HttpError as exc:
            errors.append(exc)
        raise DataUnavailable(f"No intraday prices for {symbol}", errors)

    # ----- lookups -----

    async def search(self, query: str, news: int = 0, quotes: int = 8) -> tuple[list[dict], list[dict]]:
        """Yahoo's search (symbols and news); the symbol directory when Yahoo is down (no news then)."""
        if self.yahoo_ok:
            try:
                return await self.yahoo.search(query, news=news, quotes=quotes)
            except YahooError as exc:
                log.info("Yahoo search failed (%s); using the symbol list", exc)
        found = [{"symbol": i.symbol, "shortname": i.name, "longname": i.name,
                  "quoteType": {"crypto": "CRYPTOCURRENCY", "etf": "ETF", "index": "INDEX",
                                "future": "FUTURE"}.get(i.kind, "EQUITY"),
                  "exchDisp": "Crypto" if i.market == CRYPTO else i.kind.upper()}
                 for i in self.directory.search(query, quotes)] if quotes else []
        return found, []

    async def screener(self, scr_id: str, count: int = 25) -> list[dict]:
        """Yahoo's predefined lists (day_gainers, day_losers, most_actives...); for the movers lists, Nasdaq's
        whole-market screener (as of the last close) when Yahoo is down."""
        errors: list[Exception] = []
        if self.yahoo_ok:
            try:
                rows = await self.yahoo.screener(scr_id, count)
                if rows:
                    return rows
            except YahooError as exc:
                errors.append(exc)
        if scr_id not in SCREENS:
            raise DataUnavailable(f"No {scr_id} list", errors)
        try:
            rows = await self.nasdaq.screener()
        except HttpError as exc:
            raise DataUnavailable(f"No {scr_id} list", errors + [exc]) from exc
        return nasdaq_movers(rows, scr_id, count)

    async def summary(self, symbol: str, modules: tuple[str, ...]) -> dict:
        return await self.yahoo.summary(symbol, modules)

    async def options(self, symbol: str, date: int | None = None) -> dict:
        return await self.yahoo.options(symbol, date)


def nasdaq_movers(rows: list[dict], scr_id: str, count: int) -> list[dict]:
    """Nasdaq screener rows as Yahoo screener quotes: the day's biggest gainers, losers or most traded companies
    worth at least $2B."""
    from .backup import clean_name, from_nasdaq_symbol, number
    out = []
    for r in rows:
        cap, price, pct, vol = (number(r.get(k)) for k in ("marketCap", "lastsale", "pctchange", "volume"))
        sym = from_nasdaq_symbol(str(r.get("symbol") or ""))
        if not sym or cap is None or cap < MIN_CAP or not price or pct is None or "^" in sym:
            continue
        out.append({"symbol": sym, "shortName": clean_name(str(r.get("name") or sym)), "regularMarketPrice": price,
                    "regularMarketChangePercent": pct, "regularMarketVolume": vol or 0, "marketCap": cap,
                    "source": "Nasdaq (last close)"})
    key = {"day_gainers": lambda q: -q["regularMarketChangePercent"],
           "day_losers": lambda q: q["regularMarketChangePercent"],
           "most_actives": lambda q: -q["regularMarketVolume"]}[scr_id]
    out.sort(key=key)
    if scr_id == "day_gainers":
        out = [q for q in out if q["regularMarketChangePercent"] > 0]
    elif scr_id == "day_losers":
        out = [q for q in out if q["regularMarketChangePercent"] < 0]
    return out[:count]
