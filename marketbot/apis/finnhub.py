"""Finnhub's free plan (FINNHUB_API_KEY): company news, earnings (calendar and the last 4 surprises), analyst
recommendation trends, insider transactions and company profiles. 60 calls a minute; the bot keeps to 50, and
caches answers so the same question isn't asked twice in a few minutes. Price history, upgrades/downgrades,
Congress trades and the economic calendar are paid plans, so they come from elsewhere."""

from __future__ import annotations

import time
from datetime import date, timedelta

from . import Api, env_key

BASE = "https://finnhub.io/api/v1"
TTL = {"news": 600.0, "recommendation": 12 * 3600.0, "earnings": 6 * 3600.0, "profile": 7 * 86400.0,
       "insiders": 3600.0, "calendar": 3600.0, "metric": 12 * 3600.0}


class Finnhub(Api):
    def __init__(self, http, key: str | None = None, state_file=None, **kw):
        super().__init__("Finnhub", http, BASE, env_key("FINNHUB_API_KEY", "FINNHUB_KEY", "FINNHUB_TOKEN") if key is None
                         else key, key_header="X-Finnhub-Token", limits=((50, 60.0), (20, 1.0)),
                         state_file=state_file, **kw)
        self._cache: dict[tuple, tuple[float, object]] = {}

    async def _cached(self, kind: str, path: str, params: dict, wait: float = 5.0):
        key = (kind, path, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL[kind]:
            return hit[1]
        data = await self.get(path, params, wait=wait)
        self._cache[key] = (time.monotonic(), data)
        if len(self._cache) > 2000:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:500]:
                del self._cache[k]
        return data

    async def company_news(self, symbol: str, days: int = 3, today: date | None = None) -> list[dict]:
        """Newest first: [{datetime, headline, source, summary, url, related}]."""
        today = today or date.today()
        data = await self._cached("news", "company-news", {"symbol": symbol, "from": str(today - timedelta(days=days)),
                                                          "to": str(today)})
        items = [d for d in data if isinstance(d, dict) and d.get("headline")] if isinstance(data, list) else []
        return sorted(items, key=lambda d: -(d.get("datetime") or 0))

    async def recommendation(self, symbol: str) -> list[dict]:
        """Monthly analyst counts, newest first: [{period, strongBuy, buy, hold, sell, strongSell}]."""
        data = await self._cached("recommendation", "stock/recommendation", {"symbol": symbol})
        items = [d for d in data if isinstance(d, dict) and d.get("period")] if isinstance(data, list) else []
        return sorted(items, key=lambda d: d["period"], reverse=True)

    async def earnings(self, symbol: str) -> list[dict]:
        """The last 4 quarters' EPS, newest first: [{period, actual, estimate, surprisePercent}]."""
        data = await self._cached("earnings", "stock/earnings", {"symbol": symbol})
        items = [d for d in data if isinstance(d, dict) and d.get("period")] if isinstance(data, list) else []
        return sorted(items, key=lambda d: d["period"], reverse=True)

    async def profile(self, symbol: str) -> dict:
        data = await self._cached("profile", "stock/profile2", {"symbol": symbol})
        return data if isinstance(data, dict) else {}

    async def insider_transactions(self, symbol: str, days: int = 90, today: date | None = None) -> list[dict]:
        today = today or date.today()
        data = await self._cached("insiders", "stock/insider-transactions",
                                  {"symbol": symbol, "from": str(today - timedelta(days=days)), "to": str(today)})
        rows = data.get("data") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def earnings_calendar(self, start: date, end: date, symbol: str = "") -> list[dict]:
        params = {"from": str(start), "to": str(end)}
        if symbol:
            params["symbol"] = symbol
        data = await self._cached("calendar", "calendar/earnings", params)
        rows = data.get("earningsCalendar") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict) and r.get("symbol")] if isinstance(rows, list) else []
