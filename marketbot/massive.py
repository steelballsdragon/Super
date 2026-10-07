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
import heapq
import logging
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hours import NEW_YORK
from .http import Http, HttpError, _quiet
from .storage import read_json, write_json

log = logging.getLogger(__name__)

BASE = "https://api.massive.com"
SOURCE = "Massive"
CALLS_PER_MINUTE = 5
WINDOW = 61.0  # a second of slack on Massive's minute
# Only these names are read as the key, plus other MASSIVE… names below. Polygon-named variables count only under their
# exact API-key names: crypto setups keep wallet and explorer secrets under POLYGON_… names, and whatever is found here
# is sent to Massive.
KEY_NAMES = ("MASSIVE_API_KEY", "MASSIVE_KEY", "MASSIVE_API", "MASSIVE_TOKEN", "MASSIVE", "POLYGON_API_KEY",
             "POLYGON_IO_API_KEY", "POLYGONIO_API_KEY")
NOT_A_KEY = re.compile(r"PRIVATE|SECRET|MNEMONIC|SEED|WALLET|RPC|SCAN|PASSWORD|PASSPHRASE", re.I)
SYMBOL = "NVDA"
_KEYLIKE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def find_key(env=os.environ) -> tuple[str | None, str | None]:
    """(key, the variable it came from): one of KEY_NAMES, else another variable with MASSIVE in its name (e.g.
    MASSIVE_APIKEY) holding something shaped like a key."""
    for name in KEY_NAMES:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    for name in sorted(env):
        value = (env.get(name) or "").strip()
        if "MASSIVE" in name.upper() and not NOT_A_KEY.search(name) and _KEYLIKE.match(value):
            return value, name
    return None, None


class RateLimiter:
    """At most `calls` requests in any `window` seconds (a sliding window, so there are no bursts at a minute's
    edge). Waiters are served in order. A request's slot is stamped again when it finishes, so however late a slow
    request reaches the server, the next ones still keep a full window clear of it."""

    def __init__(self, calls: int = CALLS_PER_MINUTE, window: float = WINDOW, clock=time.monotonic,
                 sleep=asyncio.sleep, wall=time.time):
        self.calls = calls
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._sent: list[list[float]] = []  # one [time] per slot in use
        self._blocked_until = 0.0
        self._queue: deque[object] = deque()  # acquire() callers, first come first served
        self.total = 0

    def _trim(self, now: float) -> None:
        self._sent = [s for s in self._sent if now - s[0] < self.window]

    def used(self) -> int:
        """Requests in the last window."""
        self._trim(self._clock())
        return len(self._sent)

    def wait_time(self) -> float:
        now = self._clock()
        self._trim(now)
        wait = max(self._blocked_until - now, 0.0)
        if len(self._sent) >= self.calls:
            wait = max(wait, min(s[0] for s in self._sent) + self.window - now)
        return wait

    def _turn_at(self, position: int) -> float:
        """When the caller `position` places back in the queue can expect its slot."""
        now = self._clock()
        self._trim(now)
        free = [s[0] + self.window for s in self._sent] + [now] * max(self.calls - len(self._sent), 0)
        heapq.heapify(free)
        t = now
        for _ in range(position + 1):
            t = max(heapq.heappop(free), self._blocked_until, now)
            heapq.heappush(free, t + self.window)
        return t

    def _take(self) -> list[float]:
        slot = [self._clock()]
        self._sent.append(slot)
        self.total += 1
        return slot

    def try_slot(self) -> list[float] | None:
        """A slot now, or None (never waits)."""
        return None if self.wait_time() > 0 else self._take()

    def try_acquire(self) -> bool:
        return self.try_slot() is not None

    async def acquire_slot(self, timeout: float | None = None) -> list[float] | None:
        """A slot, waiting in line up to `timeout` seconds (forever if None); None at once if it would take longer."""
        deadline = None if timeout is None else self._clock() + timeout
        me = object()
        self._queue.append(me)
        try:
            while True:
                head = self._queue[0] is me
                if head and self.wait_time() <= 0:
                    return self._take()
                now = self._clock()
                turn = self._turn_at(self._queue.index(me))
                if deadline is not None and turn > deadline:
                    return None
                await self._sleep(max(turn - now, 0.0 if head else 0.05))
        finally:
            self._queue.remove(me)

    async def acquire(self, timeout: float | None = None) -> bool:
        return await self.acquire_slot(timeout) is not None

    def finish(self, slot: list[float]) -> None:
        """The request using `slot` is done: count its window from now."""
        slot[0] = max(slot[0], self._clock())

    def penalize(self, seconds: float = WINDOW) -> None:
        """Massive said we went over: send nothing for a while."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    def recent(self) -> list[float]:
        """Wall-clock times of the slots still counting, to carry the budget across a restart."""
        now, wall = self._clock(), self._wall()
        self._trim(now)
        return sorted(wall - (now - s[0]) for s in self._sent)

    def restore(self, walls) -> None:
        """Counts requests a previous run made (wall-clock times) against the current window."""
        now, wall = self._clock(), self._wall()
        for w in walls or []:
            if isinstance(w, (int, float)) and not isinstance(w, bool) and 0 <= wall - w < self.window:
                self._sent.append([now - (wall - w)])
        self._sent = self._sent[-self.calls:] if len(self._sent) > self.calls else self._sent


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

    def _scrub(self, text) -> str:
        """Server or network error text without the key (in case anything echoes it)."""
        text = _quiet(str(text or ""))
        return text.replace(self.key, "…") if self.key else text

    async def get(self, path: str, params: dict | None = None, wait: float = 0.0) -> dict:
        """One request (one slot of the budget). Returns the JSON object; raises MassiveError."""
        if not self.enabled:
            raise MassiveError("no usable Massive key", "key")
        slot = self.limiter.try_slot() if wait <= 0 else await self.limiter.acquire_slot(wait)
        if slot is None:
            raise MassiveError("the 5-a-minute budget is used up for now", "budget")
        try:
            # The key goes in a header, so it never shows up in a logged URL.
            resp = await self.http.get(f"{BASE}{path}", params=params, source=SOURCE, retries=0, force=True,
                                       headers={"Authorization": f"Bearer {self.key}"}, answered=(403,))
        except HttpError as exc:
            if exc.status == 429:
                self.limiter.penalize()
                self.last_error = "rate limited by Massive (backing off a minute)"
                raise MassiveError(self.last_error, "rate", 429) from None
            self.last_error = self._scrub(exc)[:160]
            raise MassiveError(self.last_error, "network", exc.status) from None
        finally:
            self.limiter.finish(slot)
        try:
            data = resp.json()
        except HttpError:
            data = None
        info = data if isinstance(data, dict) else {}
        said = self._scrub(info.get("error") or info.get("message") or "")
        if resp.status == 401:
            self.key_rejected = True
            self.last_error = f"Massive rejected the key ({said or 'HTTP 401'})"[:160]
            raise MassiveError(self.last_error, "key", 401)
        if resp.status == 403:
            raise MassiveError(said or "not in this Massive plan", "plan", 403)
        if resp.status >= 400:
            self.last_error = f"HTTP {resp.status}: {said}"[:160]
            raise MassiveError(self.last_error, "other", resp.status)
        if not isinstance(data, dict):
            self.last_error = f"Massive sent an unreadable answer (HTTP {resp.status})"
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
RETRY_FIRST, RETRY_MAX = 120.0, 1800.0  # after a timeout, server error or garbled answer: 2 minutes, doubling
SNAPSHOT_FRESH = 1800.0  # a delayed snapshot older than this isn't shown
AFTER_NEW_SESSION = ("daily", "minutes", *INDICATORS)  # refreshed as soon as a new session's bar is out
MAX_NEWS = 40


def _date(t: float) -> str:
    return datetime.fromtimestamp(t, NEW_YORK).strftime("%Y-%m-%d")


# ----- checking what Massive sent (and what was saved) before keeping it -----

def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _bar(b) -> dict | None:
    """A price bar with numbers where they must be, else None."""
    if not isinstance(b, dict) or any(_num(b.get(k)) is None for k in ("o", "h", "l", "c", "t")):
        return None
    out = {k: _num(b[k]) for k in ("o", "h", "l", "c", "t")}
    for k in ("v", "vw", "n"):
        if _num(b.get(k)) is not None:
            out[k] = _num(b[k])
    return out


def _bars(rows) -> list[dict]:
    if not isinstance(rows, list):
        raise MassiveError("unexpected answer (no bars)", "other")
    return [b for b in (_bar(r) for r in rows) if b]


def _dicts(rows, need: tuple[str, ...] = (), numbers: tuple[str, ...] = ()) -> list[dict]:
    """The dict items that have the `need` keys as text and the `numbers` keys as numbers."""
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and all(isinstance(r.get(k), str) for k in need)
            and all(_num(r.get(k)) is not None for k in numbers)]


def _indicator(v) -> dict | None:
    if not isinstance(v, dict) or _num(v.get("value")) is None:
        return None
    return {k: _num(v[k]) for k in ("value", "signal", "histogram", "timestamp") if _num(v.get(k)) is not None}


def _news(rows) -> list[dict]:
    out = []
    for n in _dicts(rows, need=("title",)):
        if not isinstance(n.get("article_url"), str):
            n = {**n, "article_url": ""}
        if not isinstance(n.get("published_utc"), str):
            n = {**n, "published_utc": ""}
        if not isinstance(n.get("insights"), list):
            n = {**n, "insights": []}
        n["insights"] = [i for i in n["insights"] if isinstance(i, dict)]
        out.append(n)
    return out


def _times(d) -> dict[str, float]:
    return {k: float(v) for k, v in d.items() if isinstance(k, str) and _num(v) is not None} if isinstance(d, dict) else {}


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
        self.retry_at: dict[str, float] = {}  # job -> when to try again after a passing failure
        self._fails: dict[str, int] = {}
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
            if now < self.retry_at.get(job.name, 0):
                continue
            every = job.every_open if market_open else job.every_closed
            if job.name in self.not_in_plan:
                every = max(every, PLAN_RETRY)
            if now - self.ran.get(job.name, 0) >= every:
                out.append(job)
        return out

    async def step(self, market_open: bool) -> list[str]:
        """Runs the jobs that are due, in priority order, while the call budget lasts. Returns what ran."""
        if not self.enabled:
            return []
        done = []
        sent = self.massive.limiter.total
        for job in self.due(market_open):
            try:
                await self._run(job.name)
            except MassiveError as exc:
                if exc.kind in ("budget", "rate", "key"):
                    break  # the rest wait for the next step (after a 429 the limiter pauses for a minute)
                now = self._clock()
                if exc.kind == "plan":
                    self.ran[job.name] = self.not_in_plan[job.name] = now
                    self.ok_at.pop(job.name, None)
                    self.errors.pop(job.name, None)
                    if job.name == "snapshot":
                        self.snapshot = None  # e.g. a paid plan ended: its old price mustn't linger
                    self._dirty = True
                elif exc.kind == "network" or exc.status is None or exc.status >= 500 or exc.status < 400:
                    # Timeouts, server errors, garbled answers: try again soon, not after the job's whole interval.
                    fails = self._fails[job.name] = self._fails.get(job.name, 0) + 1
                    self.retry_at[job.name] = now + min(RETRY_FIRST * 2 ** (fails - 1), RETRY_MAX)
                    self.errors[job.name] = str(exc)[:160]
                else:  # a 4xx that asking again won't fix
                    self.ran[job.name] = now
                    self.errors[job.name] = str(exc)[:160]
                continue
            except Exception as exc:  # a bug in reading an answer: note it, and don't retry before the interval
                log.warning("Massive %s failed", job.name, exc_info=True)
                self.ran[job.name] = self._clock()
                self.errors[job.name] = f"{type(exc).__name__}: {exc}"[:160]
                continue
            now = self._clock()
            self.ran[job.name] = self.ok_at[job.name] = now
            for d in (self.not_in_plan, self.errors, self.retry_at, self._fails):
                d.pop(job.name, None)
            self._dirty = True
            done.append(job.name)
        if self._dirty or self.massive.limiter.total != sent:
            self.save()  # also when only failures happened: the requests made count against a restart's budget
        return done

    async def _run(self, name: str) -> None:
        m, sym = self.massive, self.symbol
        if name == "snapshot":
            d = await m.get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
            ticker = d.get("ticker")
            if not isinstance(ticker, dict):
                raise MassiveError("unexpected snapshot answer", "other")
            self.snapshot = ticker
        elif name == "news":
            d = await m.get("/v2/reference/news", {"ticker": sym, "order": "desc", "sort": "published_utc",
                                                   "limit": "20"})
            if not isinstance(d.get("results", []), list):
                raise MassiveError("unexpected news answer", "other")
            self._add_news(_news(d.get("results") or []))
        elif name == "prev":
            d = await m.get(f"/v2/aggs/ticker/{sym}/prev", {"adjusted": "true"})
            results = d.get("results") or []
            bar = _bar(results[0]) if isinstance(results, list) and results else None
            if bar is None:
                raise MassiveError("no previous-day bar in the answer", "other")
            if not self.prev or bar.get("t") != self.prev.get("t"):
                self.prev = bar
                for job in AFTER_NEW_SESSION:  # a new session closed: refresh what depends on it
                    self.ran.pop(job, None)
        elif name == "status":
            self.status = await m.get("/v1/marketstatus/now")  # Massive.get only returns JSON objects
        elif name == "daily":
            # The free plan has two years of history, up to the last finished session (asking for today is refused).
            start = (datetime.fromtimestamp(self._clock(), timezone.utc) - timedelta(days=720)).strftime("%Y-%m-%d")
            d = await m.get(f"/v2/aggs/ticker/{sym}/range/1/day/{start}/{self.last_session()}",
                            {"adjusted": "true", "sort": "asc", "limit": "5000"})
            bars = _bars(d.get("results", []))
            if not bars:
                raise MassiveError("no daily bars in the answer", "other")  # keep the ones we have
            self.daily = bars
        elif name == "minutes":
            day = _date(self.prev["t"] / 1000) if self.prev and self.prev.get("t") else None
            if day is None:
                raise MassiveError("waiting for the previous session's bar", "other")
            d = await m.get(f"/v2/aggs/ticker/{sym}/range/1/minute/{day}/{day}",
                            {"adjusted": "true", "sort": "asc", "limit": "5000"})
            self.minutes = _bars(d.get("results", []))
        elif name in INDICATORS:
            kind, window = INDICATORS[name]
            params = {"timespan": "day", "series_type": "close", "order": "desc", "limit": "1", "adjusted": "true",
                      "timestamp.lte": self.last_session()}
            if window:
                params["window"] = str(window)
            d = await m.get(f"/v1/indicators/{kind}/{sym}", params)
            results = d.get("results")
            values = results.get("values") if isinstance(results, dict) else None
            value = _indicator(values[0]) if isinstance(values, list) and values else None
            if value is None:
                raise MassiveError(f"no {name} value in the answer", "other")
            self.indicators[name] = value
        elif name == "details":
            details = (await m.get(f"/v3/reference/tickers/{sym}")).get("results")
            if not isinstance(details, dict):
                raise MassiveError("unexpected company details answer", "other")
            self.details = details
        elif name == "dividends":
            d = await m.get("/v3/reference/dividends", {"ticker": sym, "order": "desc", "limit": "8"})
            if not isinstance(d.get("results", []), list):
                raise MassiveError("unexpected dividends answer", "other")
            self.dividends = _dicts(d.get("results") or [], numbers=("cash_amount",))
        elif name == "splits":
            d = await m.get("/v3/reference/splits", {"ticker": sym, "order": "desc", "limit": "5"})
            if not isinstance(d.get("results", []), list):
                raise MassiveError("unexpected splits answer", "other")
            self.splits = _dicts(d.get("results") or [], numbers=("split_from", "split_to"))
        elif name == "related":
            d = await m.get(f"/v1/related-companies/{sym}")
            if not isinstance(d.get("results", []), list):
                raise MassiveError("unexpected related companies answer", "other")
            self.related = [r["ticker"] for r in _dicts(d.get("results") or [], need=("ticker",))][:10]
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
        """The last finished session's date (New York): the previous bar's, else yesterday's (by the calendar, so
        the 25-hour day when clocks go back is no exception)."""
        if self.prev and _num(self.prev.get("t")):
            return _date(self.prev["t"] / 1000)
        today = datetime.fromtimestamp(self._clock(), NEW_YORK).date()
        return (today - timedelta(days=1)).isoformat()

    def snapshot_fresh(self) -> bool:
        """A delayed snapshot worth showing: Massive is usable and it came in the last half hour."""
        return bool(self.enabled and self.snapshot and "snapshot" not in self.not_in_plan
                    and self._clock() - self.ok_at.get("snapshot", 0) < SNAPSHOT_FRESH)

    def take_fresh_news(self) -> list[dict]:
        out, self.fresh_news = self.fresh_news, []
        return sorted(out, key=lambda n: n.get("published_utc") or "")

    # ----- what the board shows -----

    def sentiment(self, item: dict) -> tuple[str, str]:
        """(positive/negative/neutral or "", the reasoning) for this stock in one story."""
        for ins in item.get("insights") or []:
            if isinstance(ins, dict) and ins.get("ticker") == self.symbol:
                mood, why = ins.get("sentiment"), ins.get("sentiment_reasoning")
                return (mood.lower() if isinstance(mood, str) else ""), (why if isinstance(why, str) else "")
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
        if "snapshot" in self.not_in_plan:
            return "free plan (end of day)"
        if "snapshot" in self.ok_at:
            return "paid plan (delayed snapshot available)"
        return "plan being checked"

    # ----- saving -----

    def state(self) -> dict:
        return {"symbol": self.symbol, "prev": self.prev, "snapshot": self.snapshot, "daily": self.daily,
                "minutes": self.minutes, "indicators": self.indicators, "details": self.details, "news": self.news,
                "dividends": self.dividends, "splits": self.splits, "related": self.related, "status": self.status,
                "ran": self.ran, "ok_at": self.ok_at, "not_in_plan": self.not_in_plan,
                "seen": sorted(self._seen_news)[-2000:],
                "recent_requests": self.massive.limiter.recent() if self.massive else []}

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
        # Everything is checked as if Massive had just sent it: a damaged file mustn't stop the bot.
        self.prev = _bar(d.get("prev"))
        self.snapshot = d.get("snapshot") if isinstance(d.get("snapshot"), dict) else None
        self.details = d.get("details") if isinstance(d.get("details"), dict) else None
        self.status = d.get("status") if isinstance(d.get("status"), dict) else None
        self.daily = [b for b in map(_bar, d.get("daily") or []) if b] if isinstance(d.get("daily"), list) else []
        self.minutes = ([b for b in map(_bar, d.get("minutes") or []) if b]
                        if isinstance(d.get("minutes"), list) else [])
        self.news = _news(d.get("news"))
        self.dividends = _dicts(d.get("dividends"), numbers=("cash_amount",))
        self.splits = _dicts(d.get("splits"), numbers=("split_from", "split_to"))
        self.related = [r for r in d.get("related") or [] if isinstance(r, str)] if isinstance(d.get("related"), list) else []
        indicators = d.get("indicators") if isinstance(d.get("indicators"), dict) else {}
        self.indicators = {k: v for k, v in ((k, _indicator(v)) for k, v in indicators.items()) if v and k in INDICATORS}
        self.ran, self.ok_at, self.not_in_plan = (_times(d.get(k)) for k in ("ran", "ok_at", "not_in_plan"))
        seen = d.get("seen")
        self._seen_news = {x for x in seen if isinstance(x, str)} if isinstance(seen, list) else set()
        if self.massive and isinstance(d.get("recent_requests"), list):
            self.massive.limiter.restore(d["recent_requests"])  # what the last run sent still counts


def _utc(text: str | None) -> float:
    if not text or not isinstance(text, str):
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
