"""Offline tests for marketbot/massive.py (the 5-a-minute limiter, the Massive client and the NVIDIA spotlight) and the
NVIDIA parts of the bot and the embeds.

Nothing here touches the network: a fake Massive server (every documented path, realistic JSON) sits behind the real
Http class, and every clock is fake, so hours of the refresh loop run in well under a second.
"""

import asyncio
import dataclasses
import heapq
import json
import math
import random
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

pytest.importorskip("numpy")
pytest.importorskip("discord")

from marketbot import bot as botmod, embeds as E  # noqa: E402
from marketbot.ai import NewsAI  # noqa: E402
from marketbot.bot import NVIDIA_NEWS_PER_STEP, NVIDIA_STEPS, STALE_QUOTE, MarketBot  # noqa: E402
from marketbot.hours import NEW_YORK, is_trading_day, market_open  # noqa: E402
from marketbot.http import TIMEOUT, Http, Response  # noqa: E402
from marketbot.massive import (AFTER_NEW_SESSION, BASE, INDICATORS, JOBS, KEY_NAMES, MAX_NEWS, PLAN_RETRY,  # noqa: E402
                               RETRY_FIRST, RETRY_MAX, SNAPSHOT_FRESH, SOURCE, WINDOW, Massive, MassiveError,
                               RateLimiter, Spotlight, find_key)
from tests.market_helpers import quote  # noqa: E402

KEY = "mk_S3cretMassiveKey_0123456789abcdef"
JOB_NAMES = [j.name for j in JOBS]
HOUR = 3600
DAY = 86400


# ----- fake time -----

class Clock:
    """A settable clock. Its sleep() moves time forward at once and yields (a sleeper never wakes early)."""

    def __init__(self, t: float = 1000.0):
        self.t = float(t)
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += max(seconds, 0.0)
        await asyncio.sleep(0)

    async def run(self, *aws):
        return list(await asyncio.gather(*aws))


class VirtualTime(Clock):
    """Event-driven fake time: sleep() parks the caller until run() has moved the clock to its wake-up time, which
    happens only once every task is blocked. Concurrent sleepers wake in time order, like real ones."""

    def __init__(self, t: float = 1000.0):
        super().__init__(t)
        self._queue: list = []
        self._n = 0

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        fut = asyncio.get_running_loop().create_future()
        self._n += 1
        heapq.heappush(self._queue, (self.t + max(seconds, 0.0), self._n, fut))
        await fut

    async def run(self, *aws, settle: int = 25):
        tasks = [asyncio.ensure_future(a) for a in aws]
        while True:
            for _ in range(settle):
                await asyncio.sleep(0)
            if all(t.done() for t in tasks):
                return [t.result() for t in tasks]
            for _ in range(2000):
                while self._queue and self._queue[0][2].done():  # cancelled sleepers
                    heapq.heappop(self._queue)
                if self._queue or all(t.done() for t in tasks):
                    break
                await asyncio.sleep(0)
            if all(t.done() for t in tasks):
                return [t.result() for t in tasks]
            assert self._queue, "every task is blocked and nobody is asleep"
            wake, _, fut = heapq.heappop(self._queue)
            self.t = max(self.t, wake)
            fut.set_result(None)


def ny(*args) -> float:
    return datetime(*args, tzinfo=NEW_YORK).timestamp()


def assert_budget(times, calls: int = 5, window: float = WINDOW) -> None:
    """No `window`-second span holds more than `calls` of these moments."""
    times = sorted(times)
    for i in range(len(times) - calls):
        assert times[i + calls] - times[i] >= window, f"{calls + 1} requests within {window}s: {times[i:i + calls + 1]}"


async def no_retry_sleep(seconds):
    raise AssertionError("Massive requests must never be retried (Http slept before a retry)")


# ----- a scripted backend for the client tests -----

def reply(status: int = 200, body=None, url: str = "") -> Response:
    if isinstance(body, str):
        return Response(status, body, url)
    return Response(status, json.dumps({"status": "OK", "request_id": "r1"} if body is None else body), url)


class Backend:
    """Answers each request with the next scripted answer (the last one repeats); exceptions are raised."""

    name = "scripted"

    def __init__(self, *answers):
        self.answers = list(answers) or [reply()]
        self.calls: list = []

    async def get(self, url, headers, timeout, proxy=None):
        self.calls.append(SimpleNamespace(url=url, headers=dict(headers), timeout=timeout, proxy=proxy))
        await asyncio.sleep(0)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def close(self):
        pass


def make_massive(*answers, key=KEY, clock=None, calls=5):
    backend = Backend(*answers)
    clock = clock or Clock(1000.0)
    http = Http(backend, sleep=no_retry_sleep)
    return Massive(key, http, RateLimiter(calls=calls, clock=clock, sleep=clock.sleep, wall=clock)), backend, clock


def no_key_anywhere(m: Massive, exc: BaseException | None = None) -> None:
    texts = [m.last_error or ""]
    h = m.http.health.get(SOURCE)
    if h:
        texts += [h.line(), h.last_error or ""]
    while exc is not None:
        texts += [str(exc), repr(exc)]
        exc = exc.__cause__ or exc.__context__
    for text in texts:
        assert KEY not in text, text


# ----- a fake Massive server -----

NOT_ENTITLED = {"status": "NOT_AUTHORIZED", "request_id": "6a7e466379af0a71039d60cc78e72282",
                "message": "You are not entitled to this data. Please upgrade your plan at https://massive.com/pricing"}
TODAY_REFUSED = {"status": "NOT_AUTHORIZED", "request_id": "3b1c2b1ba4bd4ba3d1f0de6b1b8c5f17",
                 "message": "Attempted to request today's data before end of day. Please upgrade your plan at "
                          "https://massive.com/pricing"}
RELATED = ["AMD", "AVGO", "INTC", "QCOM", "TSM", "MU", "ARM", "MSFT", "GOOGL", "META", "AMZN", "AAPL"]
_DATE = r"(\d{4}-\d{2}-\d{2})"
ROUTES = [
    ("snapshot", re.compile(r"^/v2/snapshot/locale/us/markets/stocks/tickers/([A-Z.]+)$")),
    ("news", re.compile(r"^/v2/reference/news$")),
    ("prev", re.compile(r"^/v2/aggs/ticker/([A-Z.]+)/prev$")),
    ("status", re.compile(r"^/v1/marketstatus/now$")),
    ("daily", re.compile(rf"^/v2/aggs/ticker/([A-Z.]+)/range/1/day/{_DATE}/{_DATE}$")),
    ("minutes", re.compile(rf"^/v2/aggs/ticker/([A-Z.]+)/range/1/minute/{_DATE}/{_DATE}$")),
    ("indicator", re.compile(r"^/v1/indicators/(sma|ema|rsi|macd)/([A-Z.]+)$")),
    ("details", re.compile(r"^/v3/reference/tickers/([A-Z.]+)$")),
    ("dividends", re.compile(r"^/v3/reference/dividends$")),
    ("splits", re.compile(r"^/v3/reference/splits$")),
    ("related", re.compile(r"^/v1/related-companies/([A-Z.]+)$")),
]


def ms(t: float) -> int:
    return int(round(t * 1000))


def session_ms(d: date) -> int:
    """Massive stamps a daily bar with midnight New York time of its session."""
    return ms(datetime(d.year, d.month, d.day, tzinfo=NEW_YORK).timestamp())


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def published(item: dict) -> float:
    return datetime.fromisoformat(item["published_utc"].replace("Z", "+00:00")).timestamp()


def trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def day_bar(d: date) -> dict:
    k = d.toordinal()
    c = round(150 + 25 * math.sin(k / 23) + k % 5, 2)
    o = round(c * (1 + 0.004 * (k % 3 - 1)), 2)
    h, low = round(max(o, c) * 1.012, 2), round(min(o, c) * 0.988, 2)
    return {"v": 150_000_000 + (k % 11) * 10_000_000, "vw": round((h + low + c) / 3, 4), "o": o, "c": c, "h": h,
            "l": low, "t": session_ms(d), "n": 1_500_000 + k % 1000}


def minute_bars(d: date) -> list[dict]:
    base = day_bar(d)
    start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NEW_YORK).timestamp()
    out = []
    for i in range(390):
        c = round(base["o"] + (base["c"] - base["o"]) * i / 389 + 0.3 * math.sin(i / 7), 2)
        out.append({"v": 300_000 + 50 * i, "vw": c, "o": c, "c": c, "h": round(c + 0.05, 2), "l": round(c - 0.05, 2),
                    "t": ms(start + 60 * i), "n": 2000 + i})
    return out


def story(i: int, at: float, mood: str = "positive", **extra) -> dict:
    s = {"id": f"nvda-story-{i:04d}",
         "publisher": {"name": "Benzinga", "homepage_url": "https://www.benzinga.com/",
                       "logo_url": "https://s3.massive.com/public/assets/news/logos/benzinga.svg",
                       "favicon_url": "https://s3.massive.com/public/assets/news/favicons/benzinga.ico"},
         "title": f"Nvidia headline number {i}", "author": "Benzinga Newsdesk", "published_utc": iso(at),
         "article_url": f"https://www.benzinga.com/news/nvda/{i}", "tickers": ["NVDA", "AMD", "TSM"],
         "image_url": f"https://cdn.benzinga.com/files/images/story/{i}.jpeg",
         "description": f"Story {i}: Nvidia's data-center business and what analysts expect next.",
         "keywords": ["AI", "semiconductors"],
         "insights": [{"ticker": "AMD", "sentiment": "negative", "sentiment_reasoning": "Losing share."},
                      {"ticker": "NVDA", "sentiment": mood,
                       "sentiment_reasoning": f"Story {i} reads {mood} for NVDA."}]}
    s.update(extra)
    return s


def once(answer):
    """A server hook that answers `answer` the first time, then normally."""
    used = []

    def hook(call):
        if used:
            return None
        used.append(call)
        return answer
    return hook


class FakeMassive:
    """Massive's REST API as the free plan (or a paid one) answers it, on a fake clock. Every request is recorded
    with the server's view of the day: `today` and `last_session` (New York)."""

    name = "fake massive"

    def __init__(self, clock, key: str = KEY, paid: bool = False):
        self.clock, self.key, self.paid = clock, key, paid
        self.calls: list = []
        self.stories: list[tuple[float, dict]] = []  # (visible from, story)
        self.hooks: dict = {}  # job -> Response | exception | callable(call) -> one of those or None
        self.prev_bar = None  # None: the last session's bar; a date: that session's; "none": an empty answer
        self.chaos = None  # callable(call) -> a failure or None

    def add(self, item: dict, visible_at: float | None = None) -> None:
        self.stories.append((published(item) if visible_at is None else visible_at, item))

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), NEW_YORK).date()

    def last_session(self) -> date:
        d = self.today() - timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def of(self, name: str) -> list:
        return [c for c in self.calls if c.name == name]

    async def get(self, url, headers, timeout, proxy=None):
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query))
        name, match = None, None
        for route_name, pattern in ROUTES:
            match = pattern.match(parts.path)
            if match:
                name = route_name
                break
        if name == "indicator":
            kind = match.group(1)
            name = next((n for n, (k, w) in INDICATORS.items()
                         if k == kind and (not w or params.get("window") == str(w))), f"{kind}?")
        call = SimpleNamespace(t=self.clock(), url=url, base=f"{parts.scheme}://{parts.netloc}", path=parts.path,
                               params=params, headers=dict(headers), name=name, match=match, today=self.today(),
                               last_session=self.last_session(), timeout=timeout)
        self.calls.append(call)
        await asyncio.sleep(0)
        for hook in (self.chaos, self.hooks.get(name)):
            answer = hook(call) if callable(hook) and not isinstance(hook, (Response, BaseException)) else hook
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, Response):
                return answer
        if headers.get("Authorization") != f"Bearer {self.key}":
            return reply(401, {"status": "ERROR", "request_id": "r401", "error": "Unknown API Key"})
        if name is None:
            return reply(404, {"status": "NOT_FOUND", "request_id": "r404", "message": "Not found"})
        status, body = self._indicator(call) if name in INDICATORS else getattr(self, f"_{name}")(call)
        return reply(status, body, url)

    async def close(self):
        pass

    # the endpoints

    def _snapshot(self, call):
        if not self.paid:
            return 403, NOT_ENTITLED
        last = day_bar(call.last_session)
        price = round(last["c"] * 1.0123, 2)
        ns = ms(call.t) * 1_000_000
        day = {"o": last["c"], "h": price + 1, "l": last["c"] - 1, "c": price, "v": 98_000_000, "vw": price - 0.2}
        return 200, {"status": "OK", "request_id": "rs", "ticker": {
            "ticker": "NVDA", "todaysChangePerc": round((price / last["c"] - 1) * 100, 4),
            "todaysChange": round(price - last["c"], 4), "updated": ns, "day": day, "min": dict(day, t=ms(call.t)),
            "prevDay": {k: last[k] for k in ("o", "h", "l", "c", "v", "vw")},
            "lastTrade": {"c": [14, 41], "i": "71675577320245", "p": price, "s": 100, "t": ns - 900 * 10 ** 9, "x": 4},
            "lastQuote": {"P": price + 0.01, "S": 2, "p": price - 0.01, "s": 3, "t": ns - 900 * 10 ** 9}}}

    def _news(self, call):
        limit = int(call.params.get("limit", 10))
        visible = [s for at, s in self.stories if at <= call.t]
        visible.sort(key=lambda s: s["published_utc"], reverse=True)
        visible = visible[:limit]
        return 200, {"results": visible, "status": "OK", "request_id": "rn", "count": len(visible),
                     "next_url": "https://api.massive.com/v2/reference/news?cursor=YXA9MjAyNi0xMC0wNlQxMg"}

    def _prev(self, call):
        if self.prev_bar == "none":
            return 200, {"ticker": "NVDA", "queryCount": 0, "resultsCount": 0, "adjusted": True, "status": "OK",
                         "request_id": "rp"}
        bar = dict(T="NVDA", **day_bar(self.prev_bar or call.last_session))
        return 200, {"ticker": "NVDA", "queryCount": 1, "resultsCount": 1, "adjusted": True, "results": [bar],
                     "status": "OK", "request_id": "rp", "count": 1}

    def _status(self, call):
        state = "open" if market_open(datetime.fromtimestamp(call.t, NEW_YORK)) else "closed"
        return 200, {"afterHours": False, "currencies": {"crypto": "open", "fx": "open"}, "earlyHours": False,
                     "exchanges": {"nasdaq": state, "nyse": state, "otc": state},
                     "indicesGroups": {"s_and_p": state, "societe_generale": state, "msci": state},
                     "market": state, "serverTime": datetime.fromtimestamp(call.t, NEW_YORK).isoformat()}

    def _range(self, call, minutes: bool):
        start, end = date.fromisoformat(call.match.group(2)), date.fromisoformat(call.match.group(3))
        if end >= call.today and not self.paid:
            return 403, TODAY_REFUSED
        start = max(start, call.today - timedelta(days=730))  # the free plan's two years
        days = trading_days(start, min(end, call.last_session))
        bars = [b for d in days for b in minute_bars(d)] if minutes else [day_bar(d) for d in days]
        return 200, {"ticker": "NVDA", "queryCount": len(bars), "resultsCount": len(bars), "adjusted": True,
                     "results": bars, "status": "OK", "request_id": "ra", "count": len(bars)}

    def _daily(self, call):
        return self._range(call, minutes=False)

    def _minutes(self, call):
        return self._range(call, minutes=True)

    def _indicator(self, call):
        lte = date.fromisoformat(call.params["timestamp.lte"])
        if lte >= call.today and not self.paid:
            return 403, TODAY_REFUSED
        d = trading_days(lte - timedelta(days=10), lte)[-1]
        c = day_bar(d)["c"]
        value = {"timestamp": session_ms(d)}
        if call.name == "macd":
            value.update(value=2.51, signal=1.75, histogram=0.76)
        elif call.name == "rsi14":
            value["value"] = 58.31
        else:
            value["value"] = round(c * {"sma50": 0.96, "sma200": 0.81, "ema20": 0.99}.get(call.name, 1.0), 4)
        return 200, {"results": {"underlying": {"url": f"{BASE}/v2/aggs/ticker/NVDA/range/1/day/2024-10-01/{lte}"},
                                 "values": [value]}, "status": "OK", "request_id": "ri"}

    def _details(self, call):
        return 200, {"request_id": "rd", "status": "OK", "results": {
            "ticker": "NVDA", "name": "Nvidia Corp", "market": "stocks", "locale": "us", "primary_exchange": "XNAS",
            "type": "CS", "active": True, "currency_name": "usd", "cik": "0001045810", "market_cap": 4.43e12,
            "description": "Nvidia designs GPUs and the systems built around them.",
            "homepage_url": "https://www.nvidia.com", "total_employees": 36000, "list_date": "1999-01-22",
            "share_class_shares_outstanding": 24_300_000_000, "weighted_shares_outstanding": 24_300_000_000}}

    def _dividends(self, call):
        rows = [{"cash_amount": 0.01, "currency": "USD", "declaration_date": f"2026-0{m}-20", "dividend_type": "CD",
                 "ex_dividend_date": f"2026-0{m + 1}-11", "frequency": 4, "id": f"E{m}",
                 "pay_date": f"2026-0{m + 1}-30",
                 "record_date": f"2026-0{m + 1}-11", "ticker": "NVDA"} for m in (8, 5, 2)]
        return 200, {"results": rows, "status": "OK", "request_id": "rv"}

    def _splits(self, call):
        return 200, {"results": [{"execution_date": "2024-06-10", "id": "S1", "split_from": 1, "split_to": 10,
                                  "ticker": "NVDA"},
                                 {"execution_date": "2021-07-20", "id": "S2", "split_from": 1, "split_to": 4,
                                  "ticker": "NVDA"}], "status": "OK", "request_id": "rx"}

    def _related(self, call):
        return 200, {"request_id": "rr", "status": "OK", "stocks_symbol": "NVDA",
                     "results": [{"ticker": t} for t in RELATED]}


def limiter_on(clock, calls=5, mono_offset=0.0):
    """A limiter on fake time. Its wall clock is `clock`; its monotonic clock runs `mono_offset` seconds behind (a
    new process's monotonic clock has nothing to do with the last one's)."""
    if mono_offset:
        return RateLimiter(calls=calls, clock=lambda: clock.t - mono_offset, sleep=clock.sleep, wall=clock)
    return RateLimiter(calls=calls, clock=clock, sleep=clock.sleep, wall=clock)


def desk(clock, key=KEY, path=None, calls=5, server=None, mono_offset=0.0, **kw):
    """A Spotlight wired to a fake Massive server through the real Http and Massive classes."""
    server = server or FakeMassive(clock, **kw)
    massive = Massive(key, Http(server, sleep=no_retry_sleep), limiter_on(clock, calls, mono_offset))
    return Spotlight(massive, path, clock=clock), server


def is_open(t: float) -> bool:
    return market_open(datetime.fromtimestamp(t, NEW_YORK))


async def run_steps(spot, clock, hours: float, every: float = 15.0, each=None) -> None:
    end = clock.t + hours * HOUR
    while clock.t < end:
        done = await spot.step(is_open(clock.t))
        if each:
            each(clock.t, done)
        clock.t += every


def simulate(spot, clock, hours: float, every: float = 15.0, each=None) -> None:
    asyncio.run(run_steps(spot, clock, hours, every, each))


def ids(items) -> list[str]:
    return [n.get("id") for n in items]


def assert_fits(e) -> None:
    """Discord's embed limits."""
    assert e.title is None or len(e.title) <= 256
    assert e.description is None or len(e.description) <= 4096
    assert len(e.fields) <= 25
    for f in e.fields:
        assert 0 < len(f.name) <= 256 and 0 < len(f.value) <= 1024
    assert not e.footer.text or len(e.footer.text) <= 2048
    assert len(e) <= 6000
    json.dumps(e.to_dict())


# =====================================================================================================================
# find_key
# =====================================================================================================================

def test_the_documented_key_names_in_order():
    assert KEY_NAMES == ("MASSIVE_API_KEY", "MASSIVE_KEY", "MASSIVE_API", "MASSIVE_TOKEN", "MASSIVE", "POLYGON_API_KEY",
                         "POLYGON_IO_API_KEY", "POLYGONIO_API_KEY")


@pytest.mark.parametrize("name", KEY_NAMES)
def test_find_key_reads_each_documented_name(name):
    assert find_key({name: f"  {KEY}\n"}) == (KEY, name)
    assert find_key({name: "short"}) == ("short", name)  # a documented name is trusted whatever the value looks like
    assert find_key({name: "has spaces: and colons"}) == ("has spaces: and colons", name)


def test_find_key_prefers_the_documented_names_in_order():
    env = {name: f"value-for-{name}" for name in KEY_NAMES}
    for i, name in enumerate(KEY_NAMES):
        assert find_key({k: v for k, v in env.items() if k in KEY_NAMES[i:]}) == (f"value-for-{name}", name)
    # a documented name beats a look-alike, even one that sorts first
    assert find_key({"A_MASSIVE_KEY_OLD": "x" * 32, "POLYGONIO_API_KEY": "pk"}) == ("pk", "POLYGONIO_API_KEY")
    assert find_key({"A_MASSIVE_KEY_OLD": "x" * 32, "MASSIVE": "mk"}) == ("mk", "MASSIVE")
    # a bare POLYGON is no documented name: never read, so the MASSIVE look-alike is the key
    assert find_key({"A_MASSIVE_KEY_OLD": "x" * 32, "POLYGON": "pk"}) == ("x" * 32, "A_MASSIVE_KEY_OLD")


@pytest.mark.parametrize("empty", ["", "   ", "\n\t "])
def test_find_key_skips_empty_values(empty):
    assert find_key({"MASSIVE_API_KEY": empty, "POLYGON_API_KEY": "pk_live"}) == ("pk_live", "POLYGON_API_KEY")
    assert find_key({name: empty for name in KEY_NAMES}) == (None, None)
    assert find_key({"MASSIVE_X": empty}) == (None, None)
    assert find_key({}) == (None, None)


@pytest.mark.parametrize("name", ["massive_api_token", "MASSIVE_API_KEY_2", "MASSIVEKEY", "Massive_ApiKey",
                                  "RAILWAY_MASSIVE_IO", "MY_MASSIVE", "MASSIVE_POLYGON_KEY"])
def test_find_key_finds_key_like_values_under_other_massive_names(name):
    assert find_key({name: f" {KEY} ", "OTHER": "x" * 40}) == (KEY, name)


@pytest.mark.parametrize("name", [
    # Polygon names other than the documented three: crypto setups keep wallet and explorer secrets under these
    "POLYGON", "POLYGON_KEY", "POLYGON_API", "POLYGON_TOKEN", "POLYGON_APIKEY", "RAILWAY_POLYGON_IO", "PolygonKey",
    "POLYGON_PRIVATE_KEY", "POLYGON_RPC_URL", "POLYGONSCAN_API_KEY", "POLYGON_MNEMONIC", "POLYGON_WALLET_KEY",
    # MASSIVE names that say they hold something else
    "MY_MASSIVE_SECRET", "massive_secret_key", "MASSIVE_PRIVATE_KEY", "MASSIVE_WALLET", "MASSIVE_SEED_PHRASE",
    "MASSIVE_MNEMONIC", "MASSIVE_RPC", "MASSIVESCAN_KEY", "MASSIVE_PASSWORD", "massive_passphrase",
])
def test_find_key_never_reads_secrets_or_other_polygon_names(name):
    assert find_key({name: f" {KEY} ", "OTHER": "x" * 40}) == (None, None)
    # and a usable name next to it still wins, even when the refused one sorts first
    assert find_key({name: KEY, "ZZ_MASSIVE_KEY": "z" * 20}) == ("z" * 20, "ZZ_MASSIVE_KEY")


@pytest.mark.parametrize("value", ["https://api.massive.com", "true", "1", "your key here", "abc def ghi jkl mno pq",
                                   "key:with:colons:12345", "x" * 15, "y" * 129, "ünïcödé-ünïcödé-ünïcödé",
                                   "=" * 20,
                                   "${MASSIVE_API_KEY}", '"' + "a" * 20 + '"'])
def test_find_key_ignores_junk_under_look_alike_names(value):
    assert find_key({"MASSIVE_URL": value, "MASSIVE_SETTING": value}) == (None, None)


def test_find_key_key_like_length_bounds_and_unrelated_names():
    assert find_key({"MASSIVE_X": "k" * 16}) == ("k" * 16, "MASSIVE_X")
    assert find_key({"MASSIVE_X": "k" * 128}) == ("k" * 128, "MASSIVE_X")
    assert find_key({"MASSIVE_X": "k" * 15}) == (None, None)
    assert find_key({"MASSIVE_X": "k" * 129}) == (None, None)
    assert find_key({"MASSIVE_X": "Ab_-09" * 4}) == ("Ab_-09" * 4, "MASSIVE_X")
    assert find_key({"GITHUB_TOKEN": KEY, "OPENAI_API_KEY": KEY, "MASS_IVE": KEY}) == (None, None)
    # several look-alikes: the first by name, every time (a POLYGON look-alike sorting first is never read)
    env = {"A_POLYGON": "a" * 20, "B_MASSIVE": "b" * 20, "M_MASSIVE": "m" * 20, "Z_POLYGON": "z" * 20}
    assert find_key(env) == ("b" * 20, "B_MASSIVE")
    # a look-alike with junk is skipped for the next one
    assert find_key({"A_MASSIVE": "not a key", "B_MASSIVE": "b" * 20}) == ("b" * 20, "B_MASSIVE")


def test_find_key_reads_the_process_environment_by_default(monkeypatch):
    import os
    for name in list(os.environ):
        if "MASSIVE" in name.upper() or "POLYGON" in name.upper():
            monkeypatch.delenv(name)
    assert find_key() == (None, None)
    monkeypatch.setenv("POLYGON_KEY", " pk_from_env_0123456789 ")
    monkeypatch.setenv("POLYGON_PRIVATE_KEY", "0x" + "ab" * 32)
    assert find_key() == (None, None)  # neither is a Massive key
    monkeypatch.setenv("POLYGON_API_KEY", " pk_from_env ")
    assert find_key() == ("pk_from_env", "POLYGON_API_KEY")
    monkeypatch.setenv("MASSIVE_API_KEY", KEY)
    assert find_key() == (KEY, "MASSIVE_API_KEY")


# =====================================================================================================================
# RateLimiter
# =====================================================================================================================

def test_limiter_grants_five_then_refuses_until_the_oldest_expires():
    clock = Clock(1000.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    for t in (1000, 1010, 1020, 1030, 1040):
        clock.t = t
        assert lim.try_acquire()
    clock.t = 1040
    assert not lim.try_acquire() and lim.wait_time() == 21.0
    clock.t = 1060.999
    assert not lim.try_acquire()
    clock.t = 1061.0  # the first one is exactly a window old: it no longer counts
    assert lim.try_acquire()
    clock.t = 1070.5
    assert not lim.try_acquire() and lim.wait_time() == 0.5
    clock.t = 1071.0
    assert lim.try_acquire()
    assert lim.total == 7 and lim.used() == 5
    assert clock.slept == []  # try_acquire never waits


def test_limiter_used_counts_the_last_window_and_total_counts_everything():
    clock = Clock(0.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    assert lim.used() == 0 and lim.total == 0 and lim.wait_time() == 0
    for i in range(5):
        clock.t = i * 10.0
        assert lim.try_acquire()
    assert lim.used() == 5
    for t, used in ((60.9, 5), (61.0, 4), (71.0, 3), (100.9, 1), (101.0, 0), (500.0, 0)):
        clock.t = t
        assert lim.used() == used, t
    assert lim.total == 5


def test_limiter_custom_calls_and_window():
    clock = Clock(0.0)
    lim = RateLimiter(calls=2, window=10.0, clock=clock, sleep=clock.sleep)
    assert lim.try_acquire() and lim.try_acquire() and not lim.try_acquire()
    clock.t = 9.99
    assert not lim.try_acquire()
    clock.t = 10.0
    assert lim.try_acquire() and lim.try_acquire() and not lim.try_acquire()


def test_acquire_waits_exactly_until_a_slot_frees():
    clock = Clock(1000.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(5):
        assert lim.try_acquire()
    assert asyncio.run(lim.acquire()) is True
    assert clock.t == 1061.0 and clock.slept == [61.0] and lim.total == 6


def test_acquire_timeout_returns_false_without_taking_a_slot():
    clock = Clock(1000.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(5):
        lim.try_acquire()

    async def scenario():
        return [await lim.acquire(timeout) for timeout in (0.0, 1.0, 60.99, -5.0)]
    assert asyncio.run(scenario()) == [False] * 4
    assert lim.total == 5 and lim.used() == 5 and clock.t == 1000.0 and clock.slept == []
    assert asyncio.run(lim.acquire(61.0)) is True  # exactly enough time: it waits
    assert clock.t == 1061.0 and lim.total == 6


def test_acquire_with_a_free_slot_never_sleeps_whatever_the_timeout():
    clock = Clock(5.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)

    async def scenario():
        return [await lim.acquire(timeout) for timeout in (None, 0.0, -1.0, 0.001, 1e9)]
    assert asyncio.run(scenario()) == [True] * 5
    assert clock.slept == [] and lim.total == 5


def test_penalize_blocks_for_its_duration():
    clock = Clock(1000.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    assert lim.try_acquire()
    lim.penalize(30.0)
    assert lim.used() == 1 and lim.total == 1  # a penalty takes no slot
    for t in (1000.0, 1015.0, 1029.999):
        clock.t = t
        assert not lim.try_acquire()
    clock.t = 1030.0
    assert lim.try_acquire()
    # the default penalty is the whole window, and a shorter one never shortens a longer one
    lim.penalize()
    assert lim.wait_time() == WINDOW
    lim.penalize(5.0)
    assert lim.wait_time() == WINDOW
    lim.penalize(100.0)
    assert lim.wait_time() == 100.0
    assert asyncio.run(lim.acquire()) is True
    assert clock.t == 1130.0 and clock.slept == [100.0]


def test_waiters_are_served_in_arrival_order():
    vt = VirtualTime(1000.0)
    lim = RateLimiter(clock=vt, sleep=vt.sleep)
    for _ in range(5):
        assert lim.try_acquire()
    order = []

    async def waiter(i):
        await vt.sleep(i * 0.5)  # waiter i arrives i/2 seconds in
        assert await lim.acquire() is True
        order.append((i, vt.t))
    asyncio.run(vt.run(*[waiter(i) for i in reversed(range(12))]))  # created backwards, arriving forwards
    assert [i for i, _ in order] == list(range(12))
    assert [t for _, t in order] == [1061.0] * 5 + [1122.0] * 5 + [1183.0] * 2
    assert lim.total == 17


def test_try_acquire_may_jump_the_queue_but_never_over_grants():
    vt = VirtualTime(1000.0)
    lim = RateLimiter(clock=vt, sleep=vt.sleep)
    grants = []
    for t in (1000.0, 1001.0, 1002.0, 1003.0, 1004.0):
        vt.t = t
        assert lim.try_acquire()
        grants.append(t)
    got = {}

    async def thief():
        await vt.sleep(57.0)  # wakes at 1061, the moment the first slot frees, just before the waiter
        got["thief"] = lim.try_acquire()
        grants.append(vt.t)

    async def waiter():
        assert await lim.acquire() is True
        got["waiter"] = vt.t
        grants.append(vt.t)
    asyncio.run(vt.run(thief(), waiter()))
    assert got == {"thief": True, "waiter": 1062.0}
    assert_budget(grants)
    assert lim.total == 7


def test_a_cancelled_waiter_takes_no_slot_and_releases_the_queue():
    vt = VirtualTime(1000.0)
    lim = RateLimiter(clock=vt, sleep=vt.sleep)
    for _ in range(5):
        lim.try_acquire()

    async def scenario():
        waiter = asyncio.ensure_future(lim.acquire())
        for _ in range(5):
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert lim.total == 5
        return await lim.acquire()
    assert asyncio.run(vt.run(scenario())) == [True]
    assert vt.t == 1061.0 and lim.total == 6


@pytest.mark.parametrize("model", ["virtual", "eager"])
@pytest.mark.parametrize("seed", range(25))
def test_limiter_never_grants_more_than_five_in_any_61_seconds(model, seed):
    """Property test: many acquire() waiters (with and without timeouts), try_acquire() pollers and penalties, in a
    random mix, on two models of time. No 61-second window may ever hold more than 5 grants."""
    rng = random.Random(seed * 7919 + (model == "eager"))
    clock = VirtualTime(1000.0) if model == "virtual" else Clock(1000.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    grants, refused, blocks = [], [], []

    def gap(hi: float) -> float:  # multiples of 1/16 s, so every sum is exact
        return rng.randint(0, int(hi * 16)) / 16

    async def waiter():
        await clock.sleep(gap(240))
        for _ in range(rng.randint(1, 4)):
            timeout = rng.choice([None, None, 0.0, gap(5), gap(90), gap(400)])
            if await lim.acquire(timeout):
                grants.append(clock())
            else:
                assert timeout is not None, "acquire() without a timeout never gives up"
                refused.append(clock())
            await clock.sleep(gap(30))

    async def poller():
        for _ in range(rng.randint(5, 50)):
            await clock.sleep(gap(25))
            if lim.try_acquire():
                grants.append(clock())

    async def penalizer():
        for _ in range(rng.randint(0, 3)):
            await clock.sleep(gap(500))
            seconds = gap(90)
            blocks.append((clock(), clock() + seconds))
            lim.penalize(seconds)

    tasks = ([waiter() for _ in range(rng.randint(3, 30))] + [poller() for _ in range(rng.randint(1, 6))]
             + [penalizer()])
    rng.shuffle(tasks)
    asyncio.run(clock.run(*tasks))
    assert grants
    assert_budget(grants)
    assert lim.total == len(grants)  # a refused acquire() took nothing
    assert lim.used() <= 5
    for start, end in blocks:
        assert not [t for t in grants if start <= t < end], (start, end)


def test_acquire_timeout_holds_while_another_waiter_sleeps():
    vt = VirtualTime(1000.0)
    lim = RateLimiter(clock=vt, sleep=vt.sleep)
    for _ in range(5):
        assert lim.try_acquire()
    out = {}

    async def patient():  # no timeout: holds the queue while it sleeps until 1061
        assert await lim.acquire() is True
        out["patient"] = vt.t

    async def hurried():  # can wait one second at most
        ok = await lim.acquire(timeout=1.0)
        out["hurried"] = (ok, vt.t)
    asyncio.run(vt.run(patient(), hurried()))
    assert out == {"hurried": (False, 1000.0), "patient": 1061.0}  # gave up at once, without sleeping
    assert lim.total == 6 and list(lim._queue) == []


def test_acquire_deadlines_are_judged_by_the_place_in_the_queue():
    """Five slots free at 1061 (all used at 1000), the next five at 1122. Five patient waiters queue first."""
    vt = VirtualTime(1000.0)
    lim = RateLimiter(clock=vt, sleep=vt.sleep)
    for _ in range(5):
        assert lim.try_acquire()
    out = {}

    async def waiter(name, timeout):
        ok = await lim.acquire(timeout)
        out[name] = (ok, vt.t)
    names = [f"patient{i}" for i in range(5)]
    timeouts = {"one short": 121.0, "just enough": 122.0, "also enough": 122.0, "plenty": 500.0, "too short": 60.0}
    asyncio.run(vt.run(*[waiter(n, None) for n in names], *[waiter(n, t) for n, t in timeouts.items()]))
    assert out == {**{n: (True, 1061.0) for n in names},
                   "one short": (False, 1000.0),  # sixth in line: its turn (1122) is after its deadline (1121)
                   "just enough": (True, 1122.0), "also enough": (True, 1122.0), "plenty": (True, 1122.0),
                   "too short": (False, 1000.0)}
    assert lim.total == 5 + 8 and list(lim._queue) == []
    assert 1000.0 not in [t for name, (ok, t) in out.items() if ok]


def test_acquire_deadline_counts_a_penalty():
    clock = Clock(1000.0)
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    lim.penalize(100.0)
    assert asyncio.run(lim.acquire(99.9)) is False and clock.t == 1000.0 and clock.slept == []
    assert asyncio.run(lim.acquire(100.0)) is True and clock.t == 1100.0 and lim.total == 1


def test_slots_are_one_element_lists_and_the_bool_forms_agree():
    clock = Clock(1000.0)
    lim = RateLimiter(calls=2, clock=clock, sleep=clock.sleep)
    a = lim.try_slot()
    assert a == [1000.0] and isinstance(a, list)
    clock.t = 1001.0
    assert asyncio.run(lim.acquire_slot(0.0)) == [1001.0]
    assert lim.try_slot() is None and lim.try_acquire() is False
    assert asyncio.run(lim.acquire_slot(59.9)) is None and clock.t == 1001.0  # needs 60 s: gives up at once
    assert asyncio.run(lim.acquire(59.9)) is False and clock.slept == []
    slot = asyncio.run(lim.acquire_slot(60.0))
    assert slot == [1061.0] and clock.t == 1061.0 and lim.total == 3
    assert asyncio.run(lim.acquire_slot()) == [1062.0] and clock.slept == [60.0, 1.0]


def test_finish_counts_the_window_from_when_the_request_ended():
    clock = Clock(1000.0)
    lim = RateLimiter(calls=2, clock=clock, sleep=clock.sleep)
    slow, quick = lim.try_slot(), lim.try_slot()
    clock.t = 1030.0
    lim.finish(slow)  # a slow request: its answer came 30 s later
    assert slow == [1030.0] and quick == [1000.0]
    assert lim.wait_time() == 31.0  # the quick one frees first
    clock.t = 1061.0
    assert lim.used() == 1 and lim.wait_time() == 0.0
    assert lim.try_slot() == [1061.0]
    assert lim.wait_time() == 30.0  # the slow one still counts until 1091
    clock.t = 1091.0
    assert lim.used() == 1 and lim.try_acquire()


def test_finish_never_moves_a_slot_back():
    clock = Clock(1000.0)
    lim = RateLimiter(calls=1, clock=clock, sleep=clock.sleep)
    slot = lim.try_slot()
    clock.t = 990.0  # a clock that seems to go back (only fake ones do)
    lim.finish(slot)
    assert slot == [1000.0]
    clock.t = 1000.0
    lim.finish(slot)
    lim.finish(slot)
    assert slot == [1000.0] and lim.used() == 1 and lim.total == 1  # finishing takes no extra slot


def test_recent_gives_the_wall_clock_times_of_the_slots_still_counting():
    mono = Clock(50.0)
    wall0 = 1_791_000_000.0
    lim = RateLimiter(clock=mono, sleep=mono.sleep, wall=lambda: wall0 + mono.t)
    assert lim.recent() == []
    slots = []
    for t in (100.0, 50.0, 60.0):  # out of order on purpose
        mono.t = t
        slots.append(lim.try_slot())
    mono.t = 105.0
    lim.finish(slots[0])
    assert lim.recent() == [wall0 + 50, wall0 + 60, wall0 + 105]  # sorted, and the finished one re-stamped
    mono.t = 111.0
    assert lim.recent() == [wall0 + 60, wall0 + 105]  # the one from 50 is a window old
    mono.t = 166.0
    assert lim.recent() == []


def test_restore_counts_what_a_previous_run_sent_in_the_last_window():
    mono = Clock(10.0)  # a new process: its monotonic clock starts over
    wall = 1_791_000_000.0
    lim = RateLimiter(clock=mono, sleep=mono.sleep, wall=lambda: wall + mono.t - 10.0)
    lim.restore([wall - 70, wall - 61, wall - 60.5, wall - 30, wall, wall + 5, "x", None, True, [1], {"t": wall}])
    assert lim.used() == 3 and lim.total == 0  # only those still inside the window; restoring isn't sending
    assert lim.recent() == [wall - 60.5, wall - 30, wall]
    assert lim.wait_time() == 0.0
    mono.t = 10.5
    assert lim.used() == 2  # the one from 60.5 s before the restart has just left the window
    lim.restore([wall - 20, wall - 10, wall - 5])
    assert lim.used() == 5 and lim.recent() == [wall - 30, wall - 20, wall - 10, wall - 5, wall]
    assert lim.wait_time() == 30.5 and not lim.try_acquire()
    lim.restore([wall - 1, wall - 2])  # never more than `calls`
    assert lim.used() == 5
    lim.restore(None)
    lim.restore([])
    assert lim.used() == 5
    mono.t = 10.0 + 61.0
    assert lim.used() == 0 and lim.try_acquire()


def test_recent_and_restore_carry_the_budget_across_a_restart():
    clock = Clock(1_791_000_000.0)
    first = limiter_on(clock, mono_offset=1_000_000.0)
    for _ in range(5):
        assert first.try_acquire()
    saved = first.recent()
    assert saved == [clock.t] * 5
    clock.t += 20.0
    second = limiter_on(clock, mono_offset=1_791_000_000.0 - 3.0)  # the new process's clock reads 3 + 20
    second.restore(saved)
    assert second.used() == 5 and second.wait_time() == 41.0 and not second.try_acquire()
    assert asyncio.run(second.acquire()) is True and clock.t == saved[0] + 61.0


# =====================================================================================================================
# Massive.get
# =====================================================================================================================

def test_get_sends_the_key_as_a_bearer_header_only():
    m, backend, _ = make_massive(reply(200, {"status": "OK", "results": [{"c": 1.0}]}))
    data = asyncio.run(m.get("/v2/aggs/ticker/NVDA/prev", {"adjusted": "true"}))
    assert data["results"] == [{"c": 1.0}]
    (call,) = backend.calls
    assert call.url == f"{BASE}/v2/aggs/ticker/NVDA/prev?adjusted=true"
    assert call.headers == {"Authorization": f"Bearer {KEY}"}
    assert KEY not in call.url and "apikey" not in call.url.lower() and call.timeout == TIMEOUT
    assert m.last_error is None and m.limiter.total == 1 and m.enabled
    h = m.http.health[SOURCE]
    assert h.ok == 1 and h.failed == 0 and h.status == 200


def test_get_encodes_params_into_the_query_string():
    m, backend, _ = make_massive(reply())
    asyncio.run(m.get("/v1/indicators/sma/NVDA", {"timestamp.lte": "2026-10-05", "window": "50", "q": "a b&c"}))
    url = backend.calls[0].url
    assert url.startswith(f"{BASE}/v1/indicators/sma/NVDA?")
    assert dict(parse_qsl(urlsplit(url).query)) == {"timestamp.lte": "2026-10-05", "window": "50", "q": "a b&c"}
    asyncio.run(m.get("/v1/marketstatus/now"))
    assert backend.calls[1].url == f"{BASE}/v1/marketstatus/now"


@pytest.mark.parametrize("key", [None, ""])
def test_get_without_a_key_is_disabled_and_never_calls_out(key):
    m, backend, _ = make_massive(reply(), key=key)
    assert not m.enabled
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert err.value.kind == "key" and backend.calls == [] and m.limiter.total == 0


FAILURES = [  # (answer, kind, status)
    (OSError("Connection reset by peer while talking to api.massive.com"), "network", None),
    (TimeoutError("timed out after 30s"), "network", None),
    (ConnectionRefusedError(111, "Connection refused"), "network", None),
    (reply(500, "Internal Server Error"), "network", 500),
    (reply(502, "<html><body>502 Bad Gateway</body></html>"), "network", 502),
    (reply(503, ""), "network", 503),
    (reply(504, {"status": "ERROR", "error": "upstream timeout"}), "network", 504),
    (reply(429, {"status": "ERROR", "request_id": "x",
                 "error": "You've exceeded the maximum requests per minute, please wait or upgrade your subscription"}),
     "rate", 429),
    (reply(401, {"status": "ERROR", "request_id": "x", "error": "Unknown API Key"}), "key", 401),
    (reply(401, "<html>Unauthorized</html>"), "key", 401),
    (reply(403, NOT_ENTITLED), "plan", 403),
    (reply(403, ""), "plan", 403),
    (reply(404, {"status": "NOT_FOUND", "request_id": "x", "message": "Ticker not found."}), "other", 404),
    (reply(400, {"status": "ERROR", "request_id": "x", "error": "Could not parse the date."}), "other", 400),
    (reply(407, "Proxy Authentication Required"), "other", 407),
    (reply(418, "{not json"), "other", 418),
]


@pytest.mark.parametrize("answer,kind,status", FAILURES)
def test_every_failure_is_one_request_with_the_right_kind_and_no_key_in_sight(answer, kind, status):
    m, backend, _ = make_massive(answer)
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v2/aggs/ticker/NVDA/prev", {"adjusted": "true"}))
    assert err.value.kind == kind and err.value.status == status
    assert len(backend.calls) == 1 and m.limiter.total == 1  # no retries, ever
    assert KEY not in backend.calls[0].url
    no_key_anywhere(m, err.value)


def test_three_5xx_in_a_row_rest_the_source_but_massive_still_asks():
    m, backend, clock = make_massive(reply(500, "boom"), reply(500, "boom"), reply(500, "boom"), reply(200, {"x": 1}),
                                     calls=100)

    async def scenario():
        for _ in range(3):
            with pytest.raises(MassiveError):
                await m.get("/v1/marketstatus/now")
        assert m.http.resting(SOURCE)
        return await m.get("/v1/marketstatus/now")  # force=True: a rest never blocks a budgeted request
    assert asyncio.run(scenario()) == {"x": 1}
    assert len(backend.calls) == 4 and not m.http.resting(SOURCE)


def test_401_rejects_the_key_for_good():
    m, backend, _ = make_massive(reply(401, {"status": "ERROR", "request_id": "x", "error": "Unknown API Key"}),
                                 reply(200, {"status": "OK"}))
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert err.value.kind == "key" and err.value.status == 401
    assert m.key_rejected and not m.enabled
    assert m.last_error == "Massive rejected the key (Unknown API Key)"

    async def later():
        for _ in range(3):
            with pytest.raises(MassiveError) as e:
                await m.get("/v1/marketstatus/now", wait=30)
            assert e.value.kind == "key"
    asyncio.run(later())
    assert len(backend.calls) == 1 and m.limiter.total == 1  # nothing more went out, and no slot was used


def test_401_without_a_json_body_still_says_why():
    m, _, _ = make_massive(reply(401, "<html>401</html>"))
    with pytest.raises(MassiveError):
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert m.last_error == "Massive rejected the key (HTTP 401)"


def test_403_is_a_plan_limit_and_never_rests_the_source():
    m, backend, clock = make_massive(reply(403, NOT_ENTITLED), calls=100)

    async def scenario():
        for _ in range(8):
            with pytest.raises(MassiveError) as err:
                await m.get("/v2/snapshot/locale/us/markets/stocks/tickers/NVDA")
            assert err.value.kind == "plan" and err.value.status == 403
            assert str(err.value) == NOT_ENTITLED["message"]
    asyncio.run(scenario())
    h = m.http.health[SOURCE]
    assert len(backend.calls) == 8 and not m.http.resting(SOURCE)
    assert h.streak == 0 and h.failed == 0 and h.ok == 8 and not h.failing
    assert m.enabled and not m.key_rejected and m.last_error is None


def test_429_backs_off_a_whole_window():
    m, backend, clock = make_massive(reply(429, {"status": "ERROR", "error": "too many"}), reply(200, {"ok": 1}))
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert err.value.kind == "rate" and err.value.status == 429
    assert m.last_error == "rate limited by Massive (backing off a minute)"
    assert m.limiter.wait_time() == WINDOW and m.limiter.used() == 1

    async def during():
        for t in (1000.0, 1030.0, 1060.99):
            clock.t = t
            with pytest.raises(MassiveError) as e:
                await m.get("/v1/marketstatus/now")
            assert e.value.kind == "budget"
    asyncio.run(during())
    assert len(backend.calls) == 1
    clock.t = 1061.0
    assert asyncio.run(m.get("/v1/marketstatus/now")) == {"ok": 1}
    assert len(backend.calls) == 2 and m.last_error is None


def test_the_budget_is_checked_before_any_request():
    m, backend, clock = make_massive(reply(200, {"n": 1}))

    async def scenario():
        out = []
        for _ in range(8):
            try:
                out.append(await m.get("/v1/marketstatus/now"))
            except MassiveError as exc:
                out.append(exc.kind)
        return out
    assert asyncio.run(scenario()) == [{"n": 1}] * 5 + ["budget"] * 3
    assert len(backend.calls) == 5 and m.limiter.total == 5 and m.last_error is None
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now", wait=60.0))  # would need 61 s
    assert err.value.kind == "budget" and len(backend.calls) == 5 and clock.t == 1000.0
    assert asyncio.run(m.get("/v1/marketstatus/now", wait=61.0)) == {"n": 1}  # waits for the slot
    assert clock.t == 1061.0 and len(backend.calls) == 6


def test_many_concurrent_gets_send_exactly_five():
    m, backend, clock = make_massive(reply(200, {"n": 1}))

    async def scenario():
        return await asyncio.gather(*(m.get(f"/v3/reference/tickers/T{i}") for i in range(40)),
                                    return_exceptions=True)
    out = asyncio.run(scenario())
    assert sum(1 for r in out if r == {"n": 1}) == 5
    assert all(isinstance(r, MassiveError) and r.kind == "budget" for r in out if r != {"n": 1})
    assert len(backend.calls) == 5


def test_many_concurrent_waiting_gets_stay_within_the_budget():
    vt = VirtualTime(1000.0)
    backend = Backend(reply(200, {"n": 1}))
    sent = []
    real_get = backend.get

    async def timed_get(url, headers, timeout, proxy=None):
        sent.append(vt.t)
        return await real_get(url, headers, timeout, proxy)
    backend.get = timed_get
    m = Massive(KEY, Http(backend, sleep=no_retry_sleep), RateLimiter(clock=vt, sleep=vt.sleep))
    results = asyncio.run(vt.run(*(m.get("/v1/marketstatus/now", wait=600.0) for _ in range(23))))
    assert results == [{"n": 1}] * 23
    assert_budget(sent)
    assert sent[-1] == 1000.0 + 4 * 61  # 23 requests: five windows


def test_a_success_clears_the_last_error_and_errors_stay_short():
    m, _, clock = make_massive(reply(400, {"status": "ERROR", "error": "e" * 5000}), reply(200, {"n": 1}))
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert len(m.last_error) <= 160 and m.last_error.startswith("HTTP 400: eee")
    assert err.value.status == 400
    assert asyncio.run(m.get("/v1/marketstatus/now")) == {"n": 1}
    assert m.last_error is None


@pytest.mark.parametrize("status", [200, 201, 204])
@pytest.mark.parametrize("body", ["", "   ", "null", "[]", "[1, 2, 3]", '"OK"', "42", "true",
                                  "<html><body>Service temporarily unavailable</body></html>", "{not json", '{"a": 1'])
def test_a_2xx_answer_that_is_not_a_json_object_raises(status, body):
    m, backend, _ = make_massive(Response(status, body))
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert err.value.kind == "other" and err.value.status == status
    assert str(err.value) == m.last_error == f"Massive sent an unreadable answer (HTTP {status})"
    assert len(backend.calls) == 1 and m.limiter.total == 1 and m.enabled  # no retry, and the key is fine
    no_key_anywhere(m, err.value)


@pytest.mark.parametrize("body,data", [("{}", {}), ('{"results": []}', {"results": []}),
                                       (' {"a": {"b": [1, null]}} ', {"a": {"b": [1, None]}})])
def test_a_json_object_answer_is_returned_as_it_came(body, data):
    m, _, _ = make_massive(Response(200, body))
    assert asyncio.run(m.get("/v1/marketstatus/now")) == data and m.last_error is None


@pytest.mark.parametrize("status,kind", [(401, "key"), (403, "plan"), (404, "other"), (400, "other")])
@pytest.mark.parametrize("body", ['"Forbidden"', "[1, 2]", "42", "null", "", "true"])
def test_error_answers_with_odd_json_still_raise_massive_error(status, kind, body):
    m, backend, _ = make_massive(Response(status, body))
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    assert err.value.kind == kind and err.value.status == status and len(backend.calls) == 1
    expected = {401: "Massive rejected the key (HTTP 401)", 403: "not in this Massive plan",
                404: "HTTP 404: ", 400: "HTTP 400: "}[status]
    assert str(err.value) == expected
    assert m.key_rejected == (status == 401)


ECHOES = {  # answers (or failures) that repeat the key back
    "401": reply(401, {"status": "ERROR", "error": f"key {KEY}"}),
    "401 long": reply(401, {"status": "ERROR", "request_id": "x", "error": f"API key '{KEY}' is not valid",
                            "message": f"API key '{KEY}' is not valid"}),
    "400": reply(400, {"status": "ERROR", "error": f"bad key {KEY}"}),
    "403": reply(403, {"status": "NOT_AUTHORIZED", "message": f"{KEY} is not entitled to this data"}),
    "404": reply(404, {"status": "NOT_FOUND", "message": f"nothing for {KEY}"}),
    "418": reply(418, {"error": f"{KEY}{KEY}"}),
    "500": reply(500, f"crash for {KEY}"),
    "503 json": reply(503, {"error": f"key {KEY}"}),
    "network": OSError(f"connect failed (Authorization: Bearer {KEY})"),
}


@pytest.mark.parametrize("answer", list(ECHOES.values()), ids=list(ECHOES))
def test_an_answer_that_echoes_the_key_does_not_leak_it(answer):
    m, _, _ = make_massive(answer)
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    for text in (str(err.value), repr(err.value), m.last_error or ""):
        assert KEY not in text, text
    assert "…" in str(err.value)  # replaced, not dropped


@pytest.mark.parametrize("answer", [reply(401, {"error": KEY}), reply(500, f"crash for {KEY}"), reply(503, {"error": KEY}),
                                    OSError(f"connect failed (Authorization: Bearer {KEY})")],
                         ids=["401", "500", "503", "network"])
def test_a_key_echoed_by_massive_stays_out_of_the_status_health_line(answer):
    m, _, _ = make_massive(answer)
    with pytest.raises(MassiveError):
        asyncio.run(m.get("/v1/marketstatus/now"))
    h = m.http.health[SOURCE]
    assert KEY not in h.line() and KEY not in (h.last_error or "")


@pytest.mark.parametrize("body", [{"error": "GET /v2/x?apiKey=abc123&key=zzz failed"},
                                  {"message": "see https://api.massive.com/v1/x?apikey=SECRET1&token=SECRET2"}])
def test_key_parameters_in_error_text_are_scrubbed(body):
    m, _, _ = make_massive(reply(400, body))
    with pytest.raises(MassiveError) as err:
        asyncio.run(m.get("/v1/marketstatus/now"))
    for secret in ("abc123", "zzz", "SECRET1", "SECRET2"):
        assert secret not in str(err.value) and secret not in m.last_error
    assert "=…" in str(err.value)


class SlowBackend(Backend):
    """Each request takes `seconds` of fake time before its answer (or failure) comes."""

    def __init__(self, clock, seconds, *answers):
        super().__init__(*answers)
        self.clock, self.seconds = clock, seconds

    async def get(self, url, headers, timeout, proxy=None):
        self.clock.t += self.seconds
        return await super().get(url, headers, timeout, proxy)


SLOW = {"ok": reply(200, {"n": 1}), "network": OSError("reset"), "500": reply(500, "x"),
        "404": reply(404, {"message": "no"}), "403": reply(403, NOT_ENTITLED), "garbled": Response(200, "<html>"),
        "429": reply(429, {"error": "too many"}), "401": reply(401, {"error": "Unknown API Key"})}


@pytest.mark.parametrize("answer", list(SLOW.values()), ids=list(SLOW))
def test_a_slow_request_counts_against_the_budget_from_when_it_ended(answer):
    clock = Clock(1000.0)
    backend = SlowBackend(clock, 30.0, answer)
    m = Massive(KEY, Http(backend, sleep=no_retry_sleep), limiter_on(clock, calls=1))
    try:
        asyncio.run(m.get("/v1/marketstatus/now"))
    except MassiveError:
        pass
    assert clock.t == 1030.0 and len(backend.calls) == 1
    assert m.limiter.recent() == [1030.0] and m.limiter.total == 1  # the slot is finished, success or not
    clock.t = 1090.9
    assert m.limiter.try_slot() is None
    clock.t = 1091.0
    assert m.limiter.wait_time() == 0.0


# =====================================================================================================================
# Spotlight: scheduling
# =====================================================================================================================

def test_everything_is_due_on_a_fresh_spotlight_in_priority_order():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, _ = desk(clock)
    assert [j.name for j in spot.due(True)] == JOB_NAMES
    assert [j.name for j in spot.due(False)] == JOB_NAMES
    assert JOB_NAMES[:3] == ["snapshot", "news", "prev"]
    assert set(AFTER_NEW_SESSION) == {"daily", "minutes", *INDICATORS}


@pytest.mark.parametrize("job", JOBS, ids=lambda j: j.name)
@pytest.mark.parametrize("market_is_open", [True, False])
def test_each_job_comes_due_after_its_interval(job, market_is_open):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, _ = desk(clock)
    every = job.every_open if market_is_open else job.every_closed
    for other in JOB_NAMES:
        spot.ran[other] = clock.t
    spot.ran[job.name] = clock.t - every + 1
    assert job.name not in [j.name for j in spot.due(market_is_open)]
    spot.ran[job.name] = clock.t - every
    assert [j.name for j in spot.due(market_is_open)] == [job.name]
    spot.not_in_plan[job.name] = clock.t  # outside the plan: asked again only every 12 hours
    spot.ran[job.name] = clock.t - max(every, PLAN_RETRY) + 1
    assert job.name not in [j.name for j in spot.due(market_is_open)]
    spot.ran[job.name] = clock.t - max(every, PLAN_RETRY)
    assert [j.name for j in spot.due(market_is_open)] == [job.name]
    # a passing failure's retry time holds the job back, whatever its interval says
    spot.retry_at[job.name] = clock.t + 1
    assert spot.due(market_is_open) == []
    spot.retry_at[job.name] = clock.t
    assert [j.name for j in spot.due(market_is_open)] == [job.name]


def test_the_intervals_and_retry_constants():
    assert {j.name: (j.every_open, j.every_closed) for j in JOBS} == {
        "snapshot": (60, 900), "news": (180, 600), "prev": (900, 1800), "status": (900, 3600),
        "daily": (21600, 21600), "minutes": (21600, 21600), "sma50": (21600, 21600), "sma200": (21600, 21600),
        "ema20": (21600, 21600), "rsi14": (21600, 21600), "macd": (21600, 21600), "details": (86400, 86400),
        "dividends": (86400, 86400), "splits": (604800, 604800), "related": (604800, 604800)}
    assert PLAN_RETRY == 12 * HOUR and (RETRY_FIRST, RETRY_MAX) == (120.0, 1800.0) and SNAPSHOT_FRESH == 1800.0


def test_the_snapshot_is_due_on_its_interval_until_massive_says_it_is_not_in_the_plan():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock)
    for name in JOB_NAMES:
        spot.ran[name] = clock.t
    # never answered yet: no special rule, just its interval (a minute while open, 15 minutes while closed)
    clock.t = t0 + 59
    assert "snapshot" not in [j.name for j in spot.due(True)]
    clock.t = t0 + 60
    assert [j.name for j in spot.due(True)] == ["snapshot"] and "snapshot" not in spot.ok_at
    assert "snapshot" not in [j.name for j in spot.due(False)]
    clock.t = t0 + 900
    assert "snapshot" in [j.name for j in spot.due(False)]
    # the free plan answers 403: from then on every 12 hours, open or closed
    assert asyncio.run(spot.step(True)) == ["news", "prev", "status"]
    assert server.of("snapshot")[-1].t == t0 + 900 and spot.not_in_plan == {"snapshot": t0 + 900}
    assert spot.ran["snapshot"] == t0 + 900 and spot.plan() == "free plan (end of day)"
    for t, due in ((t0 + 900 + 60, False), (t0 + 900 + PLAN_RETRY - 1, False), (t0 + 900 + PLAN_RETRY, True)):
        clock.t = t
        assert ("snapshot" in [j.name for j in spot.due(True)]) is due
        assert ("snapshot" in [j.name for j in spot.due(False)]) is due
    # an upgraded key: the next probe works, and it's back to every minute
    server.paid = True
    assert "snapshot" in asyncio.run(spot.step(True))
    assert spot.not_in_plan == {} and spot.ok_at["snapshot"] == clock.t
    assert spot.plan() == "paid plan (delayed snapshot available)"
    clock.t += 60
    assert "snapshot" in [j.name for j in spot.due(True)]


def test_the_first_step_asks_for_the_most_important_things_first():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    done = asyncio.run(spot.step(True))
    assert [c.name for c in server.calls] == ["snapshot", "news", "prev", "status", "daily"]
    assert done == ["news", "prev", "status", "daily"]  # the snapshot isn't in the free plan
    assert spot.not_in_plan.keys() == {"snapshot"} and spot.errors == {}
    assert "minutes" not in spot.ran  # out of budget: waits for the next step without being marked as tried
    assert asyncio.run(spot.step(True)) == [] and len(server.calls) == 5  # still the same minute
    clock.t += 61
    assert asyncio.run(spot.step(True)) == ["minutes", "sma50", "sma200", "ema20", "rsi14"]
    clock.t += 61
    assert asyncio.run(spot.step(True)) == ["macd", "details", "dividends", "splits", "related"]
    assert spot.due(True) == []  # all done until the news is due again
    assert len(server.calls) == 15 and spot.massive.limiter.total == 15


@pytest.mark.parametrize("how", ["no massive", "no key", "rejected key"])
def test_a_disabled_spotlight_does_nothing(how):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    if how == "no massive":
        spot, server = Spotlight(None, clock=clock), FakeMassive(clock)
    else:
        spot, server = desk(clock, key=None if how == "no key" else KEY)
        if how == "rejected key":
            spot.massive.key_rejected = True
    assert not spot.enabled
    assert asyncio.run(spot.step(True)) == [] and server.calls == []
    assert spot.plan() == {"no massive": "no key", "no key": "no key", "rejected key": "key rejected"}[how]


def test_a_rejected_key_stops_the_desk_at_once():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock, key="mk_wrong_key_0000000000")
    assert asyncio.run(spot.step(True)) == []
    assert len(server.calls) == 1  # the first 401 ends the step
    assert spot.massive.key_rejected and not spot.enabled and spot.plan() == "key rejected"
    assert spot.massive.last_error == "Massive rejected the key (Unknown API Key)"
    assert spot.ran == spot.ok_at == spot.retry_at == spot.errors == {}  # nothing is marked: the key is the problem
    simulate(spot, clock, hours=2)
    assert len(server.calls) == 1


def test_a_429_ends_the_step_and_silences_the_desk_for_a_window():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock)
    server.hooks["prev"] = once(reply(429, {"status": "ERROR", "error": "exceeded the maximum requests per minute"}))
    assert asyncio.run(spot.step(True)) == ["news"]
    assert [c.name for c in server.calls] == ["snapshot", "news", "prev"]  # status and the rest wait
    assert spot.massive.last_error == "rate limited by Massive (backing off a minute)"
    assert "prev" not in spot.ran and "prev" not in spot.errors and "prev" not in spot.retry_at
    assert spot.massive.limiter.wait_time() == WINDOW
    simulate(spot, clock, hours=0.5)
    later = server.calls[3:]
    assert later[0].name == "prev" and later[0].t == t0 + 75  # the first step after the minute's pause
    assert min(c.t for c in later) >= t0 + WINDOW
    assert_budget([c.t for c in server.calls])
    assert spot.prev and "prev" in spot.ok_at


def test_one_broken_answer_does_not_stop_the_other_jobs():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock)
    server.hooks["news"] = reply(200, {"results": "not a list"})
    server.hooks["related"] = reply(200, {"results": ["AMD", {"ticker": 7}, {"ticker": "INTC"}, None, {"t": "X"}]})
    server.hooks["status"] = OSError("network is unreachable")
    simulate(spot, clock, hours=0.1)
    assert set(spot.ok_at) == set(JOB_NAMES) - {"snapshot", "news", "status"}
    assert spot.errors == {"news": "unexpected news answer", "status": "OSError: network is unreachable"}
    assert spot.related == ["INTC"] and spot.news == []  # only the rows that make sense are kept
    # the failed ones are tried again in 2 minutes (when the budget allows), then 4: not after their interval
    assert [c.t - t0 for c in server.of("news")] == [c.t - t0 for c in server.of("status")] == [0, 150]
    assert spot.retry_at == {"news": t0 + 150 + 240, "status": t0 + 150 + 240}
    assert "news" not in spot.ran and "status" not in spot.ran
    assert len(server.of("related")) == 1 and spot.ran["related"] == t0 + 225


def test_odd_but_valid_json_answers_never_escape_step():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock, calls=100)
    for name in JOB_NAMES:
        server.hooks[name] = Response(200, "[1, 2, 3]")
    assert asyncio.run(spot.step(True)) == []
    assert [c.name for c in server.calls] == [n for n in JOB_NAMES if n != "minutes"]  # no session to ask minutes for
    assert spot.errors == {n: ("waiting for the previous session's bar" if n == "minutes"
                               else "Massive sent an unreadable answer (HTTP 200)") for n in JOB_NAMES}
    assert spot.retry_at == {n: t0 + 120 for n in JOB_NAMES} and spot.ran == {} and spot.ok_at == {}
    assert spot.prev is None and spot.status is None and spot.snapshot is None and spot.details is None
    assert spot.news == spot.daily == spot.minutes == spot.dividends == spot.splits == spot.related == []
    assert spot.indicators == {}
    clock.t = t0 + 119
    assert asyncio.run(spot.step(True)) == [] and len(server.calls) == len(JOB_NAMES) - 1
    server.hooks.clear()
    clock.t = t0 + 120
    assert asyncio.run(spot.step(True)) == [n for n in JOB_NAMES if n != "snapshot"]
    assert spot.errors == {} and spot.not_in_plan.keys() == {"snapshot"}
    assert not [n for n, t in spot.retry_at.items() if t > clock.t]


# How each kind of failure of one job (news) is handled: (the answer, how the step goes on, when news is asked again)
OUTCOMES = {
    "timeout": (TimeoutError("timed out"), "retry", 120),
    "connection reset": (OSError("Connection reset by peer"), "retry", 120),
    "500": (reply(500, "Internal Server Error"), "retry", 120),
    "503": (reply(503, ""), "retry", 120),
    "html with 200": (Response(200, "<html>captive portal</html>"), "retry", 120),
    "empty 200": (Response(200, ""), "retry", 120),
    "a list": (Response(200, "[]"), "retry", 120),
    "the wrong shape": (reply(200, {"results": "nope"}), "retry", 120),
    "404": (reply(404, {"status": "NOT_FOUND", "message": "Not found"}), "ran", 180),
    "400": (reply(400, {"status": "ERROR", "error": "bad request"}), "ran", 180),
    "418": (reply(418, "{not json"), "ran", 180),
    "403": (reply(403, NOT_ENTITLED), "plan", None),
    "429": (reply(429, {"status": "ERROR", "error": "too many"}), "stop", 75),
    "401": (reply(401, {"status": "ERROR", "error": "Unknown API Key"}), "stop", None),
}


@pytest.mark.parametrize("answer,how,again", list(OUTCOMES.values()), ids=list(OUTCOMES))
def test_how_each_failure_is_handled(answer, how, again):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock, calls=100)
    server.hooks["news"] = once(answer)
    done = asyncio.run(spot.step(True))
    if how == "stop":  # 429 and 401: the step ends there, and nothing is marked
        assert [c.name for c in server.calls] == ["snapshot", "news"] and done == []
        assert "news" not in spot.ran and "news" not in spot.retry_at and "news" not in spot.errors
    else:  # the other jobs carry on
        assert done == [n for n in JOB_NAMES if n not in ("snapshot", "news")]
        assert len(server.calls) == len(JOB_NAMES)
        assert "news" not in spot.ok_at
    if how == "retry":
        assert "news" not in spot.ran and spot.retry_at["news"] == t0 + 120 and spot.errors["news"]
    elif how == "ran":
        assert spot.ran["news"] == t0 and "news" not in spot.retry_at and spot.errors["news"].startswith("HTTP 4")
    elif how == "plan":
        assert spot.ran["news"] == spot.not_in_plan["news"] == t0 and "news" not in spot.errors
    simulate(spot, clock, hours=0.2)
    asked = [c.t - t0 for c in server.of("news")]
    assert asked[0] == 0 and (asked[1] if len(asked) > 1 else None) == again
    if again is not None:
        assert "news" in spot.ok_at and "news" not in spot.errors and "news" not in spot.retry_at


def test_passing_failures_back_off_doubling_up_to_half_an_hour():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock, calls=1000)
    server.hooks["status"] = OSError("network is unreachable")
    simulate(spot, clock, hours=2)
    assert [c.t - t0 for c in server.of("status")] == [0, 120, 360, 840, 1800, 3600, 5400]
    assert spot.retry_at["status"] == t0 + 5400 + 1800 and spot._fails["status"] == 7
    assert "status" not in spot.ran and "status" not in spot.ok_at
    assert spot.errors["status"] == "OSError: network is unreachable"
    # the other jobs kept to their own intervals meanwhile
    assert [c.t - t0 for c in server.of("prev")] == [0, 900, 1800, 2700, 3600, 4500, 5400, 6300]
    # it comes back: everything about the failures is forgotten, and the job keeps its interval again
    server.hooks.clear()
    assert "status" in asyncio.run(spot.step(True))
    assert spot.ok_at["status"] == spot.ran["status"] == t0 + 7200
    for d in (spot.retry_at, spot._fails, spot.errors):
        assert "status" not in d
    simulate(spot, clock, hours=0.51)
    assert [c.t - t0 for c in server.of("status")][-3:] == [7200, 8100, 9000]


def test_a_403_for_another_job_keeps_its_data_and_asks_again_in_12_hours():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock)
    simulate(spot, clock, hours=0.1)
    daily = list(spot.daily)
    assert daily and "daily" in spot.ok_at
    server.hooks["daily"] = reply(403, TODAY_REFUSED)
    spot.ran.pop("daily")
    clock.t = t0 + 1000
    assert "daily" not in asyncio.run(spot.step(True))
    assert spot.not_in_plan["daily"] == spot.ran["daily"] == t0 + 1000
    assert "daily" not in spot.ok_at and "daily" not in spot.errors
    assert spot.daily == daily  # the bars it has are still good
    assert spot.plan() == "free plan (end of day)"
    clock.t = t0 + 1000 + PLAN_RETRY - 1
    assert "daily" not in [j.name for j in spot.due(True)]
    clock.t = t0 + 1000 + PLAN_RETRY
    server.hooks.clear()
    assert "daily" in asyncio.run(spot.step(True))
    assert "daily" not in spot.not_in_plan and spot.ok_at["daily"] == clock.t


# =====================================================================================================================
# Spotlight: hours of the refresh loop against the fake server
# =====================================================================================================================

def test_a_day_and_a_half_of_steps_keeps_the_budget_and_the_rules():
    """Monday 04:00 to Tuesday 10:00 New York, a step every 15 seconds, through closed, open and closed markets and a
    new session at midnight. Checked at the backend: what was asked, when, and how often."""
    start = ny(2026, 10, 5, 4, 0)
    clock = Clock(start)
    spot, server = desk(clock)
    for i in range(120):  # a story every 45 minutes, from 40 hours before the start
        server.add(story(i, start - 40 * HOUR + i * 2700, mood=("positive", "neutral", "negative")[i % 3]))
    fresh, news_runs = [], []

    def each(t, done):
        if "news" in done:
            news_runs.append(t)
        fresh.extend(spot.take_fresh_news())
    simulate(spot, clock, hours=30, each=each)
    calls = server.calls
    assert len(calls) > 300
    # the budget, at the wire
    assert_budget([c.t for c in calls])
    assert spot.massive.limiter.total == len(calls)
    # the key travels only as a header
    for c in calls:
        assert c.base == BASE and c.name is not None and "?" not in c.name
        assert c.headers == {"Authorization": f"Bearer {KEY}"}
        assert KEY not in c.url and "apikey" not in c.url.lower()
    # priorities
    assert [c.name for c in calls[:5]] == ["snapshot", "news", "prev", "status", "daily"]
    # the snapshot (not in the free plan) is probed every 12 hours, no more
    snaps = [c.t for c in server.of("snapshot")]
    assert snaps[0] == start and len(snaps) == 3
    assert all(b - a >= PLAN_RETRY for a, b in zip(snaps, snaps[1:]))
    # no request ever asks for today: the daily and indicator series end at the last finished session
    for c in server.of("daily"):
        begin, end = date.fromisoformat(c.match.group(2)), date.fromisoformat(c.match.group(3))
        assert end <= c.last_session < c.today
        assert begin == (datetime.fromtimestamp(c.t, timezone.utc) - timedelta(days=720)).date()
        assert c.params == {"adjusted": "true", "sort": "asc", "limit": "5000"}
    for name, (kind, window) in INDICATORS.items():
        runs = server.of(name)
        assert len(runs) >= 2, name
        for c in runs:
            lte = date.fromisoformat(c.params["timestamp.lte"])
            assert lte <= c.last_session < c.today
            assert c.params.get("window") == (str(window) if window else None)
            assert c.params["timespan"] == "day" and c.params["series_type"] == "close" and c.params["limit"] == "1"
    # minutes wait for the previous session's bar, then ask for exactly that session
    first_prev = calls.index(server.of("prev")[0])
    minutes = server.of("minutes")
    assert calls.index(minutes[0]) > first_prev
    for c in minutes:
        assert c.match.group(2) == c.match.group(3) <= str(c.last_session)
    # Tuesday's first prev brings Monday's bar, and everything built on it is asked again within minutes
    midnight = ny(2026, 10, 6, 0, 0)
    for name in AFTER_NEW_SESSION:
        again = [c for c in server.of(name) if midnight <= c.t <= midnight + 45 * 60]
        assert again, name
    assert any(c.match.group(3) == "2026-10-05" for c in server.of("daily") if c.t >= midnight)
    # each job keeps to its interval (a new session's bar may bring the session jobs forward)
    for name, every_open, every_closed in (("news", 180, 600), ("prev", 900, 1800), ("status", 900, 3600)):
        times = [c.t for c in server.of(name)]
        for a, b in zip(times, times[1:]):
            assert b - a >= (every_open if is_open(b) else every_closed), (name, a, b)
    daily = server.of("daily")
    for a, b in zip(daily, daily[1:]):
        assert b.t - a.t >= 6 * HOUR or b.match.group(3) != a.match.group(3)
    # in the end: everything but the paid snapshot loaded, up to Monday's session
    assert spot.errors == {} and spot.not_in_plan.keys() == {"snapshot"}
    assert set(spot.ok_at) == set(JOB_NAMES) - {"snapshot"}
    assert spot.plan() == "free plan (end of day)"
    monday = session_ms(date(2026, 10, 5))
    assert spot.prev["t"] == monday and spot.daily[-1]["t"] == monday and spot.last_session() == "2026-10-05"
    assert len(spot.daily) > 480 and all(a["t"] < b["t"] for a, b in zip(spot.daily, spot.daily[1:]))
    assert len(spot.minutes) == 390 and spot.minutes[0]["t"] == ms(ny(2026, 10, 5, 9, 30))
    assert set(spot.indicators) == set(INDICATORS)
    assert spot.indicators["macd"]["signal"] == 1.75 and spot.indicators["rsi14"]["value"] == 58.31
    assert spot.details["market_cap"] == 4.43e12 and len(spot.dividends) == 3 and len(spot.splits) == 2
    assert spot.related == RELATED[:10] and spot.status["market"] == "open"
    # news: nothing from the first load is an alert; every later story is, once
    first_load = news_runs[0]
    expected = [s["id"] for at, s in sorted(server.stories, key=lambda p: p[0]) if first_load < at <= news_runs[-1]]
    assert ids(fresh) == expected and len(expected) > 30
    assert len(spot.news) == MAX_NEWS and ids(spot.news) == sorted(ids(spot.news), reverse=True)


def test_a_paid_key_gets_the_delayed_snapshot_every_minute_while_open():
    start = ny(2026, 10, 5, 8, 0)
    clock = Clock(start)
    spot, server = desk(clock, paid=True)
    simulate(spot, clock, hours=12)
    assert_budget([c.t for c in server.calls])
    snaps = [c.t for c in server.of("snapshot")]
    for a, b in zip(snaps, snaps[1:]):
        assert b - a >= (60 if is_open(b) else 900)
    during = [t for t in snaps if is_open(t)]
    assert len(during) > 300  # about one a minute for six and a half hours
    assert spot.plan() == "paid plan (delayed snapshot available)" and not spot.not_in_plan
    assert spot.snapshot["lastTrade"]["p"] > 0 and spot.errors == {}


@pytest.mark.parametrize("seed", range(6))
def test_random_failures_never_break_the_budget_or_the_loop(seed):
    rng = random.Random(seed)
    start = ny(2026, 10, 6, 6, 0)
    clock = Clock(start)
    spot, server = desk(clock)
    for i in range(60):
        server.add(story(i, start - 20 * HOUR + i * 1800))
    chaos_until = start + 10 * HOUR
    answers = [
        lambda: reply(500, "Internal Server Error"), lambda: reply(502, "<html>Bad gateway</html>"),
        lambda: reply(503, ""), lambda: OSError("Connection reset by peer"), lambda: TimeoutError("timed out"),
        lambda: reply(429, {"status": "ERROR", "error": "exceeded the maximum requests per minute"}),
        lambda: Response(200, "<html>captive portal</html>"), lambda: Response(200, "[]"),
        lambda: Response(200, '{"results": [1, 2'), lambda: Response(200, '{"results": "nope"}'),
        lambda: Response(200, '{"results": [{"bad": true}], "ticker": null}'),
        lambda: reply(404, {"status": "NOT_FOUND", "message": "Not found"}),
        lambda: reply(400, {"status": "ERROR", "error": "bad request"}),
    ]

    def chaos(call):
        if call.t < chaos_until and rng.random() < 0.15:
            return rng.choice(answers)()
        return None
    server.chaos = chaos
    statuses = []
    real_get = server.get

    async def recording_get(url, headers, timeout, proxy=None):
        n = len(server.calls)
        try:
            resp = await real_get(url, headers, timeout, proxy)
        except Exception:
            statuses.append((server.calls[n].t, None))
            raise
        statuses.append((server.calls[n].t, resp.status))
        return resp
    server.get = recording_get
    simulate(spot, clock, hours=14)  # ten hours of trouble, then four quiet ones
    assert_budget([c.t for c in server.calls])
    assert spot.massive.limiter.total == len(server.calls) and spot.enabled
    for t, status in statuses:
        if status == 429:
            later = [c.t for c in server.calls if c.t > t]
            assert not later or later[0] >= t + WINDOW
    # once the trouble is over the frequent jobs recover by themselves
    end = clock.t
    for name in ("news", "prev", "status"):
        assert end - spot.ok_at[name] < HOUR, name
        assert name not in spot.errors


def test_two_steppers_at_once_still_share_one_budget():
    clock = Clock(ny(2026, 10, 6, 9, 0))
    spot, server = desk(clock, paid=True)

    async def scenario():
        for _ in range(4 * 60 * 3):  # three hours
            await asyncio.gather(spot.step(is_open(clock.t)), spot.step(is_open(clock.t)), spot.step(True))
            clock.t += 15
    asyncio.run(scenario())
    assert len(server.calls) > 100
    assert_budget([c.t for c in server.calls])


def test_a_new_session_bar_makes_the_session_jobs_due_again():
    clock = Clock(ny(2026, 10, 6, 12, 0))  # Tuesday: the last session is Monday's
    spot, server = desk(clock)
    simulate(spot, clock, hours=0.25)
    assert set(spot.ok_at) == set(JOB_NAMES) - {"snapshot"}
    assert spot.last_session() == "2026-10-05"
    # the same bar again changes nothing
    spot.ran.pop("prev")
    assert "prev" in asyncio.run(spot.step(True))
    assert not [j.name for j in spot.due(True) if j.name in AFTER_NEW_SESSION]
    # Wednesday 00:30: Tuesday's bar is out
    clock.t = ny(2026, 10, 7, 0, 30)
    before = len(server.calls)
    done = asyncio.run(spot.step(False))
    assert done[0] == "news" and "prev" in done and spot.last_session() == "2026-10-06"
    still_due = {j.name for j in spot.due(False)}
    assert set(AFTER_NEW_SESSION) <= still_due | set(done)
    simulate(spot, clock, hours=0.1)
    asked = server.calls[before:]
    for name in AFTER_NEW_SESSION:
        (c,) = [c for c in asked if c.name == name]
        if name == "daily":
            assert c.match.group(3) == "2026-10-06"
        elif name == "minutes":
            assert c.match.group(2) == c.match.group(3) == "2026-10-06"
        else:
            assert c.params["timestamp.lte"] == "2026-10-06"
    assert spot.daily[-1]["t"] == session_ms(date(2026, 10, 6)) and spot.errors == {}
    assert spot.minutes[0]["t"] == ms(ny(2026, 10, 6, 9, 30))
    # details, dividends, splits and related don't depend on the session: not asked again
    assert not [c for c in asked if c.name in ("details", "dividends", "splits", "related")]


def test_minutes_wait_for_the_previous_session_bar():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    server.prev_bar = "none"
    simulate(spot, clock, hours=0.1)
    assert server.of("minutes") == []  # never asked without a session to ask for
    assert spot.errors["minutes"] == "waiting for the previous session's bar"
    assert "minutes" not in spot.ok_at and spot.prev is None
    assert spot.massive.limiter.total == len(server.calls)  # the wait took no slot
    (daily,) = server.of("daily")
    assert daily.match.group(3) == "2026-10-05"  # yesterday, without a bar
    server.prev_bar = None
    clock.t += 3600
    simulate(spot, clock, hours=0.1)
    (minutes,) = server.of("minutes")
    assert minutes.match.group(2) == minutes.match.group(3) == "2026-10-05"
    assert "minutes" not in spot.errors and len(spot.minutes) == 390


def test_last_session_is_the_previous_bar_or_yesterday():
    clock = Clock(ny(2026, 10, 5, 10, 0))  # Monday
    spot = Spotlight(None, clock=clock)
    assert spot.last_session() == "2026-10-04"  # no bar yet: yesterday (a Sunday, so nothing is asked for today)
    spot.prev = {"t": session_ms(date(2026, 10, 2)), "c": 1.0}
    assert spot.last_session() == "2026-10-02"
    for t in (0, None, "1759377600000", True, float("nan")):
        spot.prev = {"t": t}
        assert spot.last_session() == "2026-10-04", t
    for hour in range(24):
        for minute in (0, 59):
            clock.t = ny(2026, 10, 6, hour, minute)
            assert Spotlight(None, clock=clock).last_session() == "2026-10-05"
    for when, day in (((2026, 3, 9, 0, 30), "2026-03-08"),  # the day after the clocks went forward
                      ((2026, 3, 8, 23, 30), "2026-03-07"),  # the 23-hour day itself
                      ((2026, 1, 1, 0, 0), "2025-12-31")):
        clock.t = ny(*when)
        assert Spotlight(None, clock=clock).last_session() == day, when


def test_last_session_is_never_today_on_the_25_hour_day():
    """Sunday 2026-11-01 has 25 hours in New York. 'Now minus 24 hours' would still be that Sunday late in the day."""
    for hour, minute in ((0, 30), (1, 30), (12, 0), (22, 59), (23, 0), (23, 30), (23, 59)):
        clock = Clock(ny(2026, 11, 1, hour, minute))
        spot, server = desk(clock)
        server.prev_bar = "none"
        assert spot.last_session() == "2026-10-31", (hour, minute)
        simulate(spot, clock, hours=0.1)
        asked = server.of("daily") + [c for name in INDICATORS for c in server.of(name)]
        assert len(asked) == 1 + len(INDICATORS), (hour, minute)
        for c in asked:
            end = c.match.group(3) if c.name == "daily" else c.params["timestamp.lte"]
            # the calendar day before the server's today (at 23:59 the indicators are asked after midnight)
            assert end == (c.today - timedelta(days=1)).isoformat() < str(c.today), (hour, minute, c.name)


def test_losing_the_paid_snapshot_is_noticed():
    clock = Clock(ny(2026, 10, 6, 10, 0))
    spot, server = desk(clock, paid=True)
    simulate(spot, clock, hours=0.1)
    assert spot.plan() == "paid plan (delayed snapshot available)" and spot.snapshot and spot.snapshot_fresh()
    q = quote("NVDA", price=180.0, change=1.0)
    assert "15-min delayed" in E.nvidia_board(q, spot).description
    server.paid = False  # the trial ended, or the key was swapped for a free one
    simulate(spot, clock, hours=0.1)
    assert "snapshot" in spot.not_in_plan  # Massive said so
    assert spot.snapshot is None and "snapshot" not in spot.ok_at and not spot.snapshot_fresh()
    assert spot.plan() == "free plan (end of day)"
    board = E.nvidia_board(q, spot)
    assert "15-min delayed" not in board.description
    assert board.footer.text.startswith("Live price: Yahoo Finance · Massive (free plan (end of day)): ")


def test_a_rate_limited_job_is_asked_again_after_the_back_off():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    server.hooks["related"] = once(reply(429, {"status": "ERROR", "error": "exceeded the maximum requests per minute"}))
    simulate(spot, clock, hours=1)
    first, second = server.of("related")  # asked twice in the hour, not once a week
    assert WINDOW <= second.t - first.t <= WINDOW + 15  # the first step after the minute's pause
    assert spot.related == RELATED[:10] and "related" not in spot.errors and spot.ok_at["related"] == second.t
    assert_budget([c.t for c in server.calls])


def test_a_garbled_answer_does_not_wipe_good_data():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    simulate(spot, clock, hours=0.1)
    names = ("daily", "details", "dividends", "splits", "related")
    before = {name: getattr(spot, name) for name in names}
    ok_before = {name: spot.ok_at[name] for name in names}
    assert all(before.values())
    html = Response(200, "<html><body><h1>Service temporarily unavailable</h1></body></html>")
    for name in names:
        server.hooks[name] = html
        spot.ran.pop(name)
    t1 = clock.t
    simulate(spot, clock, hours=0.1)
    assert {name: getattr(spot, name) for name in names} == before
    assert {name: spot.ok_at[name] for name in names} == ok_before  # not counted as fresh
    for name in names:
        assert spot.errors[name] == "Massive sent an unreadable answer (HTTP 200)"
        assert name not in spot.ran and spot.retry_at[name] > t1  # tried again soon, not after a day or a week


# =====================================================================================================================
# Spotlight: news
# =====================================================================================================================

def test_news_first_load_is_not_fresh_and_later_stories_are():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    now = clock.t
    for i in range(25):  # one an hour, the newest an hour ago
        server.add(story(i, now - (25 - i) * HOUR))
    asyncio.run(spot.step(True))
    assert len(spot.news) == 20 and spot.news[0]["id"] == "nvda-story-0024"
    assert ids(spot.news) == sorted(ids(spot.news), reverse=True)
    assert spot.fresh_news == [] and spot.take_fresh_news() == []
    clock.t += 200
    now = clock.t
    server.add(story(100, now - 120, mood="negative"))
    server.add(story(101, now - 60))
    server.add(story(102, now - 7 * HOUR), visible_at=now - 1)  # published 7 hours ago, only now in the feed
    server.add(story(103, now - 6 * HOUR), visible_at=now - 1)  # exactly 6 hours old: still news
    server.add(story(104, now - 6 * HOUR - 1), visible_at=now - 1)  # a second older: not
    server.add(story(24, now - HOUR))  # a story it already has
    assert "news" in asyncio.run(spot.step(True))
    fresh = spot.take_fresh_news()
    assert ids(fresh) == ["nvda-story-0103", "nvda-story-0100", "nvda-story-0101"]  # oldest first
    assert spot.take_fresh_news() == []
    assert {"nvda-story-0102", "nvda-story-0104"} <= set(ids(spot.news))  # kept for the board, just no alert
    assert len(ids(spot.news)) == len(set(ids(spot.news)))
    clock.t += 200
    asyncio.run(spot.step(True))  # the same feed again: nothing new
    assert spot.take_fresh_news() == []


def test_news_is_capped_and_newest_first():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    for i in range(20):
        server.add(story(i, clock.t - (20 - i) * 60))
    asyncio.run(spot.step(True))
    for batch in range(4):
        clock.t += 200
        for j in range(15):
            server.add(story(1000 + batch * 15 + j, clock.t - 100 + j))
        asyncio.run(spot.step(True))
        assert len(spot.news) <= MAX_NEWS
    assert len(spot.news) == MAX_NEWS
    assert ids(spot.news) == [f"nvda-story-{1059 - k:04d}" for k in range(MAX_NEWS)]
    assert len(spot.take_fresh_news()) == 60


def test_news_without_an_id_is_keyed_by_its_link_and_anonymous_stories_are_skipped():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    server.add(story(1, clock.t - 600))
    asyncio.run(spot.step(True))
    clock.t += 200
    linked = story(2, clock.t - 100)
    del linked["id"]
    anonymous = story(3, clock.t - 90)
    del anonymous["id"], anonymous["article_url"]
    server.add(linked)
    server.add(dict(linked))  # the same link twice
    server.add(anonymous)
    asyncio.run(spot.step(True))
    fresh = spot.take_fresh_news()
    assert [n.get("article_url") for n in fresh] == ["https://www.benzinga.com/news/nvda/2"]
    assert len(spot.news) == 2


def test_an_empty_first_feed_keeps_the_next_one_quiet():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    asyncio.run(spot.step(True))  # nothing published yet
    for i in range(5):
        server.add(story(i, clock.t - 3600 + i))
    clock.t += 200
    asyncio.run(spot.step(True))
    assert len(spot.news) == 5 and spot.take_fresh_news() == []  # the backlog isn't news


def test_the_seen_list_is_trimmed_to_the_kept_stories():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    spot._seen_news = {f"old-{i}" for i in range(2000)}
    server.add(story(1, clock.t - 60))
    asyncio.run(spot.step(True))
    assert spot._seen_news == {"nvda-story-0001"}
    assert ids(spot.take_fresh_news()) == ["nvda-story-0001"]


def test_sentiment_and_news_mood():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot = Spotlight(None, clock=clock)
    item = story(1, clock.t)
    assert spot.sentiment(item) == ("positive", "Story 1 reads positive for NVDA.")
    assert spot.sentiment({"insights": [{"ticker": "NVDA", "sentiment": "NEGATIVE"}]}) == ("negative", "")
    assert spot.sentiment({"insights": [{"ticker": "AMD", "sentiment": "positive"}]}) == ("", "")
    assert spot.sentiment({"insights": None}) == ("", "") and spot.sentiment({}) == ("", "")
    assert spot.sentiment({"insights": [{"ticker": "NVDA", "sentiment": None}]}) == ("", "")
    other = Spotlight(None, symbol="AMD", clock=clock)
    assert other.sentiment(item) == ("negative", "Losing share.")
    now = clock.t
    spot.news = [story(1, now - HOUR, "positive"), story(2, now - 2 * HOUR, "positive"),
                 story(3, now - 3 * HOUR, "neutral"), story(4, now - 47 * HOUR, "negative"),
                 story(5, now - 49 * HOUR, "negative"), story(6, now - HOUR, "mixed"),
                 {"id": "x", "published_utc": "garbage", "insights": [{"ticker": "NVDA", "sentiment": "positive"}]},
                 {"id": "y", "insights": [{"ticker": "NVDA", "sentiment": "positive"}]}]
    assert spot.news_mood() == (2, 1, 1)
    assert spot.news_mood(hours=2.5) == (2, 0, 0)
    assert spot.news_mood(hours=100) == (2, 1, 2)
    assert Spotlight(None, clock=clock).news_mood() == (0, 0, 0)


def test_range_52w_and_average_volume():
    spot = Spotlight(None, clock=Clock(ny(2026, 10, 6, 12, 0)))
    assert spot.range_52w() is None and spot.avg_volume() is None
    spot.daily = [{"t": i, "c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "v": 1000.0 * (i + 1)} for i in range(300)]
    assert spot.range_52w() == (99.0 + 48, 101.0 + 299)  # the last 252 bars only
    assert spot.avg_volume() == sum(1000.0 * (i + 1) for i in range(250, 300)) / 50
    assert spot.avg_volume(days=1) == 300_000.0
    spot.daily = [{"c": 1.0, "h": 2.0, "l": 0.5}, {"c": 1.0, "h": 3.0, "l": 0.7, "v": 10}]
    assert spot.range_52w() == (0.5, 3.0) and spot.avg_volume() == 5.0


def test_plan_strings():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    assert Spotlight(None, clock=clock).plan() == "no key"
    spot, _ = desk(clock)
    assert spot.plan() == "plan being checked"
    spot.ok_at["snapshot"] = clock.t
    assert spot.plan() == "paid plan (delayed snapshot available)"
    spot.not_in_plan["snapshot"] = clock.t  # Massive's 403 wins over an old success
    assert spot.plan() == "free plan (end of day)"
    spot.ok_at.clear()
    assert spot.plan() == "free plan (end of day)"
    spot.not_in_plan = {"daily": clock.t}  # another job outside the plan says nothing about the snapshot
    assert spot.plan() == "plan being checked"
    spot.not_in_plan["snapshot"] = clock.t
    spot.massive.key_rejected = True
    assert spot.plan() == "key rejected"
    assert desk(clock, key="")[0].plan() == "no key"
    assert desk(clock, key=None)[0].plan() == "no key"


def test_snapshot_fresh_needs_a_usable_key_a_snapshot_and_a_recent_success():
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, _ = desk(clock)
    assert not spot.snapshot_fresh()
    spot.snapshot = {"lastTrade": {"p": 181.0}}
    assert not spot.snapshot_fresh()  # it never worked
    for age, fresh in ((0, True), (1799.9, True), (1800, False), (86400, False)):
        spot.ok_at["snapshot"] = clock.t - age
        assert spot.snapshot_fresh() is fresh, age
    spot.ok_at["snapshot"] = clock.t
    spot.not_in_plan["snapshot"] = clock.t
    assert not spot.snapshot_fresh()
    del spot.not_in_plan["snapshot"]
    spot.snapshot = {}
    assert not spot.snapshot_fresh()
    spot.snapshot = {"lastTrade": {"p": 181.0}}
    assert spot.snapshot_fresh()
    spot.massive.key_rejected = True
    assert not spot.snapshot_fresh()
    stale = Spotlight(None, clock=clock)
    stale.snapshot, stale.ok_at = {"lastTrade": {"p": 181.0}}, {"snapshot": clock.t}
    assert not stale.snapshot_fresh()  # no Massive at all


_BARS = [day_bar(d) for d in trading_days(date(2026, 9, 1), date(2026, 10, 5))]
MALFORMED = {  # (job, its answer, what the spotlight keeps)
    "a daily bar without high and low": (
        "daily", {"results": _BARS[:3] + [{"c": 170.0, "t": _BARS[3]["t"], "v": 1}] + _BARS[4:], "status": "OK"},
        lambda spot: spot.daily == _BARS[:3] + _BARS[4:] and "daily" in spot.ok_at),
    "details as a list": (
        "details", {"results": [{"ticker": "NVDA", "market_cap": 4.4e12}], "status": "OK"},
        lambda spot: spot.details is None and spot.errors["details"] == "unexpected company details answer"),
    "dividends as text": (
        "dividends", {"results": ["0.01 on 2026-09-11"], "status": "OK"},
        lambda spot: spot.dividends == [] and "dividends" in spot.ok_at),
    "a dividend without an amount": (
        "dividends", {"results": [{"cash_amount": None, "ex_dividend_date": "2026-09-11", "pay_date": "2026-10-02"}],
                      "status": "OK"},
        lambda spot: spot.dividends == []),
    "an indicator value as a bare number": (
        "sma50", {"results": {"values": [181.5]}, "status": "OK"},
        lambda spot: "sma50" not in spot.indicators and spot.errors["sma50"] == "no sma50 value in the answer"),
    "a story with an empty insight": (
        "news", {"results": [story(1, ny(2026, 10, 6, 11, 0), insights=[None])], "status": "OK"},
        lambda spot: [n["insights"] for n in spot.news] == [[]]),
    "a snapshot that is text": (
        "snapshot", {"ticker": "NVDA", "status": "OK"},
        lambda spot: spot.snapshot is None and spot.errors["snapshot"] == "unexpected snapshot answer"),
}


@pytest.mark.parametrize("job,body,kept", list(MALFORMED.values()), ids=list(MALFORMED))
def test_the_board_survives_malformed_answers(job, body, kept):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock)
    server.hooks[job] = reply(200, body)
    simulate(spot, clock, hours=0.05)
    assert server.of(job)
    assert kept(spot)
    assert_fits(E.nvidia_board(quote("NVDA", price=180.0), spot))
    assert_fits(E.nvidia_board(None, spot))


NAN = float("nan")
GOOD_BAR = {"o": 180.0, "h": 182.5, "l": 178.25, "c": 181.0, "t": session_ms(date(2026, 10, 5))}
# (job, what Massive sends, what is kept: an attribute and its value, or an error when nothing usable came)
VALIDATED = {
    "prev without a close": ("prev", {"results": [dict(GOOD_BAR, c=None)]}, "no previous-day bar in the answer"),
    "prev with text": ("prev", {"results": [dict(GOOD_BAR, o="180")]}, "no previous-day bar in the answer"),
    "prev as a list of lists": ("prev", {"results": [[1, 2, 3]]}, "no previous-day bar in the answer"),
    "prev results as text": ("prev", {"results": "none"}, "no previous-day bar in the answer"),
    "prev keeps numbers only": ("prev", {"results": [dict(GOOD_BAR, v="many", vw=181.1, n=True, T="NVDA")]},
                                ("prev", dict(GOOD_BAR, vw=181.1))),
    "daily with odd bars": ("daily", {"results": [GOOD_BAR, dict(GOOD_BAR, h=None), dict(GOOD_BAR, t="x"),
                                                  dict(GOOD_BAR, l=True), dict(GOOD_BAR, c=NAN), "bar", None, 5,
                                                  dict(GOOD_BAR, t=GOOD_BAR["t"] + 1, v=10, extra="kept?")]},
                            ("daily", [GOOD_BAR, dict(GOOD_BAR, t=GOOD_BAR["t"] + 1, v=10)])),
    "daily with no good bar": ("daily", {"results": [dict(GOOD_BAR, o=None)]}, "no daily bars in the answer"),
    "daily without results": ("daily", {"status": "OK"}, "no daily bars in the answer"),
    "daily results as an object": ("daily", {"results": {"o": 1}}, "unexpected answer (no bars)"),
    "dividends": ("dividends", {"results": [{"cash_amount": 0.25, "pay_date": "2026-10-02"}, {"cash_amount": "0.25"},
                                            {"cash_amount": True}, {"pay_date": "x"}, "0.25", None]},
                  ("dividends", [{"cash_amount": 0.25, "pay_date": "2026-10-02"}])),
    "dividends as an object": ("dividends", {"results": {"cash_amount": 0.25}}, "unexpected dividends answer"),
    "splits": ("splits", {"results": [{"split_from": 1, "split_to": 4}, {"split_from": "1", "split_to": 10},
                                      {"split_from": 1}, {"split_from": 1, "split_to": None}, ["x"]]},
               ("splits", [{"split_from": 1, "split_to": 4}])),
    "splits as text": ("splits", {"results": "1-for-10"}, "unexpected splits answer"),
    "related": ("related", {"results": [{"ticker": "AMD"}, {"ticker": 5}, "TSM", {"ticker": None}, {},
                                        {"ticker": "INTC"}]}, ("related", ["AMD", "INTC"])),
    "related as an object": ("related", {"results": {"ticker": "AMD"}}, "unexpected related companies answer"),
    "news": ("news", {"results": [{"id": "a", "title": "Fine", "article_url": "https://x.example/a",
                                   "published_utc": "2026-10-06T15:00:00Z", "insights": [{"ticker": "NVDA"}]},
                                  {"id": "b", "title": 5}, {"id": "c"},
                                  {"id": "d", "title": "Odd", "article_url": 7, "published_utc": None,
                                   "insights": [None, "x", {"ticker": "NVDA", "sentiment": "positive"}]},
                                  {"id": "e", "title": "Odder", "insights": {"ticker": "NVDA"}}]},
             ("news", [{"id": "a", "title": "Fine", "article_url": "https://x.example/a",
                        "published_utc": "2026-10-06T15:00:00Z", "insights": [{"ticker": "NVDA"}]},
                       {"id": "d", "title": "Odd", "article_url": "", "published_utc": "",
                        "insights": [{"ticker": "NVDA", "sentiment": "positive"}]},
                       {"id": "e", "title": "Odder", "article_url": "", "published_utc": "", "insights": []}])),
    "news as an object": ("news", {"results": {"title": "x"}}, "unexpected news answer"),
    "an indicator as text": ("rsi14", {"results": {"values": [{"value": "58.3", "timestamp": 1}]}},
                             "no rsi14 value in the answer"),
    "an indicator without values": ("ema20", {"results": {"values": []}}, "no ema20 value in the answer"),
    "an indicator's results as a list": ("sma200", {"results": [{"value": 1.0}]}, "no sma200 value in the answer"),
    "macd keeps its numbers": ("macd", {"results": {"values": [{"value": 2.5, "signal": "x", "histogram": 0.75,
                                                                "timestamp": 7, "note": "hi"}]}},
                               ("indicators", {"macd": {"value": 2.5, "histogram": 0.75, "timestamp": 7.0}})),
    "details as text": ("details", {"results": "Nvidia"}, "unexpected company details answer"),
    "a snapshot as a list": ("snapshot", {"ticker": [1]}, "unexpected snapshot answer"),
}


def kept_by(spot, job):
    """What a job's answers are kept in."""
    return spot.indicators.get(job) if job in INDICATORS else getattr(spot, job)


@pytest.mark.parametrize("job,answer,expected", list(VALIDATED.values()), ids=list(VALIDATED))
def test_answers_are_checked_before_they_are_kept(job, answer, expected):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock, calls=100, paid=True)
    server.hooks[job] = reply(200, answer)
    # what a good earlier answer left behind (the previous bar is the session before)
    spot.prev = dict(GOOD_BAR, c=1.0, t=session_ms(date(2026, 10, 2)))
    spot.daily, spot.details = [dict(GOOD_BAR, c=2.0)], {"name": "Nvidia Corp"}
    spot.snapshot, spot.dividends, spot.splits = {"lastTrade": {"p": 3.0}}, [{"cash_amount": 0.01}], [
        {"split_from": 1, "split_to": 10}]
    spot.related, spot.indicators = ["OLD"], {k: {"value": 4.0} for k in INDICATORS}
    spot.news = [{"id": "old", "title": "Old", "article_url": "", "published_utc": "", "insights": []}]
    spot._seen_news = {"old"}
    before = kept_by(spot, job)
    asyncio.run(spot.step(True))
    assert len(server.of(job)) == 1
    if isinstance(expected, str):  # nothing usable: what it had stays, and the job is tried again soon
        assert spot.errors[job] == expected and job not in spot.ok_at and job not in spot.ran
        assert spot.retry_at[job] == t0 + 120
        assert kept_by(spot, job) == before
    else:
        name, value = expected
        assert job in spot.ok_at and job not in spot.errors and job not in spot.retry_at
        got = getattr(spot, name)
        if name == "indicators":
            got = {job: got[job]}
        elif name == "news":
            got = [n for n in got if n["id"] != "old"]
        assert got == value
    assert_fits(E.nvidia_board(quote("NVDA", price=180.0), spot))
    assert_fits(E.nvidia_board(None, spot))


# =====================================================================================================================
# Spotlight: saving
# =====================================================================================================================

STATE_KEYS = {"symbol", "prev", "snapshot", "daily", "minutes", "indicators", "details", "news", "dividends", "splits",
              "related", "status", "ran", "ok_at", "not_in_plan", "seen", "recent_requests"}


def restarted(server, path, clock, mono_offset=5_000_000.0):
    """The bot after a restart: a new Http, Massive and limiter (with a new monotonic clock), the same file."""
    massive = Massive(KEY, Http(server, sleep=no_retry_sleep), limiter_on(clock, mono_offset=mono_offset))
    return Spotlight(massive, path, clock=clock)


def test_spotlight_state_survives_a_restart(tmp_path):
    path = tmp_path / "nvidia.json"
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock, path=path)
    for i in range(10):
        server.add(story(i, clock.t - (10 - i) * 600))
    simulate(spot, clock, hours=0.2)
    text = path.read_text()
    state = json.loads(text)
    assert KEY not in text and state["symbol"] == "NVDA" and set(state) == STATE_KEYS
    again = restarted(server, path, clock)
    for name in ("prev", "daily", "minutes", "indicators", "details", "news", "dividends", "splits", "related",
                 "status", "ran", "ok_at", "not_in_plan", "snapshot"):
        assert getattr(again, name) == getattr(spot, name), name
    # a restart asks nothing again by itself: the snapshot keeps its 12-hour wait for the free plan
    assert again.not_in_plan.keys() == {"snapshot"} and again.plan() == "free plan (end of day)"
    assert [j.name for j in again.due(True)] == [j.name for j in spot.due(True)]
    assert "snapshot" not in [j.name for j in again.due(True)]
    assert again.state()["seen"] == spot.state()["seen"]
    assert again.errors == again.retry_at == {}  # passing troubles aren't saved
    # stories seen before the restart aren't alerted again; a new one is
    clock.t += 700
    server.add(story(500, clock.t - 30))
    asyncio.run(again.step(False))
    assert ids(again.take_fresh_news()) == ["nvda-story-0500"]
    assert json.loads(path.read_text())["news"][0]["id"] == "nvda-story-0500"


def test_a_restart_keeps_the_minute_budget(tmp_path):
    path = tmp_path / "nvidia.json"
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock, path=path)
    assert asyncio.run(spot.step(True)) == ["news", "prev", "status", "daily"]
    assert json.loads(path.read_text())["recent_requests"] == [t0] * 5  # wall-clock times
    clock.t = t0 + 20  # the bot restarts 20 seconds later
    again = restarted(server, path, clock)
    assert again.massive.limiter.used() == 5 and again.massive.limiter.wait_time() == 41.0
    assert asyncio.run(again.step(True)) == [] and len(server.calls) == 5  # the five requests before still count
    clock.t = t0 + 60
    assert asyncio.run(again.step(True)) == [] and len(server.calls) == 5
    clock.t = t0 + 61
    assert asyncio.run(again.step(True)) == ["minutes", "sma50", "sma200", "ema20", "rsi14"]
    assert_budget([c.t for c in server.calls])
    # a restart after the window: the old requests no longer count
    clock.t = t0 + 61 + 61
    third = restarted(server, path, clock, mono_offset=123.0)
    assert third.massive.limiter.used() == 0
    assert asyncio.run(third.step(True)) == ["macd", "details", "dividends", "splits", "related"]
    assert_budget([c.t for c in server.calls])


def test_spotlight_saves_whenever_requests_were_made(tmp_path):
    path = tmp_path / "nvidia.json"
    clock = Clock(ny(2026, 10, 6, 12, 0))
    t0 = clock.t
    spot, server = desk(clock, path=path)
    for name in JOB_NAMES:
        server.hooks[name] = OSError("down")
    assert asyncio.run(spot.step(True)) == []
    # nothing worked, but five requests went out: the file counts them for a restart
    state = json.loads(path.read_text())
    assert state["recent_requests"] == [t0] * 5 and state["ran"] == state["ok_at"] == state["not_in_plan"] == {}
    assert spot.retry_at == {n: t0 + 120 for n in ("snapshot", "news", "prev", "status", "daily", "minutes")}
    assert spot.errors["minutes"] == "waiting for the previous session's bar"  # no request for that one
    # a step that sends nothing and changes nothing writes nothing
    path.unlink()
    clock.t = t0 + 15
    assert asyncio.run(spot.step(True)) == [] and len(server.calls) == 5
    assert not path.exists()
    # failures again: written again
    clock.t = t0 + 75
    assert asyncio.run(spot.step(True)) == [] and len(server.calls) == 10
    assert json.loads(path.read_text())["recent_requests"] == [t0 + 75] * 5
    assert [c.name for c in server.calls[5:]] == ["sma50", "sma200", "ema20", "rsi14", "macd"]
    # and of course after a success
    server.hooks.clear()
    path.unlink()
    clock.t = t0 + 150
    assert asyncio.run(spot.step(True)) == ["news", "prev", "status", "daily"]
    state = json.loads(path.read_text())
    assert state["ok_at"] == {n: t0 + 150 for n in ("news", "prev", "status", "daily")}
    assert state["not_in_plan"] == {"snapshot": t0 + 150}


def test_a_save_that_fails_is_only_logged(tmp_path, monkeypatch):
    from marketbot import massive as massive_module

    def full_disk(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(massive_module, "write_json", full_disk)
    path = tmp_path / "nvidia.json"
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock, path=path)
    assert asyncio.run(spot.step(True)) == ["news", "prev", "status", "daily"]
    assert not path.exists() and not spot._dirty
    Spotlight(None, None).save()  # no path: nothing to do


TOLERATED = {
    "invalid json": "{not json",
    "empty file": "",
    "a list": "[1, 2, 3]",
    "a string": '"NVDA"',
    "another symbol": json.dumps({"symbol": "AMD", "prev": {"t": 1, "c": 5.0}, "news": [{"id": "amd-1"}],
                                  "ran": {"news": 1e18}, "recent_requests": [ny(2026, 10, 6, 11, 59, 50)] * 5}),
    "no symbol": json.dumps({"prev": {"t": 1, "c": 5.0}, "ran": {"news": 1e18},
                             "recent_requests": [ny(2026, 10, 6, 11, 59, 50)] * 5}),
    "wrong types": json.dumps({"symbol": "NVDA", "prev": [1], "snapshot": "x", "daily": {"a": 1}, "minutes": 5,
                               "indicators": [], "details": [], "news": {}, "dividends": "x", "splits": None,
                               "related": {}, "status": 3, "ran": [], "ok_at": "x", "not_in_plan": 7, "seen": None,
                               "recent_requests": {"t": ny(2026, 10, 6, 11, 59, 50)}}),
}


@pytest.mark.parametrize("content", list(TOLERATED.values()), ids=list(TOLERATED))
def test_a_corrupt_or_foreign_state_file_is_ignored(tmp_path, content):
    path = tmp_path / "nvidia.json"
    path.write_text(content)
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock, path=path)
    assert spot.prev is None and spot.snapshot is None and spot.details is None and spot.status is None
    assert spot.daily == spot.minutes == spot.news == spot.dividends == spot.splits == spot.related == []
    assert spot.indicators == spot.ran == spot.ok_at == spot.not_in_plan == {}
    assert spot._seen_news == set() and spot.massive.limiter.used() == 0
    assert [j.name for j in spot.due(True)] == JOB_NAMES
    assert asyncio.run(spot.step(True)) == ["news", "prev", "status", "daily"]
    assert_fits(E.nvidia_board(quote("NVDA", price=180.0), spot))
    assert json.loads(path.read_text())["symbol"] == "NVDA"  # replaced by a good file


def test_a_damaged_state_file_is_set_aside_not_deleted(tmp_path):
    path = tmp_path / "nvidia.json"
    path.write_bytes(b"\xff\xfe{garbage")
    Spotlight(None, path, clock=Clock(ny(2026, 10, 6, 12, 0)))
    assert not path.exists() and len(list(tmp_path.glob("nvidia.json.damaged-*"))) == 1


BAD_INSIDE = {  # (what the file holds, what is loaded)
    "seen is a number": ({"seen": 5}, lambda spot: spot._seen_news == set()),
    "seen holds lists": ({"seen": [["nvda-story-0001"]]}, lambda spot: spot._seen_news == set()),
    "ran holds text": ({"ran": {"news": "soon", "prev": None}}, lambda spot: spot.ran == {}),
    "news holds text": ({"news": ["not a story"], "seen": ["x"]},
                        lambda spot: spot.news == [] and spot._seen_news == {"x"}),
    "recent requests hold text": ({"recent_requests": ["soon", None, True]},
                                  lambda spot: spot.massive.limiter.used() == 0),
}


@pytest.mark.parametrize("extra,loaded", list(BAD_INSIDE.values()), ids=list(BAD_INSIDE))
def test_bad_values_inside_the_state_file_are_ignored(tmp_path, extra, loaded):
    path = tmp_path / "nvidia.json"
    path.write_text(json.dumps({"symbol": "NVDA", **extra}))
    clock = Clock(ny(2026, 10, 6, 12, 0))
    spot, server = desk(clock, path=path)
    assert loaded(spot)
    asyncio.run(spot.step(True))
    assert len(server.calls) == 5
    assert_fits(E.nvidia_board(quote("NVDA", price=180.0), spot))


def test_loading_checks_every_saved_value_like_a_fresh_answer(tmp_path):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    now = clock.t
    good = day_bar(date(2026, 10, 5))
    ohlct = {k: good[k] for k in ("o", "h", "l", "c", "t")}
    item = story(1, now - HOUR)
    odd_item = story(2, now - 2 * HOUR, article_url=7, published_utc=None, insights={"ticker": "NVDA"})
    mixed_item = story(3, now - 3 * HOUR, insights=[None, "x", {"ticker": "NVDA", "sentiment": "positive"}])
    state = {
        "symbol": "NVDA",
        "prev": dict(good, T="NVDA"),
        "snapshot": {"ticker": "NVDA", "lastTrade": {"p": 181.0}},
        "daily": [good, {"c": 1.0, "t": 5}, dict(good, o="1"), dict(good, h=True), dict(good, l=NAN), "bar", None,
                  dict(good, v="lots", vw=None, n=7)],
        "minutes": [dict(good, t=good["t"] + 60_000), dict(good, c=float("inf"))],
        "indicators": {"sma50": {"value": 170.5, "timestamp": 1}, "rsi14": {"value": "58"},
                       "macd": {"value": 2.5, "signal": "x", "histogram": 0.75}, "vwap9": {"value": 1.0}, "ema20": 5},
        "details": {"market_cap": 4.4e12, "total_employees": 36000},
        "news": [item, {"title": 5, "id": "t"}, {"id": "no-title"}, odd_item, mixed_item, "story", None],
        "dividends": [{"cash_amount": 0.01, "pay_date": "2026-10-02"}, {"cash_amount": "0.01"}, {"cash_amount": None},
                      "x"],
        "splits": [{"split_from": 1, "split_to": 10, "execution_date": "2024-06-10"}, {"split_from": "1", "split_to": 4},
                   {"split_to": 2}],
        "related": ["AMD", 5, None, "TSM", ["x"]],
        "status": {"market": "open"},
        "ran": {"news": now - 10, "prev": "soon", "daily": None, "status": True, "splits": now - 20, "macd": NAN,
                "snapshot": now - 30},
        "ok_at": {"news": now - 10, "splits": "x", "details": float("inf")},
        "not_in_plan": {"snapshot": now - 30, "minutes": [1]},
        "seen": ["a", 1, None, "b", ["c"]],
        "recent_requests": [now - 70, now - 30, now - 10, "x", None, True, now + 5],
    }
    path = tmp_path / "nvidia.json"
    path.write_text(json.dumps(state))  # NaN and Infinity are written as JSON's NaN/Infinity, which loads back
    spot, server = desk(clock, path=path)
    assert spot.prev == dict(good, t=float(good["t"]))  # no "T"
    assert spot.snapshot == state["snapshot"] and spot.details == state["details"] and spot.status == state["status"]
    assert spot.daily == [good, dict(ohlct, n=7)]
    assert spot.minutes == [dict(good, t=good["t"] + 60_000)]
    assert spot.indicators == {"sma50": {"value": 170.5, "timestamp": 1.0}, "macd": {"value": 2.5, "histogram": 0.75}}
    assert spot.news == [item, dict(odd_item, article_url="", published_utc="", insights=[]),
                         dict(mixed_item, insights=[{"ticker": "NVDA", "sentiment": "positive"}])]
    assert spot.dividends == state["dividends"][:1] and spot.splits == state["splits"][:1]
    assert spot.related == ["AMD", "TSM"]
    assert spot.ran == {"news": now - 10, "splits": now - 20, "snapshot": now - 30}
    assert spot.ok_at == {"news": now - 10}
    assert spot.not_in_plan == {"snapshot": now - 30}
    assert spot._seen_news == {"a", "b"}
    assert spot.massive.limiter.used() == 2  # the two requests from the last minute
    assert spot.plan() == "free plan (end of day)" and not spot.snapshot_fresh()
    assert_fits(E.nvidia_board(quote("NVDA", price=180.0), spot))
    assert_fits(E.nvidia_board(None, spot))
    assert asyncio.run(spot.step(True)) == ["prev", "status", "daily"]  # three slots left; news ran 10 s ago
    assert [c.name for c in server.calls] == ["prev", "status", "daily"]


# =====================================================================================================================
# Embeds
# =====================================================================================================================

def full_spotlight(paid=True):
    clock = Clock(ny(2026, 10, 6, 10, 0))
    spot, server = desk(clock, paid=paid)
    for i in range(30):
        server.add(story(i, clock.t - (30 - i) * 1200, mood=("positive", "neutral", "negative", "")[i % 4]))
    simulate(spot, clock, hours=0.25)
    return spot


def test_nvidia_board_with_an_empty_spotlight_and_no_quote():
    e = E.nvidia_board(None, Spotlight(None))
    assert e.title == "🟩 NVIDIA (NVDA) · Live" and e.description == "Live price unavailable right now."
    assert e.fields == [] and "add MASSIVE_API_KEY" in e.footer.text
    assert_fits(e)
    e = E.nvidia_board(quote("NVDA", price=181.25, change=-1.5, state="REGULAR", day_low=179.0, day_high=184.5,
                             volume=1.5e8), Spotlight(None))
    assert "$181.25" in e.description and "-1.50%" in e.description and "Day range 179.00 – 184.50" in e.description
    assert e.color.value == E.RED
    assert_fits(e)


def test_nvidia_board_with_everything():
    spot = full_spotlight()
    q = quote("NVDA", price=183.0, change=2.2, state="POST", ext_price=184.1, ext_change_pct=0.6, day_low=178.0,
              day_high=185.0, volume=2.1e8)
    e = E.nvidia_board(q, spot, (4, 5))
    names = [f.name for f in e.fields]
    assert names[0].startswith("Last session (Mon Oct 05)")
    assert "Technicals (at the last close)" in names and "The company" in names
    assert any(n.startswith("News · last 48h:") for n in names)
    text = "\n".join([e.description] + [f.value for f in e.fields])
    for bit in ("Massive (15-min delayed)", "Extended hours", "50-day average", "200-day average", "20-day EMA",
                "RSI 58 (neutral)", "MACD 2.51 above its signal 1.75 (bullish)", "52 weeks", "Market cap $4.43T",
                "36,000 employees", "Dividend $0.01", "Last split 10-for-1 on 2024-06-10", "Related: AMD, AVGO",
                "× its 50-day average", "VWAP"):
        assert bit in text, bit
    assert e.footer.text == ("Live price: Yahoo Finance · Massive (paid plan (delayed snapshot available)): "
                             "4/5 calls in the last minute")
    news = next(f for f in e.fields if f.name.startswith("News"))
    assert len(news.value.splitlines()) == 4 and "](https://www.benzinga.com/news/nvda/" in news.value
    assert_fits(e)


def test_nvidia_board_without_a_live_quote_uses_the_last_close():
    spot = full_spotlight(paid=False)
    e = E.nvidia_board(None, spot)
    assert e.description.startswith("Live price unavailable")
    assert "Massive (15-min delayed)" not in e.description
    text = "\n".join(f.value for f in e.fields)
    assert "50-day average" in text and "52 weeks" in text
    assert "free plan (end of day)" in e.footer.text and "0/5" in e.footer.text
    assert_fits(e)


def test_nvidia_board_survives_huge_values():
    spot = full_spotlight()
    spot.related = [f"T{i}" * 50 for i in range(500)]
    spot.details = dict(spot.details, total_employees=10 ** 15, market_cap=9.9e20)
    spot.news = [story(i, spot._clock() - 60, title="Nvidia " + "x" * 3000,
                       article_url="https://e.example/" + "a" * 3000) for i in range(MAX_NEWS)]
    spot.dividends = [{"cash_amount": 123456.789, "ex_dividend_date": "z" * 500, "pay_date": "y" * 500}]
    spot.splits = [{"split_to": 9e300, "split_from": 1e-300, "execution_date": "d" * 400}]
    spot.indicators = {k: {"value": 1e300, "signal": -1e300} for k in INDICATORS}
    spot.prev = dict(spot.prev, v=1e30, vw=1e30, h=1e30)
    q = quote("NVDA", price=1e12, change=999.0, state="PRE", ext_price=1e13, ext_change_pct=5000.0, day_low=1e-9,
              day_high=1e15, volume=1e20)
    assert_fits(E.nvidia_board(q, spot, (5, 5)))
    assert_fits(E.nvidia_board(None, spot, (10 ** 9, 5)))


def test_nvidia_board_tolerates_split_values_that_are_not_numbers():
    spot = full_spotlight()
    spot.splits = [{"split_to": "9" * 400, "split_from": "1" * 400, "execution_date": "d" * 400}]
    assert_fits(E.nvidia_board(quote("NVDA", price=180.0), spot))


def test_nvidia_board_shows_massive_data_only_while_massive_is_usable(tmp_path):
    spot = full_spotlight()
    q = quote("NVDA", price=183.0, change=2.2, day_low=178.0, day_high=185.0, volume=2.1e8)
    full = E.nvidia_board(q, spot)
    assert len(full.fields) == 4 and "Massive (15-min delayed)" in full.description
    # the key is rejected later: nothing Massive sent before is shown
    spot.massive.key_rejected = True
    e = E.nvidia_board(q, spot)
    assert e.fields == [] and "Massive" not in e.description and "$183.00" in e.description
    assert e.footer.text == "Live price: Yahoo Finance · Massive rejected the key (see /status)"
    assert_fits(e)
    # the key was removed while the file still holds everything
    path = tmp_path / "nvidia.json"
    spot.massive.key_rejected = False
    spot.path = path
    spot.save()
    for massive in (None, Massive(None, spot.massive.http), Massive("", spot.massive.http)):
        left = Spotlight(massive, path, clock=spot._clock)
        assert left.prev and left.daily and left.news and left.snapshot  # loaded, but not shown
        for quote_ in (q, None):
            e = E.nvidia_board(quote_, left)
            assert e.fields == [] and "Massive" not in e.description
            assert e.footer.text == "Live price: Yahoo Finance · add MASSIVE_API_KEY for Massive's data"
        assert E.nvidia_board(None, left).description == "Live price unavailable right now."


def test_nvidia_board_shows_the_delayed_snapshot_only_while_it_is_fresh():
    spot = full_spotlight()
    q = quote("NVDA", price=183.0, change=2.2)
    clock = spot._clock
    worked = spot.ok_at["snapshot"]
    price = spot.snapshot["lastTrade"]["p"]
    line = f"Massive (15-min delayed): ${price:,.2f} "
    for age, shown in ((0, True), (SNAPSHOT_FRESH - 1, True), (SNAPSHOT_FRESH, False), (DAY, False)):
        clock.t = worked + age
        assert (line in E.nvidia_board(q, spot).description) is shown, age
        assert (line in E.nvidia_board(None, spot).description) is shown, age
    clock.t = worked
    spot.not_in_plan["snapshot"] = worked
    assert line not in E.nvidia_board(q, spot).description
    del spot.not_in_plan["snapshot"]
    for odd in ({"lastTrade": None}, {"lastTrade": {"p": "181"}}, {"lastTrade": {"p": None}}, {"lastTrade": [1]},
                {"lastTrade": {"p": NAN}}, {"lastTrade": {"p": 181.0}, "todaysChangePerc": "x"}):
        spot.snapshot = odd
        e = E.nvidia_board(q, spot)
        assert ("15-min delayed" in e.description) == (odd.get("lastTrade") == {"p": 181.0})
        assert_fits(e)


def test_nvidia_board_news_links_only_web_addresses():
    spot = full_spotlight()
    spot.news = [dict(story(1, spot._clock() - 60), article_url="javascript:alert(1)"),
                 dict(story(2, spot._clock() - 120), article_url=""),
                 story(3, spot._clock() - 180)]
    news = next(f for f in E.nvidia_board(quote("NVDA", price=180.0), spot).fields if f.name.startswith("News"))
    assert news.value.splitlines() == ["🟢 Nvidia headline number 1", "🟢 Nvidia headline number 2",
                                       "🟢 [Nvidia headline number 3](https://www.benzinga.com/news/nvda/3)"]


def test_rsi_labels():
    assert [E.rsi_label(v) for v in (85, 70, 69.9, 60, 59.9, 50, 40.1, 40, 30.1, 30, 5)] == [
        "overbought", "overbought", "strong", "strong", "neutral", "neutral", "neutral", "weak", "weak", "oversold",
        "oversold"]


def test_nvidia_news_embed():
    spot = Spotlight(None)
    item = story(7, ny(2026, 10, 6, 11, 0), mood="negative")
    mood, why = spot.sentiment(item)
    e = E.nvidia_news(item, mood, why)
    assert e.title == "🔴 Nvidia headline number 7" and e.url == item["article_url"]
    assert e.color.value == E.RED and e.description == item["description"]
    assert [f.name for f in e.fields] == ["For NVDA: negative", "Also mentions"]
    assert e.fields[0].value == "Story 7 reads negative for NVDA." and e.fields[1].value == "AMD, TSM"
    assert e.footer.text == "Benzinga · via Massive" and e.thumbnail.url == item["image_url"]
    assert e.timestamp == datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    assert_fits(e)


def test_nvidia_news_with_little_or_odd_data():
    e = E.nvidia_news({}, "", "")
    assert e.title == "📰 NVIDIA news" and e.fields == [] and e.footer.text == "Massive news · via Massive"
    assert e.timestamp is None and e.color.value == E.BLUE
    assert_fits(e)
    e = E.nvidia_news({"title": "t", "published_utc": "yesterday-ish", "tickers": ["NVDA"], "publisher": {}},
                      "mixed", "")
    assert e.timestamp is None and [f.name for f in e.fields] == ["For NVDA: mixed"] and e.fields[0].value == "—"
    assert e.title == "📰 t"
    assert_fits(e)


def test_nvidia_news_fits_with_huge_values():
    item = story(1, ny(2026, 10, 6, 11, 0), title="N" * 5000, description="d" * 9000,
                 tickers=["NVDA"] + [f"T{i}" * 300 for i in range(40)], publisher={"name": "P" * 5000})
    e = E.nvidia_news(item, "positive", "r" * 9000)
    assert len(e.description) <= 600 and len(e.fields[0].value) <= 400
    assert_fits(e)


ODD_STORIES = {
    "a script link": ({"article_url": "javascript:alert(1)", "image_url": "javascript:x"}, None, None),
    "ftp links": ({"article_url": "ftp://x.example/a", "image_url": "ftp://x.example/a.png"}, None, None),
    "links that aren't text": ({"article_url": ["https://x.example"], "image_url": 5}, None, None),
    "plain http": ({"article_url": "http://x.example/a", "image_url": "http://x.example/a.png"},
                   "http://x.example/a", "http://x.example/a.png"),
}


@pytest.mark.parametrize("extra,url,thumb", list(ODD_STORIES.values()), ids=list(ODD_STORIES))
def test_nvidia_news_drops_links_that_are_not_web_addresses(extra, url, thumb):
    e = E.nvidia_news(story(1, ny(2026, 10, 6, 11, 0), **extra), "neutral", "")
    assert e.url == url and e.thumbnail.url == thumb
    assert_fits(e)


def test_nvidia_news_with_odd_types():
    item = {"title": "Odd", "description": ["not", "text"], "tickers": "NVDA,AMD", "publisher": ["Benzinga"],
            "published_utc": 1791300000, "article_url": None, "image_url": None}
    e = E.nvidia_news(item, "negative", "")
    assert e.title == "🔴 Odd" and e.url is None and not e.description and e.thumbnail.url is None
    assert [f.name for f in e.fields] == ["For NVDA: negative"] and e.fields[0].value == "—"
    assert e.footer.text == "Massive news · via Massive" and e.timestamp is None
    assert_fits(e)
    e = E.nvidia_news({"title": "t", "tickers": ["NVDA", 5, None, "AMD", ["TSM"]],
                       "publisher": {"name": 7}}, "", "")
    assert [(f.name, f.value) for f in e.fields] == [("Also mentions", "AMD")]
    assert e.footer.text == "Massive news · via Massive"
    assert_fits(e)


# =====================================================================================================================
# The bot
# =====================================================================================================================

class FakeData:
    def __init__(self, http=None, quotes=None):
        if http is not None:
            self.http = http
        self._quotes = dict(quotes or {})

    async def quotes(self, symbols):
        return {s: q for s, q in self._quotes.items() if s in symbols}

    async def close(self):
        pass


class FakeEngine:
    def __init__(self, data):
        self.data = data
        self.models = {}

    async def close(self):
        pass


def nvidia_bot(tmp_path, monkeypatch=None, key="k", http=True, clock=None, quotes=None):
    """A MarketBot with Discord faked out and its Massive and Spotlight on a fake clock and a fake server."""
    clock = clock or Clock(ny(2026, 10, 6, 12, 0))
    server = FakeMassive(clock, key=key or "k")
    data = FakeData(Http(server, sleep=no_retry_sleep) if http else None, quotes)
    bot = MarketBot(tmp_path, engine=FakeEngine(data), ai=NewsAI(api_key=""), massive_key=key)
    if bot.massive:
        bot.massive.limiter = limiter_on(clock)
        bot.spotlight = Spotlight(bot.massive, bot.spotlight.path, clock=clock)
    if monkeypatch:
        monkeypatch.setattr(botmod, "market_open", lambda now=None: is_open(clock.t))
    bot.sent, bot.boards = [], []

    async def send(cid, post):
        bot.sent.append((cid, post))
        return SimpleNamespace(id=1)

    async def show_board(cid, embed):
        bot.boards.append((cid, embed))
    bot.send = send
    bot.show_board = show_board
    return bot, server, clock


def test_the_bot_builds_massive_from_the_engines_http(tmp_path):
    data = FakeData(Http(Backend()))
    bot = MarketBot(tmp_path, engine=FakeEngine(data), ai=NewsAI(api_key=""), massive_key="k")
    assert isinstance(bot.massive, Massive) and bot.massive.key == "k" and bot.massive.http is data.http
    assert bot.spotlight.massive is bot.massive and bot.spotlight.enabled
    assert bot.spotlight.path == tmp_path / "nvidia.json" and bot.spotlight.symbol == "NVDA"
    assert bot.massive.limiter.calls == 5 and bot.massive.limiter.window == WINDOW
    assert bot.massive_used() == 0


@pytest.mark.parametrize("key,http", [(None, True), ("", True), ("k", False)])
def test_the_bot_has_no_massive_without_a_key_or_http(tmp_path, key, http):
    data = FakeData(Http(Backend()) if http else None)
    bot = MarketBot(tmp_path, engine=FakeEngine(data), ai=NewsAI(api_key=""), massive_key=key)
    assert bot.massive is None and not bot.spotlight.enabled and bot.massive_used() == 0
    assert bot.spotlight.plan() == "no key"


def test_the_bot_picks_up_the_saved_spotlight(tmp_path):
    clock = Clock(ny(2026, 10, 6, 12, 0))
    saved = Spotlight(None, tmp_path / "nvidia.json", clock=clock)
    bar = day_bar(date(2026, 10, 5))
    saved.prev = dict(T="NVDA", **bar)
    saved.related = ["AMD"]
    saved.ran = {"prev": clock.t - 60}
    saved.save()
    data = json.loads((tmp_path / "nvidia.json").read_text())
    data["recent_requests"] = [clock.t - 30, clock.t - 20]
    (tmp_path / "nvidia.json").write_text(json.dumps(data))
    bot, _, _ = nvidia_bot(tmp_path, clock=clock)
    assert bot.spotlight.prev == bar  # checked like a fresh answer: only the bar's numbers are kept
    assert bot.spotlight.related == ["AMD"] and bot.spotlight.ran == {"prev": clock.t - 60}
    assert bot.massive_used() == 2  # the last run's requests still count against this minute


def test_job_nvidia_needs_an_nvidia_channel(tmp_path, monkeypatch):
    bot, server, _ = nvidia_bot(tmp_path, monkeypatch)
    bot.channels.set(1, "stocks")
    bot.channels.set(2, "news")
    asyncio.run(bot.job_nvidia())
    assert server.calls == [] and bot.sent == []


def test_job_nvidia_needs_a_key(tmp_path, monkeypatch):
    bot, server, _ = nvidia_bot(tmp_path, monkeypatch, key=None)
    bot.channels.set(7, "nvidia")
    asyncio.run(bot.job_nvidia())
    assert server.calls == [] and bot.sent == [] and bot.massive is None


def test_job_nvidia_posts_fresh_news_to_nvidia_channels_with_alerts_on(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch)
    for cid in (7, 8, 9):
        bot.channels.set(cid, "nvidia")
    bot.channels.update(8, alerts=False)
    bot.channels.set(10, "stocks")
    for i in range(8):
        server.add(story(i, clock.t - (8 - i) * HOUR))
    asyncio.run(bot.job_nvidia())
    assert [c.name for c in server.calls] == ["snapshot", "news", "prev", "status", "daily"]
    assert bot.sent == []  # the first load is history, not news
    clock.t += 200
    server.add(story(200, clock.t - 60, mood="negative"))
    server.add(story(201, clock.t - 30, mood="positive"))
    asyncio.run(bot.job_nvidia())
    posted = [(cid, p.embeds[0].title) for cid, p in bot.sent]
    assert posted == [(7, "🔴 Nvidia headline number 200"), (7, "🟢 Nvidia headline number 201"),
                      (9, "🔴 Nvidia headline number 200"), (9, "🟢 Nvidia headline number 201")]
    first = bot.sent[0][1].embeds[0]
    assert first.fields[0].name == "For NVDA: negative" and first.url.endswith("/200")
    assert_fits(first)
    bot.sent.clear()
    clock.t += 200
    asyncio.run(bot.job_nvidia())
    assert bot.sent == []  # nothing new
    assert_budget([c.t for c in server.calls])


def test_job_nvidia_posts_only_the_newest_few_per_step(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch)
    bot.channels.set(7, "nvidia")
    server.add(story(0, clock.t - HOUR))
    asyncio.run(bot.job_nvidia())
    clock.t += 200
    for i in range(1, 7):
        server.add(story(i, clock.t - 100 + i))
    asyncio.run(bot.job_nvidia())
    titles = [p.embeds[0].title for _, p in bot.sent]
    assert len(titles) == NVIDIA_NEWS_PER_STEP
    assert titles == [f"🟢 Nvidia headline number {i}" for i in range(7 - NVIDIA_NEWS_PER_STEP, 7)]
    assert bot.spotlight.fresh_news == []


def test_job_nvidia_steps_with_the_market_hours(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch)
    bot.channels.set(7, "nvidia")
    seen = []
    real_step = bot.spotlight.step

    async def step(market_is_open):
        seen.append(market_is_open)
        return await real_step(market_is_open)
    bot.spotlight.step = step
    asyncio.run(bot.job_nvidia())
    clock.t = ny(2026, 10, 6, 20, 0)
    asyncio.run(bot.job_nvidia())
    assert seen == [True, False]


def test_job_nvidia_stops_asking_after_the_key_is_rejected(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch)
    server.key = "a different key"
    bot.channels.set(7, "nvidia")
    for _ in range(5):
        asyncio.run(bot.job_nvidia())
        clock.t += 900
    assert len(server.calls) == 1 and bot.massive.key_rejected and bot.spotlight.plan() == "key rejected"


def test_live_symbols_include_nvda_only_with_an_nvidia_channel(tmp_path):
    bot, _, _ = nvidia_bot(tmp_path)
    assert bot.live_symbols() == []
    bot.channels.set(5, "news")
    bot.channels.set(6, "trends")
    assert "NVDA" not in bot.live_symbols()
    bot.channels.set(7, "nvidia")
    assert bot.live_symbols() == ["NVDA"]
    bot.channels.set(8, "stocks")
    bot.channels.update(8, watchlist=("NVDA", "AAPL"))
    syms = bot.live_symbols()
    assert syms.count("NVDA") == 1 and "AAPL" in syms
    bot.channels.remove(7)
    bot.channels.update(8, watchlist=("AAPL",))
    assert "NVDA" not in bot.live_symbols()


def nvda(change, state="REGULAR", t=None, **kw):
    return quote("NVDA", price=180.0 * (1 + change / 100), change=change, state=state,
                 t=t or ny(2026, 10, 6, 15, 0), **kw)


def moves(bot):
    out = []
    for cid, post in bot.sent:
        e = post.embeds[0]
        out.append((cid, e.title, re.search(r"±([\d.]+)%", e.description).group(1)))
    return out


def test_nvidia_channel_move_alerts_once_per_line_per_day(tmp_path):
    bot, _, _ = nvidia_bot(tmp_path, key=None)
    bot.channels.set(7, "nvidia")

    def check(change, **kw):
        bot.quotes = {"NVDA": nvda(change, **kw)}
        asyncio.run(bot.check_moves())
    check(1.9)
    assert bot.sent == []  # under the first line
    check(2.4)
    check(2.9)
    assert moves(bot) == [(7, "🚀 NVDA up 2.4% today", "2")]
    check(3.1)
    check(2.2)  # back under a line it already crossed: quiet
    check(-2.1)  # the other way: a new alert
    check(-1.0)
    assert moves(bot) == [(7, "🚀 NVDA up 2.4% today", "2"), (7, "🚀 NVDA up 3.1% today", "3"),
                          (7, "📉 NVDA down 2.1% today", "2")]
    assert bot.state.get("moves", "7|NVDA|2026-10-06") == -2.0
    bot.sent.clear()
    check(2.3, t=ny(2026, 10, 7, 10, 0))  # a new session starts over
    check(2.6, t=ny(2026, 10, 7, 11, 0))
    assert moves(bot) == [(7, "🚀 NVDA up 2.3% today", "2")]


def test_nvidia_channel_alerts_on_every_nvidia_line(tmp_path):
    bot, _, _ = nvidia_bot(tmp_path, key=None)
    bot.channels.set(7, "nvidia")
    for step in NVIDIA_STEPS:
        for change in (step - 0.01, step + 0.01, step + 0.02):
            bot.quotes = {"NVDA": nvda(change)}
            asyncio.run(bot.check_moves())
    assert [float(line) for _, _, line in moves(bot)] == list(NVIDIA_STEPS)


def test_nvidia_lines_are_finer_than_a_stocks_channels(tmp_path):
    bot, _, _ = nvidia_bot(tmp_path, key=None)
    bot.channels.set(7, "nvidia")
    bot.channels.set(8, "stocks")
    bot.channels.update(8, watchlist=("NVDA",))
    bot.quotes = {"NVDA": nvda(2.5)}
    asyncio.run(bot.check_moves())
    assert [cid for cid, _ in bot.sent] == [7]  # 2.5% is a line for the NVIDIA channel only
    bot.quotes = {"NVDA": nvda(3.2)}
    asyncio.run(bot.check_moves())
    assert sorted(cid for cid, _ in bot.sent) == [7, 7, 8]


@pytest.mark.parametrize("state,alerts,alerted", [("REGULAR", True, True), ("POST", True, True),
                                                  ("POSTPOST", True, True), ("", True, True), ("PRE", True, False),
                                                  ("PREPRE", True, False), ("CLOSED", True, False),
                                                  ("REGULAR", False, False)])
def test_nvidia_move_alerts_respect_the_session_and_the_channel_setting(tmp_path, state, alerts, alerted):
    bot, _, _ = nvidia_bot(tmp_path, key=None)
    bot.channels.set(7, "nvidia")
    bot.channels.update(7, alerts=alerts)
    bot.quotes = {"NVDA": nvda(4.5, state=state)}
    asyncio.run(bot.check_moves())
    assert bool(bot.sent) == alerted


def test_nvidia_move_alerts_need_a_change(tmp_path):
    bot, _, _ = nvidia_bot(tmp_path, key=None)
    bot.channels.set(7, "nvidia")
    asyncio.run(bot.check_moves())  # no quote at all
    bot.quotes = {"NVDA": dataclasses.replace(nvda(9.0), change_pct=None)}
    asyncio.run(bot.check_moves())
    bot.quotes = {"AAPL": quote("AAPL", change=12.0)}
    asyncio.run(bot.check_moves())
    assert bot.sent == []


def test_refresh_boards_renders_the_nvidia_board(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch)
    for cid, kind in ((7, "nvidia"), (8, "stocks"), (9, "trends"), (10, "news"), (11, "nvidia")):
        bot.channels.set(cid, kind)
    bot.quotes = {"NVDA": nvda(1.2)}
    asyncio.run(bot.refresh_boards())
    assert [cid for cid, _ in bot.boards] == [7, 8, 11]
    board = dict(bot.boards)[7]
    assert board.title == "🟩 NVIDIA (NVDA) · Live" and "$182.16" in board.description
    assert board.footer.text.endswith("Massive (plan being checked): 0/5 calls in the last minute")
    assert_fits(board)
    asyncio.run(bot.job_nvidia())
    assert bot.massive_used() == 5
    bot.boards.clear()
    asyncio.run(bot.refresh_boards())
    board = dict(bot.boards)[11]
    assert board.footer.text.endswith("Massive (free plan (end of day)): 5/5 calls in the last minute")
    assert board.fields[0].name.startswith("Last session (Mon Oct 05)")
    assert_fits(board)


def test_job_live_shows_the_nvidia_board_and_alerts(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch, quotes={"NVDA": nvda(5.2)})
    bot.channels.set(7, "nvidia")
    asyncio.run(bot.job_live())
    assert [cid for cid, _ in bot.boards] == [7]
    assert moves(bot) == [(7, "🚀 NVDA up 5.2% today", "5")]


def test_a_malformed_massive_answer_does_not_break_every_board(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch, quotes={"NVDA": nvda(5.2)})
    bars = [day_bar(d) for d in trading_days(date(2026, 9, 1), date(2026, 10, 5))]
    good = list(bars)
    bars.insert(3, {"c": 170.0, "t": session_ms(date(2026, 9, 3)), "v": 1})  # no high or low
    server.hooks["daily"] = reply(200, {"results": bars, "status": "OK", "resultsCount": len(bars)})
    bot.channels.set(7, "nvidia")
    bot.channels.set(8, "stocks")
    asyncio.run(bot.job_nvidia())
    assert bot.spotlight.daily == good  # the bar without a high and low was left out
    asyncio.run(bot.job_live())
    assert [cid for cid, _ in bot.boards] == [7, 8]
    assert "52 weeks" in "\n".join(f.value for f in dict(bot.boards)[7].fields)
    # NVDA is on the stocks channel's default list too, so both channels hear about the move
    assert moves(bot) == [(7, "🚀 NVDA up 5.2% today", "5"), (8, "🚀 NVDA up 5.2% today", "5")]


def test_a_failing_nvidia_board_does_not_stop_the_other_boards_or_the_alerts(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch, quotes={"NVDA": nvda(5.2)})
    for cid, kind in ((7, "nvidia"), (8, "stocks"), (9, "nvidia")):
        bot.channels.set(cid, kind)

    def broken(*args, **kwargs):
        raise KeyError("h")
    monkeypatch.setattr(E, "nvidia_board", broken)
    asyncio.run(bot.job_live())
    assert [cid for cid, _ in bot.boards] == [8]
    assert moves(bot) == [(7, "🚀 NVDA up 5.2% today", "5"), (8, "🚀 NVDA up 5.2% today", "5"),
                          (9, "🚀 NVDA up 5.2% today", "5")]


def test_an_nvda_quote_nobody_refreshed_leaves_the_nvidia_board(tmp_path, monkeypatch):
    bot, server, clock = nvidia_bot(tmp_path, monkeypatch, quotes={"NVDA": nvda(1.2)})
    bot.channels.set(7, "nvidia")
    asyncio.run(bot.job_live())
    assert "$182.16" in bot.boards[-1][1].description
    bot.engine.data._quotes = {}  # every source stops answering for NVDA
    bot.quote_seen["NVDA"] -= STALE_QUOTE - 30  # still within the 15 minutes
    asyncio.run(bot.job_live())
    assert "NVDA" in bot.quotes and "$182.16" in bot.boards[-1][1].description
    bot.quote_seen["NVDA"] -= 60  # now past them
    asyncio.run(bot.job_live())
    assert "NVDA" not in bot.quotes
    assert bot.boards[-1][1].description == "Live price unavailable right now."
    assert STALE_QUOTE == 900
