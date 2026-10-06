"""Other free data: crypto market totals (CoinGecko), the crypto Fear & Greed index since 2018 (alternative.me),
and Robert Shiller's monthly S&P 500 data back to 1871 (prices, earnings, dividends, inflation, CAPE)."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from dataclasses import dataclass

import aiohttp
import numpy as np

from .yahoo import HEADERS

log = logging.getLogger(__name__)

COINGECKO = "https://api.coingecko.com/api/v3"
FEAR_GREED = "https://api.alternative.me/fng/"
SHILLER_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"


@dataclass
class CryptoGlobal:
    total_cap: float
    total_volume: float
    cap_change_24h: float
    btc_dominance: float
    eth_dominance: float
    stable_dominance: float


@dataclass
class Coin:
    symbol: str  # e.g. BTC
    name: str
    price: float
    cap: float
    rank: int
    change_1h: float | None
    change_24h: float | None
    change_7d: float | None
    ath_change: float | None
    volume: float


@dataclass
class LongRun:
    """Monthly S&P 500 since 1871. Missing values are NaN (earnings and CPI lag the price by months)."""
    year: np.ndarray
    month: np.ndarray
    price: np.ndarray
    dividend: np.ndarray
    earnings: np.ndarray
    cpi: np.ndarray
    long_rate: np.ndarray
    real_price: np.ndarray
    real_earnings: np.ndarray
    cape: np.ndarray

    @property
    def t(self) -> np.ndarray:
        """Fractional years, e.g. 1929.75 for October 1929."""
        return self.year + (self.month - 1) / 12


def parse_shiller(text: str) -> LongRun:
    rows = list(csv.DictReader(io.StringIO(text)))
    def col(name):
        out = []
        for r in rows:
            try:
                v = float(r.get(name) or 0)
            except ValueError:
                v = 0.0
            out.append(v if v != 0 else np.nan)
        return np.array(out)
    year = np.array([int(r["Date"][:4]) for r in rows])
    month = np.array([int(r["Date"][5:7]) for r in rows])
    return LongRun(year, month, col("SP500"), col("Dividend"), col("Earnings"), col("Consumer Price Index"),
                   col("Long Interest Rate"), col("Real Price"), col("Real Earnings"), col("PE10"))


def fear_greed_label(value: float) -> str:
    if value < 25:
        return "Extreme Fear"
    if value < 45:
        return "Fear"
    if value <= 55:
        return "Neutral"
    if value <= 75:
        return "Greed"
    return "Extreme Greed"


class Sources:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._cache: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _cached(self, key: str, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        async with self._locks.setdefault(key, asyncio.Lock()):
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            try:
                value = await fetch()
            except Exception:
                if hit:  # an old answer beats none when the source is down
                    log.warning("%s refresh failed; using the last one", key, exc_info=True)
                    return hit[1]
                raise
            self._cache[key] = (time.monotonic(), value)
            return value

    async def _get(self, url: str, params: dict | None = None, as_text: bool = False):
        session = await self.session()
        for attempt in range(3):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        raise aiohttp.ClientResponseError(resp.request_info, (), status=resp.status)
                    resp.raise_for_status()
                    return await resp.text() if as_text else await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * 2 ** attempt)

    async def crypto_global(self) -> CryptoGlobal:
        async def fetch():
            d = (await self._get(f"{COINGECKO}/global"))["data"]
            pct = d.get("market_cap_percentage", {})
            stables = sum(pct.get(k, 0) for k in ("usdt", "usdc", "dai", "usde", "fdusd"))
            return CryptoGlobal(d["total_market_cap"]["usd"], d["total_volume"]["usd"],
                                d.get("market_cap_change_percentage_24h_usd") or 0.0, pct.get("btc", 0.0),
                                pct.get("eth", 0.0), stables)
        return await self._cached("global", 300, fetch)

    async def top_coins(self, count: int = 100) -> list[Coin]:
        async def fetch():
            data = await self._get(f"{COINGECKO}/coins/markets", {
                "vs_currency": "usd", "order": "market_cap_desc", "per_page": str(count), "page": "1",
                "price_change_percentage": "1h,24h,7d"})
            return [Coin(c["symbol"].upper(), c["name"], c.get("current_price") or 0.0, c.get("market_cap") or 0.0,
                         c.get("market_cap_rank") or 0, c.get("price_change_percentage_1h_in_currency"),
                         c.get("price_change_percentage_24h_in_currency"),
                         c.get("price_change_percentage_7d_in_currency"), c.get("ath_change_percentage"),
                         c.get("total_volume") or 0.0)
                    for c in data if c.get("current_price")]
        return await self._cached(f"coins{count}", 300, fetch)

    async def fear_greed(self) -> tuple[np.ndarray, np.ndarray]:
        """(day timestamps, values 0-100), oldest first, back to February 2018."""
        async def fetch():
            data = (await self._get(FEAR_GREED, {"limit": "0"}))["data"]
            pairs = sorted((int(d["timestamp"]), float(d["value"])) for d in data)
            return np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        return await self._cached("fng", 3600, fetch)

    async def long_run(self) -> LongRun:
        async def fetch():
            return parse_shiller(await self._get(SHILLER_CSV, as_text=True))
        return await self._cached("shiller", 7 * 86400, fetch)
