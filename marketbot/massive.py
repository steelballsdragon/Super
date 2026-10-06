"""Massive (formerly Polygon.io) for the NVIDIA channel, never more than 5 calls in any minute (the free plan's limit).

The free plan (Stocks Basic) is end-of-day: the previous session's bar, two years of daily and minute bars, technical
indicators, company details, dividends, splits and news with per-stock sentiment. It has no live prices (those start
with the paid plans), so NVIDIA's live price comes from Yahoo like every other symbol and Massive supplies the rest.
If the key is for a paid plan, its delayed snapshot is picked up automatically.

Every Massive request takes a slot from one shared limiter first, so nothing (background refreshes, commands, retries)
can go over the budget. Requests that would go over wait for the next refresh instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hours import NEW_YORK
from .http import Http, HttpError
from .storage import read_json, write_json

log = logging.getLogger(__name__)

BASE = "https://api.massive.com"
SOURCE = "Massive"
CALLS_PER_MINUTE = 5
WINDOW = 61.0  # a second of slack on Massive's minute
KEY_NAMES = ("MASSIVE_API_KEY", "MASSIVE_KEY", "MASSIVE_API", "MASSIVE_TOKEN", "MASSIVE", "POLYGON_API_KEY",
             "POLYGON_KEY", "POLYGON_API", "POLYGON")
SYMBOL = "NVDA"
_KEYLIKE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def find_key(env=os.environ) -> tuple[str | None, str | None]:
    """(key, the variable it came from). Looks for the usual names, then any variable named like MASSIVE…/POLYGON…
    (the key may have been saved under another name)."""
    for name in KEY_NAMES:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    for name in sorted(env):
        upper = name.upper()
        if ("MASSIVE" in upper or "POLYGON" in upper) and _KEYLIKE.match((env.get(name) or "").strip()):
            return env[name].strip(), name
    return None, None


class RateLimiter:
    """At most `calls` requests in any `window` seconds (a sliding window, so there are no bursts at a minute's
    edge). Waiters are served in order."""

    def __init__(self, calls: int = CALLS_PER_MINUTE, window: float = WINDOW, clock=time.monotonic,
                 sleep=asyncio.sleep):
        self.calls = calls
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self.total = 0

    def _trim(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self.window:
            self._sent.popleft()

    def used(self) -> int:
        """Requests in the last window."""
        self._trim(self._clock())
        return len(self._sent)

    def wait_time(self) -> float:
        now = self._clock()
        self._trim(now)
        wait = max(self._blocked_until - now, 0.0)
        if len(self._sent) >= self.calls:
            wait = max(wait, self._sent[0] + self.window - now)
        return wait

    def _take(self) -> None:
        self._sent.append(self._clock())
        self.total += 1

    def try_acquire(self) -> bool:
        """A slot now, or False (never waits)."""
        if self.wait_time() > 0:
            return False
        self._take()
        return True

    async def acquire(self, timeout: float | None = None) -> bool:
        """A slot, waiting up to `timeout` seconds for one (forever if None); False if it would take longer."""
        deadline = None if timeout is None else self._clock() + timeout
        async with self._lock:
            while True:
                wait = self.wait_time()
                if wait <= 0:
                    self._take()
                    return True
                if deadline is not None and self._clock() + wait > deadline:
                    return False
                await self._sleep(wait)

    def penalize(self, seconds: float = WINDOW) -> None:
        """Massive said we went over: send nothing for a while."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)


class MassiveError(Exception):
    def __init__(self, message: str, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind  # "budget", "key", "plan", "rate", "network" or "other"
        self.status = status


class Massive:
    def __init__(self, key: str | None, http: Http, limiter: RateLimiter | None = None):
        self.key = key
        self.http = http
        self.limiter = limiter or RateLimiter()
        self.last_error: str | None = None
        self.key_rejected = False

    @property
    def enabled(self) -> bool:
        return bool(self.key) and not self.key_rejected

    async def get(self, path: str, params: dict | None = None, wait: float = 0.0) -> dict:
        """One request (one slot of the budget). Raises MassiveError."""
        if not self.enabled:
            raise MassiveError("no usable Massive key", "key")
        ok = self.limiter.try_acquire() if wait <= 0 else await self.limiter.acquire(wait)
        if not ok:
            raise MassiveError("the 5-a-minute budget is used up for now", "budget")
        try:
            # The key goes in a header, so it never shows up in a logged URL.
            resp = await self.http.get(f"{BASE}{path}", params=params, source=SOURCE, retries=0, force=True,
                                       headers={"Authorization": f"Bearer {self.key}"}, answered=(403,))
        except HttpError as exc:
            if exc.status == 429:
                self.limiter.penalize()
                self.last_error = "rate limited by Massive (backing off a minute)"
                raise MassiveError(self.last_error, "rate", 429) from exc
            self.last_error = str(exc)
            raise MassiveError(str(exc), "network", exc.status) from exc
        try:
            data = resp.json() or {}
        except HttpError:
            data = {}
        if resp.status == 401:
            self.key_rejected = True
            self.last_error = f"Massive rejected the key ({data.get('error') or 'HTTP 401'})"
            raise MassiveError(self.last_error, "key", 401)
        if resp.status == 403:
            raise MassiveError(data.get("message") or "not in this Massive plan", "plan", 403)
        if resp.status >= 400:
            self.last_error = f"HTTP {resp.status}: {data.get('error') or data.get('message') or ''}"[:160]
            raise MassiveError(self.last_error, "other", resp.status)
        self.last_error = None
        return data


# ----- the NVIDIA desk -----

@dataclass(frozen=True)
class Job:
    name: str
    every_open: float  # seconds between refreshes while the market is open
    every_closed: float  # and otherwise
    paid: bool = False  # needs a paid plan: probed rarely until it works


INDICATORS = {"sma50": ("sma", 50), "sma200": ("sma", 200), "ema20": ("ema", 20), "rsi14": ("rsi", 14),
              "macd": ("macd", 0)}
JOBS = [  # in priority order: when the budget is short, the first ones go first
    Job("snapshot", 60, 900, paid=True),
    Job("news", 180, 600),
    Job("prev", 900, 1800),
    Job("status", 900, 3600),
    Job("daily", 6 * 3600, 6 * 3600),
    Job("minutes", 6 * 3600, 6 * 3600),
    *(Job(name, 6 * 3600, 6 * 3600) for name in INDICATORS),
    Job("details", 86400, 86400),
    Job("dividends", 86400, 86400),
    Job("splits", 7 * 86400, 7 * 86400),
    Job("related", 7 * 86400, 7 * 86400),
]
PLAN_RETRY = 12 * 3600  # how often to re-check an endpoint the plan didn't include
AFTER_NEW_SESSION = ("daily", "minutes", *INDICATORS)  # refreshed as soon as a new session's bar is out
MAX_NEWS = 40


def _date(t: float) -> str:
    return datetime.fromtimestamp(t, NEW_YORK).strftime("%Y-%m-%d")


class Spotlight:
    """Everything Massive has on one stock, kept fresh within the call budget and saved across restarts."""

    def __init__(self, massive: Massive | None, path: str | Path | None = None, symbol: str = SYMBOL,
                 clock=time.time):
        self.massive = massive
        self.symbol = symbol
        self.path = Path(path) if path else None
        self._clock = clock
        self.prev: dict | None = None  # the last finished session's bar
        self.snapshot: dict | None = None  # paid plans only
        self.daily: list[dict] = []  # two years of daily bars, oldest first
        self.minutes: list[dict] = []  # the last finished session, minute by minute
        self.indicators: dict[str, dict] = {}
        self.details: dict | None = None
        self.news: list[dict] = []  # newest first
        self.dividends: list[dict] = []
        self.splits: list[dict] = []
        self.related: list[str] = []
        self.status: dict | None = None
        self.ran: dict[str, float] = {}  # job -> when it last ran (or was tried)
        self.ok_at: dict[str, float] = {}  # job -> when it last worked
        self.not_in_plan: dict[str, float] = {}  # job -> when Massive said the plan doesn't include it
        self.errors: dict[str, str] = {}
        self.fresh_news: list[dict] = []  # new stories not posted yet
        self._seen_news: set[str] = set()
        self._dirty = False
        self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.massive and self.massive.enabled)

    # ----- the refresh loop -----

    def due(self, market_open: bool) -> list[Job]:
        now = self._clock()
        out = []
        for job in JOBS:
            every = job.every_open if market_open else job.every_closed
            if job.name in self.not_in_plan:
                every = max(every, PLAN_RETRY)
            elif job.paid and job.name not in self.ok_at:
                every = max(every, PLAN_RETRY)  # untested: probe once, then rarely
            if now - self.ran.get(job.name, 0) >= every:
                out.append(job)
        return out

    async def step(self, market_open: bool) -> list[str]:
        """Runs the jobs that are due, in priority order, while the call budget lasts. Returns what ran."""
        if not self.enabled:
            return []
        done = []
        for job in self.due(market_open):
            try:
                await self._run(job.name)
            except MassiveError as exc:
                if exc.kind == "budget":
                    break  # the rest wait for the next step
                self.ran[job.name] = self._clock()
                if exc.kind == "plan":
                    self.not_in_plan[job.name] = self._clock()
                    self.errors.pop(job.name, None)
                else:
                    self.errors[job.name] = str(exc)[:160]
                if exc.kind in ("key", "rate"):
                    break
                continue
            except Exception as exc:  # a malformed answer: note it and move on
                log.warning("Massive %s failed", job.name, exc_info=True)
                self.ran[job.name] = self._clock()
                self.errors[job.name] = f"{type(exc).__name__}: {exc}"[:160]
                continue
            now = self._clock()
            self.ran[job.name] = self.ok_at[job.name] = now
            self.not_in_plan.pop(job.name, None)
            self.errors.pop(job.name, None)
            self._dirty = True
            done.append(job.name)
        if self._dirty:
            self.save()
        return done

    async def _run(self, name: str) -> None:
        m, sym = self.massive, self.symbol
        if name == "snapshot":
            d = await m.get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
            self.snapshot = d.get("ticker") or None
        elif name == "news":
            d = await m.get("/v2/reference/news", {"ticker": sym, "order": "desc", "sort": "published_utc",
                                                   "limit": "20"})
            self._add_news(d.get("results") or [])
        elif name == "prev":
            d = await m.get(f"/v2/aggs/ticker/{sym}/prev", {"adjusted": "true"})
            bar = (d.get("results") or [None])[0]
            if bar and (not self.prev or bar.get("t") != self.prev.get("t")):
                self.prev = bar
                for job in AFTER_NEW_SESSION:  # a new session closed: refresh what depends on it
                    self.ran.pop(job, None)
        elif name == "status":
            self.status = await m.get("/v1/marketstatus/now")
        elif name == "daily":
            # The free plan has two years of history, up to the last finished session (asking for today is refused).
            start = (datetime.fromtimestamp(self._clock(), timezone.utc) - timedelta(days=720)).strftime("%Y-%m-%d")
            d = await m.get(f"/v2/aggs/ticker/{sym}/range/1/day/{start}/{self.last_session()}",
                            {"adjusted": "true", "sort": "asc", "limit": "5000"})
            self.daily = [b for b in d.get("results") or [] if b.get("c")]
        elif name == "minutes":
            day = _date(self.prev["t"] / 1000) if self.prev and self.prev.get("t") else None
            if day is None:
                raise MassiveError("waiting for the previous session's bar", "other")
            d = await m.get(f"/v2/aggs/ticker/{sym}/range/1/minute/{day}/{day}",
                            {"adjusted": "true", "sort": "asc", "limit": "5000"})
            self.minutes = [b for b in d.get("results") or [] if b.get("c")]
        elif name in INDICATORS:
            kind, window = INDICATORS[name]
            params = {"timespan": "day", "series_type": "close", "order": "desc", "limit": "1", "adjusted": "true",
                      "timestamp.lte": self.last_session()}
            if window:
                params["window"] = str(window)
            d = await m.get(f"/v1/indicators/{kind}/{sym}", params)
            values = ((d.get("results") or {}).get("values")) or []
            if values:
                self.indicators[name] = values[0]
        elif name == "details":
            self.details = (await m.get(f"/v3/reference/tickers/{sym}")).get("results") or None
        elif name == "dividends":
            d = await m.get("/v3/reference/dividends", {"ticker": sym, "order": "desc", "limit": "8"})
            self.dividends = d.get("results") or []
        elif name == "splits":
            d = await m.get("/v3/reference/splits", {"ticker": sym, "order": "desc", "limit": "5"})
            self.splits = d.get("results") or []
        elif name == "related":
            d = await m.get(f"/v1/related-companies/{sym}")
            self.related = [r["ticker"] for r in d.get("results") or [] if r.get("ticker")][:10]
        else:
            raise ValueError(name)

    def _add_news(self, items: list[dict]) -> None:
        first_load = not self._seen_news
        fresh = []
        for item in items:
            key = item.get("id") or item.get("article_url")
            if not key or key in self._seen_news:
                continue
            self._seen_news.add(key)
            fresh.append(item)
        if not fresh:
            return
        self.news = sorted(fresh + self.news, key=lambda n: n.get("published_utc") or "", reverse=True)[:MAX_NEWS]
        if not first_load:  # on the first load these are old news, not alerts
            cutoff = self._clock() - 6 * 3600
            self.fresh_news += [n for n in fresh if _utc(n.get("published_utc")) >= cutoff]
        if len(self._seen_news) > 2000:
            self._seen_news = {n.get("id") or n.get("article_url") for n in self.news}

    def last_session(self) -> str:
        """The last finished session's date (New York): the previous bar's, else yesterday's."""
        if self.prev and self.prev.get("t"):
            return _date(self.prev["t"] / 1000)
        return _date(self._clock() - 86400)

    def take_fresh_news(self) -> list[dict]:
        out, self.fresh_news = self.fresh_news, []
        return sorted(out, key=lambda n: n.get("published_utc") or "")

    # ----- what the board shows -----

    def sentiment(self, item: dict) -> tuple[str, str]:
        """(positive/negative/neutral or "", the reasoning) for this stock in one story."""
        for ins in item.get("insights") or []:
            if ins.get("ticker") == self.symbol:
                return (ins.get("sentiment") or "").lower(), ins.get("sentiment_reasoning") or ""
        return "", ""

    def news_mood(self, hours: float = 48) -> tuple[int, int, int]:
        """(positive, neutral, negative) stories about the stock in the last `hours`."""
        cutoff = self._clock() - hours * 3600
        counts = {"positive": 0, "neutral": 0, "negative": 0}
        for n in self.news:
            if _utc(n.get("published_utc")) >= cutoff:
                s, _ = self.sentiment(n)
                if s in counts:
                    counts[s] += 1
        return counts["positive"], counts["neutral"], counts["negative"]

    def range_52w(self) -> tuple[float, float] | None:
        bars = self.daily[-252:]
        if not bars:
            return None
        return min(b["l"] for b in bars), max(b["h"] for b in bars)

    def avg_volume(self, days: int = 50) -> float | None:
        vols = [b.get("v") or 0 for b in self.daily[-days:]]
        return sum(vols) / len(vols) if vols else None

    def plan(self) -> str:
        if not self.massive or not self.massive.key:
            return "no key"
        if self.massive.key_rejected:
            return "key rejected"
        if "snapshot" in self.ok_at:
            return "paid plan (delayed snapshot available)"
        return "free plan (end of day)" if "snapshot" in self.not_in_plan else "plan being checked"

    # ----- saving -----

    def state(self) -> dict:
        return {"symbol": self.symbol, "prev": self.prev, "snapshot": self.snapshot, "daily": self.daily,
                "minutes": self.minutes, "indicators": self.indicators, "details": self.details, "news": self.news,
                "dividends": self.dividends, "splits": self.splits, "related": self.related, "status": self.status,
                "ran": self.ran, "ok_at": self.ok_at, "not_in_plan": self.not_in_plan,
                "seen": sorted(self._seen_news)[-2000:]}

    def save(self) -> None:
        self._dirty = False
        if not self.path:
            return
        try:
            write_json(self.path, self.state())
        except OSError:
            log.warning("Couldn't save the Massive data", exc_info=True)

    def _load(self) -> None:
        if not self.path:
            return
        d = read_json(self.path, {})
        if not isinstance(d, dict) or d.get("symbol") != self.symbol:
            return
        for key in ("prev", "snapshot", "details", "status"):
            setattr(self, key, d.get(key) if isinstance(d.get(key), dict) else None)
        for key in ("daily", "minutes", "news", "dividends", "splits", "related"):
            setattr(self, key, d.get(key) if isinstance(d.get(key), list) else [])
        for key in ("indicators", "ran", "ok_at", "not_in_plan"):
            setattr(self, key, d.get(key) if isinstance(d.get(key), dict) else {})
        # The key or plan may have changed since: check paid endpoints again soon.
        self.not_in_plan = {k: v for k, v in self.not_in_plan.items() if k != "snapshot"}
        self.ran.pop("snapshot", None)
        self._seen_news = set(d.get("seen") or [])


def _utc(text: str | None) -> float:
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
