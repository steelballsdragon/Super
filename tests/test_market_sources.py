"""Offline tests for the web transport (marketbot/http.py), the Yahoo client (marketbot/yahoo.py) and the backup
price sources (marketbot/backup.py: Nasdaq and Coinbase).

Every request goes to a fake backend that records it, clocks are fake and nothing really sleeps.
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

np = pytest.importorskip("numpy")

from marketbot import backup as backup_mod, http as http_mod, yahoo as yahoo_mod  # noqa: E402
from marketbot.backup import (COINBASE, DAY, NASDAQ, NASDAQ_HEADERS, OPEN_UTC, UNSUPPORTED_TTL, Coinbase,  # noqa: E402
                              Nasdaq, _day_t, _ny_time, bars_from_rows, clean_name, coinbase_product,
                              from_nasdaq_symbol, nasdaq_symbol, number)
from marketbot.hours import NEW_YORK  # noqa: E402
from marketbot.http import (FAILS_TO_REST, REST_SECONDS, TIMEOUT, Health, Http, HttpError, Response,  # noqa: E402
                            _quiet)
from marketbot.yahoo import BASE, CRUMB_TTL, EARLIEST, YahooClient, YahooError  # noqa: E402

WARN = "⚠️"
CHECK = "✅"
DOT = "\xb7"


# ----- fakes -----

class Unexpected(BaseException):
    """A request no route answers. A BaseException so Http's `except Exception` can't turn it into a retry."""


@dataclass
class Call:
    url: str
    headers: dict
    timeout: float
    proxy: str | None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def query(self) -> dict:
        return dict(parse_qsl(urlsplit(self.url).query))

    @property
    def pairs(self) -> list:
        return parse_qsl(urlsplit(self.url).query)


class Backend:
    """A fake Http backend. Answers come from routes (the first whose needle is in the URL) or else from the script.
    Each queue hands out its answers in order and repeats the last one. An answer is a Response, an int status, an
    exception (raised) or a function of the URL returning one of those."""

    name = "fake browser"

    def __init__(self, *script):
        self.script = list(script)
        self.routes: list[tuple[str, list]] = []
        self.calls: list[Call] = []
        self.unexpected: list[str] = []
        self.closed = False
        self.yield_first = True

    def on(self, needle: str, *answers) -> "Backend":
        self.routes.append((needle, list(answers)))
        return self

    def calls_to(self, needle: str) -> list[Call]:
        return [c for c in self.calls if needle in c.url]

    async def get(self, url, headers, timeout, proxy=None):
        self.calls.append(Call(url, headers, timeout, proxy))
        if self.yield_first:
            await asyncio.sleep(0)
        for needle, queue in self.routes:
            if needle in url:
                return self._answer(queue, url)
        if self.script:
            return self._answer(self.script, url)
        self.unexpected.append(url)
        raise Unexpected(url)

    @staticmethod
    def _answer(queue: list, url: str):
        a = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(a) and not isinstance(a, (Response, BaseException)):
            a = a(url)
        if isinstance(a, BaseException):
            raise a
        if isinstance(a, int):
            a = Response(a, "", url)
        return a

    async def close(self):
        self.closed = True


class Sleeper:
    """Http's sleep: records the delays without waiting."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, seconds):
        self.delays.append(seconds)
        await asyncio.sleep(0)


class Clock:
    """Stands in for the `time` module inside http, yahoo and backup."""

    def __init__(self, mono=50_000.0, wall=1_791_300_000.0):  # wall: 2026-10-06 15:20 UTC
        self.mono, self.wall = mono, wall

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    for mod in (http_mod, yahoo_mod, backup_mod):
        monkeypatch.setattr(mod, "time", c)
    return c


def J(obj, status=200) -> Response:
    return Response(status, json.dumps(obj))


def query(url: str) -> dict:
    return dict(parse_qsl(urlsplit(url).query))


def make_http(*script, **kw):
    backend = Backend(*script)
    sleeper = Sleeper()
    return Http(backend=backend, sleep=sleeper, **kw), backend, sleeper


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def ny(*args) -> int:
    return int(datetime(*args, tzinfo=NEW_YORK).timestamp())


# =====================================================================================================================
# http.py
# =====================================================================================================================

# ----- retries -----

def test_network_errors_are_retried_then_the_answer_returned(clock):
    http, backend, sleeper = make_http(ConnectionError("reset by peer"), TimeoutError("slow"), J({"a": 1}))
    resp = asyncio.run(http.get("https://x.test/a", source="S"))
    assert resp.status == 200 and resp.json() == {"a": 1}
    assert len(backend.calls) == 3 and sleeper.delays == [1.5, 3.0]
    h = http.health["S"]
    assert (h.ok, h.failed, h.streak, h.rests, h.status, h.last_ok) == (1, 0, 0, 0, 200, clock.wall)
    assert not h.failing and not http.resting("S")


def test_server_errors_are_retried_then_the_answer_returned(clock):
    http, backend, sleeper = make_http(Response(503, "busy"), Response(502, ""), J({"ok": True}))
    resp = asyncio.run(http.get("https://x.test/a", source="S"))
    assert resp.json() == {"ok": True} and len(backend.calls) == 3 and sleeper.delays == [1.5, 3.0]
    assert http.health["S"].failed == 0 and http.health["S"].ok == 1


def test_server_errors_that_keep_coming_fail_once_with_the_status(clock):
    http, backend, sleeper = make_http(Response(503, "Service Unavailable"))
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://x.test/a", source="S"))
    assert e.value.status == 503 and e.value.source == "S"
    assert str(e.value) == "HTTP 503 (Service Unavailable)"
    assert len(backend.calls) == 3 and sleeper.delays == [1.5, 3.0]  # no pointless sleep after the last try
    h = http.health["S"]
    assert (h.failed, h.streak, h.status, h.ok) == (1, 1, 503, 0)  # one call failed, not three
    assert h.last_error == "HTTP 503 (Service Unavailable)" and h.error_at == clock.wall and h.failing


def test_network_errors_that_keep_coming_fail_without_a_status(clock):
    http, backend, sleeper = make_http(ConnectionError("Could not resolve host"))
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://x.test/a", source="S"))
    assert e.value.status is None and str(e.value) == "ConnectionError: Could not resolve host"
    assert len(backend.calls) == 3 and http.health["S"].status is None


def test_retry_count_comes_from_the_call_or_the_transport():
    http, backend, sleeper = make_http(Response(500, ""))
    with pytest.raises(HttpError):
        asyncio.run(http.get("https://x.test", source="S", retries=0))
    assert len(backend.calls) == 1 and sleeper.delays == []

    http, backend, sleeper = make_http(Response(500, ""), retries=4)
    with pytest.raises(HttpError):
        asyncio.run(http.get("https://x.test", source="S"))
    assert len(backend.calls) == 5 and sleeper.delays == [1.5, 3.0, 6.0, 12.0]


def test_a_server_error_then_a_block_stops_retrying():
    http, backend, sleeper = make_http(Response(503, ""), Response(403, "Forbidden"), J({}))
    resp = asyncio.run(http.get("https://x.test", source="S"))
    assert resp.status == 403 and len(backend.calls) == 2 and sleeper.delays == [1.5]
    assert http.health["S"].failed == 1 and http.health["S"].streak == 1


# ----- rate limits and blocks -----

def test_429_is_raised_at_once_and_never_retried(clock):
    http, backend, sleeper = make_http(Response(429, "Edge: Too Many Requests"), J({}))
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://x.test", source="S"))
    assert e.value.status == 429 and "Edge: Too Many Requests" in str(e.value)
    assert len(backend.calls) == 1 and sleeper.delays == []
    h = http.health["S"]
    assert (h.failed, h.streak, h.status) == (1, 1, 429)


def test_429_after_a_server_error_is_raised_without_another_try():
    http, backend, sleeper = make_http(Response(503, ""), Response(429, "slow down"), J({}))
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://x.test", source="S"))
    assert e.value.status == 429 and len(backend.calls) == 2 and sleeper.delays == [1.5]


@pytest.mark.parametrize("status", [401, 403, 407])
def test_blocks_are_returned_and_counted_as_failures(clock, status):
    http, backend, sleeper = make_http(Response(status, "denied"), J({}))
    resp = asyncio.run(http.get("https://x.test", source="S"))
    assert resp.status == status and resp.text == "denied"
    assert len(backend.calls) == 1 and sleeper.delays == []  # the caller decides (e.g. a new crumb)
    h = http.health["S"]
    assert (h.ok, h.failed, h.streak, h.status) == (0, 1, 1, status)
    assert h.last_error == f"HTTP {status} (denied)" and h.failing


def test_three_different_blocks_in_a_row_rest_the_source(clock):
    http, backend, _ = make_http(Response(401, ""), Response(403, ""), Response(407, ""), J({}))

    async def main():
        for status in (401, 403, 407):
            assert (await http.get("https://x.test", source="S")).status == status
        assert http.resting("S")
        with pytest.raises(HttpError):
            await http.get("https://x.test", source="S")

    asyncio.run(main())
    assert len(backend.calls) == 3


@pytest.mark.parametrize("status", [200, 204, 302, 400, 404, 410, 499])
def test_answers_that_are_not_failures_count_as_the_source_working(clock, status):
    http, backend, _ = make_http(Response(status, "x"))
    resp = asyncio.run(http.get("https://x.test", source="S"))
    assert resp.status == status and len(backend.calls) == 1
    h = http.health["S"]
    assert (h.ok, h.failed, h.streak, h.status, h.last_ok) == (1, 0, 0, status, clock.wall)


def test_answered_statuses_count_as_working_and_end_a_streak(clock):
    http, backend, sleeper = make_http(Response(500, "x"), Response(500, "x"), Response(403, "outside your plan"),
                                       Response(503, "x"), retries=0)

    async def main():
        for _ in range(2):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        assert http.health["S"].streak == 2
        resp = await http.get("https://x.test", source="S", answered=(403,))
        assert resp.status == 403 and resp.text == "outside your plan"
        h = http.health["S"]
        assert (h.ok, h.streak, h.status, h.failed) == (1, 0, 403, 2)
        resp = await http.get("https://x.test", source="S", answered=(503,), retries=3)
        assert resp.status == 503  # answered: returned, not retried
        assert len(backend.calls) == 4 and sleeper.delays == []

    asyncio.run(main())


# ----- resting -----

def test_three_failures_in_a_row_rest_the_source(clock):
    http, backend, _ = make_http(Response(503, "down"), retries=0)

    async def main():
        for i in range(FAILS_TO_REST):
            assert not http.resting("S")
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        h = http.health["S"]
        assert http.resting("S") and h.resting() and h.rests == 1
        assert h.rest_until == clock.mono + REST_SECONDS[0] == clock.mono + 30
        with pytest.raises(HttpError) as e:
            await http.get("https://x.test", source="S")
        assert len(backend.calls) == FAILS_TO_REST  # resting: the backend wasn't asked
        assert str(e.value) == "S is resting after 3 failures (HTTP 503 (down))" and e.value.status == 503
        assert h.failed == 3 and h.streak == 3  # a refused call isn't another failure
        clock.advance(29.9)
        assert http.resting("S")
        clock.advance(0.2)
        assert not http.resting("S")

    asyncio.run(main())


def test_two_failures_do_not_rest_and_an_answer_in_between_resets_the_streak(clock):
    http, backend, _ = make_http(Response(500, ""), Response(500, ""), J({}), Response(500, ""), Response(500, ""),
                                 retries=0)

    async def main():
        for expect_ok in (False, False, True, False, False):
            if expect_ok:
                await http.get("https://x.test", source="S")
            else:
                with pytest.raises(HttpError):
                    await http.get("https://x.test", source="S")
            assert not http.resting("S")
        h = http.health["S"]
        assert (h.ok, h.failed, h.streak, h.rests) == (1, 4, 2, 0)

    asyncio.run(main())


def test_rests_grow_each_time_the_source_fails_again_right_after_one(clock):
    http, backend, _ = make_http(Response(502, ""), retries=0)

    async def main():
        for _ in range(3):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        h = http.health["S"]
        lengths = [h.rest_until - clock.mono]
        for _ in range(4):
            clock.advance(h.rest_until - clock.mono + 1)
            assert not http.resting("S")
            with pytest.raises(HttpError) as e:
                await http.get("https://x.test", source="S")
            assert "resting" not in str(e.value)  # it really asked
            lengths.append(h.rest_until - clock.mono)
        assert lengths == [30.0, 60.0, 120.0, 300.0, 300.0]
        assert h.rests == 5 and h.streak == 7 and len(backend.calls) == 7

    asyncio.run(main())


def test_force_asks_a_resting_source_and_a_failure_does_not_lengthen_the_rest(clock):
    http, backend, _ = make_http(Response(500, ""), retries=0)

    async def main():
        for _ in range(3):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        h = http.health["S"]
        until = h.rest_until
        clock.advance(10)
        with pytest.raises(HttpError) as e:
            await http.get("https://x.test", source="S", force=True)
        assert "resting" not in str(e.value) and len(backend.calls) == 4
        assert h.rest_until == until and h.rests == 1 and h.streak == 4

    asyncio.run(main())


def test_force_success_ends_the_rest_and_resets_its_length(clock):
    http, backend, _ = make_http(Response(500, ""), Response(500, ""), Response(500, ""), J({"back": 1}),
                                 Response(500, ""), retries=0)

    async def main():
        for _ in range(3):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        assert http.resting("S")
        resp = await http.get("https://x.test", source="S", force=True)
        assert resp.json() == {"back": 1}
        h = http.health["S"]
        assert not http.resting("S") and (h.streak, h.rests, h.rest_until) == (0, 0, 0.0)
        for _ in range(3):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        assert h.rest_until - clock.mono == REST_SECONDS[0]  # back to the shortest rest

    asyncio.run(main())


def test_recovery_after_a_rest_resets_streak_and_rests(clock):
    http, backend, _ = make_http(Response(500, ""), Response(500, ""), Response(500, ""), J({}), Response(500, ""),
                                 retries=0)

    async def main():
        for _ in range(3):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        clock.advance(31)
        await http.get("https://x.test", source="S")
        h = http.health["S"]
        assert (h.streak, h.rests, h.ok, h.failed) == (0, 0, 1, 3) and not h.failing
        with pytest.raises(HttpError):
            await http.get("https://x.test", source="S")
        assert not http.resting("S") and h.streak == 1

    asyncio.run(main())


def test_sources_rest_independently(clock):
    backend = Backend().on("bad.test", Response(500, "")).on("good.test", J({}))
    http = Http(backend=backend, retries=0, sleep=Sleeper())

    async def main():
        for _ in range(3):
            with pytest.raises(HttpError):
                await http.get("https://bad.test", source="Bad")
        assert http.resting("Bad") and not http.resting("Good")
        assert (await http.get("https://good.test", source="Good")).status == 200

    asyncio.run(main())
    assert not http.resting("Never used") and "Never used" not in http.health


def test_requests_queued_behind_the_gate_do_not_call_a_source_rested_meanwhile(clock):
    http, backend, _ = make_http(ConnectionError("down"), retries=0, limits={"S": 1})

    async def main():
        return await asyncio.gather(*(http.get("https://x.test", source="S") for _ in range(6)),
                                    return_exceptions=True)

    results = asyncio.run(main())
    assert all(isinstance(r, HttpError) for r in results)
    assert http.resting("S")
    assert len(backend.calls) == FAILS_TO_REST  # the 4th..6th were refused once the source was rested
    assert [str(r) for r in results] == ["ConnectionError: down"] * 3 + \
        ["S is resting after 3 failures (ConnectionError: down)"] * 3
    assert all(r.status is None and r.source == "S" for r in results)
    h = http.health["S"]
    assert (h.failed, h.streak, h.rests, h.ok) == (3, 3, 1, 0)  # the refused calls aren't more failures
    assert h.rest_until == clock.mono + REST_SECONDS[0]
    assert http._gates["S"]._value == 1  # the refused calls gave the gate back


def test_a_retrying_request_stops_once_the_source_is_rested_meanwhile(clock):
    backend = Backend(ConnectionError("reset"), J({"late": True}))

    class OthersFailMeanwhile(Sleeper):
        async def __call__(self, seconds):
            await super().__call__(seconds)
            for _ in range(FAILS_TO_REST):  # other requests to the source fail while this one waits to retry
                http.record_failure("S", HttpError("S", "HTTP 502", 502))

    sleeper = OthersFailMeanwhile()
    http = Http(backend=backend, retries=2, sleep=sleeper)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://x.test", source="S"))
    assert str(e.value) == "S is resting after 3 failures (HTTP 502)" and e.value.status == 502
    assert e.value.source == "S"
    assert len(backend.calls) == 1 and sleeper.delays == [1.5]  # the retry never reached the backend
    h = http.health["S"]
    assert (h.failed, h.streak, h.rests, h.ok) == (3, 3, 1, 0)  # neither the network error nor the refusal counted
    assert h.last_error == "HTTP 502" and h.rest_until == clock.mono + REST_SECONDS[0]


def test_a_retrying_request_that_is_not_rested_keeps_trying(clock):
    backend = Backend(ConnectionError("reset"), J({"late": True}))

    class OthersFailOnce(Sleeper):
        async def __call__(self, seconds):
            await super().__call__(seconds)
            http.record_failure("S", HttpError("S", "HTTP 502", 502))  # not enough to rest the source

    http = Http(backend=backend, retries=2, sleep=OthersFailOnce())
    resp = asyncio.run(http.get("https://x.test", source="S"))
    assert resp.json() == {"late": True} and len(backend.calls) == 2
    h = http.health["S"]
    assert (h.ok, h.failed, h.streak, h.rests) == (1, 1, 0, 0) and not http.resting("S")


def test_forced_requests_ignore_the_rest_on_every_attempt(clock):
    http, backend, sleeper = make_http(Response(503, ""), retries=0)

    async def main():
        for _ in range(FAILS_TO_REST):
            with pytest.raises(HttpError):
                await http.get("https://x.test", source="S")
        assert http.resting("S")
        with pytest.raises(HttpError) as e:
            await http.get("https://x.test", source="S", force=True, retries=2)
        assert str(e.value) == "HTTP 503" and len(backend.calls) == FAILS_TO_REST + 3  # every retry was asked
        assert sleeper.delays == [1.5, 3.0]

    asyncio.run(main())
    h = http.health["S"]
    assert (h.failed, h.streak, h.rests) == (4, 4, 1)  # one forced call, one failure; the rest isn't lengthened


def test_forced_requests_queued_behind_the_gate_still_ask_a_rested_source(clock):
    http, backend, _ = make_http(ConnectionError("down"), retries=0, limits={"S": 1})

    async def main():
        return await asyncio.gather(*(http.get("https://x.test", source="S", force=True) for _ in range(5)),
                                    return_exceptions=True)

    results = asyncio.run(main())
    assert [str(r) for r in results] == ["ConnectionError: down"] * 5 and len(backend.calls) == 5
    h = http.health["S"]
    assert http.resting("S") and (h.failed, h.streak, h.rests) == (5, 5, 1)


# ----- proxies, headers, params -----

def test_each_source_uses_its_own_proxy():
    http, backend, _ = make_http(J({}), proxies={"Yahoo": "http://user:pw@proxy.test:8080", "Nasdaq": "",
                                                 "Coinbase": None})
    assert http.proxies == {"Yahoo": "http://user:pw@proxy.test:8080"}

    async def main():
        await http.get("https://query1.finance.yahoo.com/x", source="Yahoo")
        await http.get("https://api.nasdaq.com/x", source="Nasdaq")
        await http.get("https://api.exchange.coinbase.com/x", source="Coinbase")
        await http.get("https://example.test/x")

    asyncio.run(main())
    assert [c.proxy for c in backend.calls] == ["http://user:pw@proxy.test:8080", None, None, None]


def test_headers_are_copied_and_timeout_passed():
    http, backend, _ = make_http(J({}))
    headers = {"Accept": "application/json"}

    async def main():
        await http.get("https://x.test", headers=headers, timeout=7.5, source="S")
        await http.get("https://x.test", source="S")

    asyncio.run(main())
    first, second = backend.calls
    assert first.headers == headers and first.headers is not headers and first.timeout == 7.5
    assert second.headers == {} and second.timeout == TIMEOUT
    first.headers["X"] = "changed"
    assert "X" not in headers


def test_params_are_encoded_and_appended():
    http, backend, _ = make_http(J({}))

    async def main():
        await http.get("https://x.test/p", params={"q": "a b&c", "crumb": "x/y="})
        await http.get("https://x.test/p?fixed=1", params={"q": "z"})
        await http.get("https://x.test/p", params=[("symbol", "aapl|stocks"), ("symbol", "brk.b|stocks")])
        await http.get("https://x.test/p", params={})
        await http.get("https://x.test/p", params=None)

    asyncio.run(main())
    c = backend.calls
    assert c[0].query == {"q": "a b&c", "crumb": "x/y="} and c[0].url.startswith("https://x.test/p?")
    assert c[1].url.startswith("https://x.test/p?fixed=1&") and c[1].query == {"fixed": "1", "q": "z"}
    assert c[2].pairs == [("symbol", "aapl|stocks"), ("symbol", "brk.b|stocks")]
    assert c[3].url == c[4].url == "https://x.test/p"


# ----- concurrency -----

def test_each_source_has_its_own_concurrency_limit():
    state = {"A": [0, 0], "B": [0, 0]}  # in flight, peak

    class Slow:
        name = "slow"

        def __init__(self):
            self.release = asyncio.Event()

        async def get(self, url, headers, timeout, proxy=None):
            s = state["A" if "a.test" in url else "B"]
            s[0] += 1
            s[1] = max(s[1], s[0])
            await self.release.wait()
            s[0] -= 1
            return Response(200, "{}")

        async def close(self):
            pass

    async def main():
        backend = Slow()
        http = Http(backend=backend, limits={"A": 2}, sleep=Sleeper())
        tasks = [asyncio.create_task(http.get("https://a.test", source="A")) for _ in range(7)]
        tasks += [asyncio.create_task(http.get("https://b.test", source="B")) for _ in range(9)]
        for _ in range(20):
            await asyncio.sleep(0)
        assert state["A"][0] == 2 and state["B"][0] == 6  # A's limit, and the default of 6
        backend.release.set()
        results = await asyncio.gather(*tasks)
        assert all(r.status == 200 for r in results)
        assert http.health["A"].ok == 7 and http.health["B"].ok == 9

    asyncio.run(main())
    assert state["A"][1] == 2 and state["B"][1] == 6


def test_the_gate_is_released_after_errors_and_cancellation():
    async def main():
        release = asyncio.Event()

        class Hang:
            name = "hang"
            calls = 0

            async def get(self, url, headers, timeout, proxy=None):
                Hang.calls += 1
                if "hang" in url:
                    await release.wait()
                if "boom" in url:
                    raise OSError("boom")
                return Response(200, "")

            async def close(self):
                pass

        http = Http(backend=Hang(), limits={"S": 1}, retries=0, sleep=Sleeper())
        with pytest.raises(HttpError):
            await http.get("https://boom.test", source="S")
        assert (await http.get("https://ok.test", source="S")).status == 200
        stuck = asyncio.create_task(http.get("https://hang.test", source="S"))
        for _ in range(3):
            await asyncio.sleep(0)
        stuck.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stuck
        resp = await asyncio.wait_for(http.get("https://ok.test", source="S"), 1)
        assert resp.status == 200
        h = http.health["S"]
        assert (h.ok, h.failed) == (2, 1)  # the cancelled call counts as neither

    asyncio.run(main())


def _every_fifth_fails(url):
    n = int(query(url)["n"])
    if n % 5 == 0:
        return ConnectionError("reset")
    if n % 7 == 0:
        return Response(404, "")
    return Response(200, str(n))


def _sixty_calls_through_a_gate_of_three():
    backend = Backend().on("x.test", _every_fifth_fails)
    http = Http(backend=backend, retries=1, limits={"S": 3}, sleep=Sleeper())

    async def main():
        return await asyncio.gather(*(http.get("https://x.test", params={"n": n}, source="S") for n in range(1, 61)),
                                    return_exceptions=True)

    return asyncio.run(main()), http, backend


def test_many_concurrent_calls_keep_the_books_straight(clock, monkeypatch):
    """60 calls at once through a gate of 3: each call is counted exactly once, as ok or failed (the source is
    never rested here, so every call runs its course)."""
    monkeypatch.setattr(http_mod, "FAILS_TO_REST", 10**6)
    results, http, backend = _sixty_calls_through_a_gate_of_three()
    failed = [r for r in results if isinstance(r, HttpError)]
    assert len(failed) == 12 and all(r.status is None and str(r) == "ConnectionError: reset" for r in failed)
    assert all(r.text == str(n) for n, r in enumerate(results, 1) if isinstance(r, Response) and r.status == 200)
    h = http.health["S"]
    assert h.ok == 48 and h.failed == 12 and h.ok + h.failed == 60 and not http.resting("S")
    assert len(backend.calls) == 48 + 12 * 2  # each failure was tried twice
    assert http._gates["S"]._value == 3  # every slot came back


def test_many_concurrent_calls_stop_retrying_once_the_source_is_rested(clock):
    """The same 60 calls with the real rest: the failures' retries queue behind the first tries, the first three
    fail in a row and rest the source, and the other nine retries are refused without asking or being counted."""
    results, http, backend = _sixty_calls_through_a_gate_of_three()
    errors = {n: str(r) for n, r in enumerate(results, 1) if isinstance(r, HttpError)}
    assert errors == {**{n: "ConnectionError: reset" for n in (5, 10, 15)},
                      **{n: "S is resting after 3 failures (ConnectionError: reset)" for n in range(20, 61, 5)}}
    answered = {n: r.status for n, r in enumerate(results, 1) if isinstance(r, Response)}
    assert answered == {n: 404 if n % 7 == 0 else 200 for n in range(1, 61) if n % 5}
    h = http.health["S"]
    assert (h.ok, h.failed, h.streak, h.rests) == (48, 3, 3, 1) and http.resting("S")
    assert h.ok + h.failed + 9 == 60  # ok, failed or refused: every call is accounted for once
    asked = [int(c.query["n"]) for c in backend.calls]
    assert len(asked) == 60 + 3 and asked[60:] == [5, 10, 15]  # every first try, then only three retries
    assert sorted(asked[:60]) == list(range(1, 61))
    assert http._gates["S"]._value == 3


def test_cancellation_from_the_backend_is_not_a_failure_or_retried():
    http, backend, sleeper = make_http(asyncio.CancelledError())

    async def main():
        with pytest.raises(asyncio.CancelledError):
            await http.get("https://x.test", source="S")

    asyncio.run(main())
    h = http.health["S"]
    assert len(backend.calls) == 1 and sleeper.delays == [] and (h.ok, h.failed, h.streak) == (0, 0, 0)


# ----- error text -----

@pytest.mark.parametrize("text, expected", [
    ("GET https://q.test/v7?crumb=AbC/12x&symbols=AAPL failed",
     "GET https://q.test/v7?crumb=…&symbols=AAPL failed"),
    ("https://api.test/v1?apiKey=SECRET123", "https://api.test/v1?apiKey=…"),
    ("https://api.test/v1?api_key=SECRET&x=1", "https://api.test/v1?api_key=…&x=1"),
    ("https://api.test/v1?api-key=SECRET", "https://api.test/v1?api-key=…"),
    ("https://api.test/v1?KEY=SECRET other", "https://api.test/v1?KEY=… other"),
    ("access_token=abc.def.ghi", "access_token=…"),
    ("TOKEN=abc", "TOKEN=…"),
    ("Crumb=xyz", "Crumb=…"),
    ("first line\nsecond line", "first line second line"),
    ("crumb=a\nkey=b", "crumb=… key=…"),
    ("nothing secret here", "nothing secret here"),
    ("key= spaced", "key= spaced"),
])
def test_quiet_scrubs_keys_tokens_and_crumbs(text, expected):
    assert _quiet(text) == expected
    assert "SECRET" not in _quiet(text)


def test_error_messages_and_health_never_show_keys():
    http, backend, _ = make_http(ConnectionError("Failed to connect https://q.test/?crumb=SECRET1&apikey=SECRET2"),
                                 retries=0)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://q.test", source="S"))
    assert "SECRET" not in str(e.value) and "crumb=…" in str(e.value)
    assert "SECRET" not in http.health["S"].last_error

    http, backend, _ = make_http(Response(500, "bad request\ntoken=SECRET3"), retries=0)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://q.test", source="S"))
    assert str(e.value) == "HTTP 500 (bad request token=…)"


def test_error_bodies_are_shortened_and_html_left_out():
    http, backend, _ = make_http(Response(500, "x" * 500), retries=0)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://q.test", source="S"))
    assert str(e.value) == f"HTTP 500 ({'x' * 60})"

    http, backend, _ = make_http(Response(502, "<html><body>Bad gateway</body></html>"), retries=0)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://q.test", source="S"))
    assert str(e.value) == "HTTP 502"

    http, backend, _ = make_http(Response(503, "   \n  "), retries=0)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://q.test", source="S"))
    assert str(e.value) == "HTTP 503"

    http, backend, _ = make_http(OSError("y" * 400), retries=0)
    with pytest.raises(HttpError) as e:
        asyncio.run(http.get("https://q.test", source="S"))
    assert str(e.value) == "OSError: " + "y" * 160
    assert len(http.health["S"].last_error) == 120


# ----- health -----

def test_health_line_reads_well(clock):
    http, _, _ = make_http(J({}))
    h = http.source("S")
    assert h.line() == "not used yet" and not h.failing and not h.resting()
    http.record_ok("S", 200)
    ok_at = int(clock.wall)
    assert h.line() == f"{CHECK} ok <t:{ok_at}:R>"
    clock.advance(100)
    http.record_failure("S", HttpError("S", "HTTP 503 (busy)", 503))
    assert h.line() == f"{WARN} failing (HTTP 503 (busy)) since <t:{ok_at + 100}:R> {DOT} last worked <t:{ok_at}:R>"
    assert h.failing
    clock.advance(100)
    http.record_ok("S", 200)
    assert h.line() == f"{CHECK} ok <t:{ok_at + 200}:R> {DOT} 1 failed calls"

    fresh = http.source("T")
    http.record_failure("T", HttpError("T", "ConnectionError: boom"))
    assert fresh.line() == f"{WARN} failing (ConnectionError: boom) since <t:{ok_at + 200}:R> {DOT} last worked never"
    assert Health(failed=2, last_error="old trouble").line() == f"{WARN} old trouble"
    assert Health(streak=1).line() == f"{WARN} failing (failing) since never {DOT} last worked never"


def test_health_resting_takes_an_explicit_time():
    h = Health(rest_until=100.0)
    assert h.resting(99.9) and not h.resting(100.0) and not h.resting(1e12)


# ----- responses and transports -----

def test_response_json():
    assert Response(200, "").json() is None and Response(200, "  \n").json() is None
    assert Response(200, '[1, {"a": null}]').json() == [1, {"a": None}]
    with pytest.raises(HttpError) as e:
        Response(502, "Edge: Too Many Requests").json()
    assert e.value.status == 502 and "not JSON (HTTP 502)" in str(e.value) and "Edge: Too Many" in str(e.value)


def test_http_error_keeps_source_and_status():
    e = HttpError("Nasdaq", "Nasdaq: HTTP 404", 404)
    assert (e.source, e.status, str(e)) == ("Nasdaq", 404, "Nasdaq: HTTP 404")
    assert HttpError("x", "y").status is None


def test_transport_name_and_close():
    http, backend, _ = make_http()
    assert http.transport == "fake browser"
    asyncio.run(http.close())
    assert backend.closed

    class Nameless:
        async def close(self):
            pass

    assert Http(backend=Nameless()).transport == "Nameless"


def test_default_backend_is_curl_and_falls_back_to_aiohttp(monkeypatch):
    assert http_mod.default_backend().name == "curl_cffi (Chrome)"
    assert Http().transport == "curl_cffi (Chrome)"
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)  # as if it weren't installed
    backend = http_mod.default_backend()
    assert isinstance(backend, http_mod._Aiohttp) and backend.name == "aiohttp"


def test_ca_bundle_prefers_the_hosts_bundle(monkeypatch):
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert http_mod.ca_bundle() is True
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/b.pem")
    assert http_mod.ca_bundle() == "/etc/ssl/b.pem"
    monkeypatch.setenv("CURL_CA_BUNDLE", "/etc/ssl/a.pem")
    assert http_mod.ca_bundle() == "/etc/ssl/a.pem"


def test_curl_backend_passes_proxy_only_when_set():
    class Session:
        def __init__(self):
            self.calls, self.closed = [], 0

        async def get(self, url, **kw):
            self.calls.append((url, kw))
            return SimpleNamespace(status_code=203, text=None, url="https://final.test/")

        async def close(self):
            self.closed += 1

    curl = http_mod._Curl()
    session = curl._session = Session()

    async def main():
        r1 = await curl.get("https://x.test", {"A": "1"}, 9.0)
        r2 = await curl.get("https://x.test", {}, 5.0, "http://proxy.test:1")
        await curl.close()
        await curl.close()
        return r1, r2

    r1, r2 = asyncio.run(main())
    assert r1 == Response(203, "", "https://final.test/")
    assert session.calls[0][1] == {"headers": {"A": "1"}, "timeout": 9.0, "allow_redirects": True}
    assert session.calls[1][1]["proxy"] == "http://proxy.test:1"
    assert session.closed == 1 and curl._session is None


def test_aiohttp_backend_passes_timeout_and_proxy():
    seen = {}

    class Resp:
        status = 418
        url = "https://final.test/"

        async def text(self, errors="strict"):
            seen["errors"] = errors
            return "teapot"

    class Ctx:
        async def __aenter__(self):
            return Resp()

        async def __aexit__(self, *exc):
            return False

    class Session:
        closed = False

        def get(self, url, headers=None, timeout=None, proxy=None):
            seen.update(url=url, headers=headers, timeout=timeout, proxy=proxy)
            return Ctx()

        async def close(self):
            Session.closed = True

    backend = http_mod._Aiohttp()
    backend._session = Session()

    async def main():
        r = await backend.get("https://x.test", {"H": "v"}, 12.0, "http://proxy.test:2")
        await backend.close()
        return r

    r = asyncio.run(main())
    assert r == Response(418, "teapot", "https://final.test/")
    assert seen["timeout"].total == 12.0 and seen["proxy"] == "http://proxy.test:2" and seen["headers"] == {"H": "v"}
    assert seen["errors"] == "replace" and Session.closed and backend._session is None


# =====================================================================================================================
# yahoo.py (the client)
# =====================================================================================================================

FC = "https://fc.yahoo.com"
GETCRUMB = f"{BASE}/v1/test/getcrumb"
V7 = "/v7/finance/quote?"
SPARK = "/v7/finance/spark"
CHART = "/v8/finance/chart/"
T0 = utc(2026, 1, 5, 14, 30)


def yahoo_client(backend, retries=0):
    http = Http(backend=backend, retries=retries, sleep=Sleeper())
    return YahooClient(http), http


def with_crumb(backend=None, *crumbs):
    backend = backend or Backend()
    return backend.on("fc.yahoo.com", Response(404, "<html>not here</html>")).on(
        "getcrumb", *(Response(200, c) for c in (crumbs or ("crumb-1",))))


def v7_row(symbol, price=100.0, prev=99.0, state="REGULAR", **extra):
    return {"symbol": symbol, "shortName": f"{symbol} Corp", "regularMarketPrice": price,
            "regularMarketPreviousClose": prev, "regularMarketChangePercent": (price / prev - 1) * 100,
            "marketState": state, "quoteType": "EQUITY", "currency": "USD", **extra}


def v7_answer(prices: dict):
    def answer(url):
        syms = query(url)["symbols"].split(",")
        return J({"quoteResponse": {"result": [v7_row(s, prices[s]) for s in syms if s in prices], "error": None}})
    return answer


def spark_entry(symbol, closes, key=True):
    ts = [T0 + i * DAY for i in range(len(closes))]
    valid = [c for c in closes if c is not None]
    meta = {"symbol": symbol, "shortName": f"{symbol} Corp", "currency": "USD"}
    if valid:
        meta.update(regularMarketPrice=valid[-1], previousClose=valid[0])
    entry = {"response": [{"meta": meta, "timestamp": ts, "indicators": {"quote": [{"close": list(closes)}]}}]}
    if key:
        entry["symbol"] = symbol
    return entry


def spark_answer(closes: dict):
    def answer(url):
        syms = query(url)["symbols"].split(",")
        return J({"spark": {"result": [spark_entry(s, closes[s]) for s in syms if s in closes], "error": None}})
    return answer


def chart_body(symbol="AAPL", adj=True, meta=None):
    q = {"open": [9.5, 10.5, 11.5], "high": [10.5, 11.5, 12.5], "low": [9.0, 10.0, 11.0],
         "close": [10.0, 11.0, 12.0], "volume": [100, 200, 300]}
    ind = {"quote": [q]}
    if adj:
        ind["adjclose"] = [{"adjclose": [5.0, 5.5, 6.0]}]
    m = {"symbol": symbol, "currency": "USD", **(meta or {})} if symbol else dict(meta or {})
    return {"chart": {"result": [{"meta": m, "timestamp": [T0, T0 + DAY, T0 + 2 * DAY], "indicators": ind}],
                      "error": None}}


# ----- the crumb -----

COOKIE = "Yahoo cookie"


def test_crumb_comes_from_the_cookie_page_then_getcrumb(clock):
    backend = with_crumb(None, "  AbC/1.2x \n")
    y, http = yahoo_client(backend, retries=2)
    assert asyncio.run(y.crumb()) == "AbC/1.2x"
    assert [c.url for c in backend.calls] == [FC, GETCRUMB]
    fc, getcrumb = backend.calls
    assert fc.timeout == 10 and getcrumb.timeout == TIMEOUT
    h = http.health["Yahoo"]
    assert (h.ok, h.failed, h.streak, h.status) == (1, 0, 0, 200)  # only getcrumb counts for Yahoo
    # fc.yahoo.com's 404 is its normal answer; it's kept as its own source and can't make Yahoo look healthy.
    cookie = http.health[COOKIE]
    assert (cookie.ok, cookie.failed, cookie.status, cookie.last_ok) == (1, 0, 404, clock.wall)


def test_the_cookie_page_alone_does_not_make_yahoo_look_healthy():
    backend = Backend().on("fc.yahoo.com", 404).on("getcrumb", Response(500, "down"))
    y, http = yahoo_client(backend)
    for _ in range(FAILS_TO_REST - 1):
        http.record_failure("Yahoo", HttpError("Yahoo", "HTTP 502", 502))
    with pytest.raises(YahooError) as e:
        asyncio.run(y.crumb())
    assert e.value.status == 500 and str(e.value) == "Yahoo: HTTP 500 (down)"
    h = http.health["Yahoo"]
    assert (h.ok, h.failed, h.streak) == (0, 3, 3) and y.resting  # the cookie's 404 didn't break the streak
    assert http.health[COOKIE].ok == 1 and not http.health[COOKIE].failing


@pytest.mark.parametrize("fc_answer, fc_failed", [
    (ConnectionError("Could not resolve host: fc.yahoo.com"), "ConnectionError: Could not resolve host: fc.yahoo.com"),
    (Response(503, ""), "HTTP 503"), (Response(429, "Too Many Requests"), "HTTP 429 (Too Many Requests)"),
    (Response(403, "denied"), "HTTP 403 (denied)"), (TimeoutError("timed out"), "TimeoutError: timed out"),
])
def test_a_failing_cookie_page_does_not_stop_getcrumb(fc_answer, fc_failed):
    backend = Backend().on("fc.yahoo.com", fc_answer).on("getcrumb", Response(200, "good"))
    y, http = yahoo_client(backend, retries=2)
    assert asyncio.run(y.crumb()) == "good"
    assert backend.calls[-1].url == GETCRUMB and len(backend.calls_to("getcrumb")) == 1
    assert len(backend.calls_to("fc.yahoo.com")) == 1  # the optional visit is never retried
    h = http.health["Yahoo"]
    assert (h.ok, h.failed, h.streak) == (1, 0, 0) and not h.failing
    cookie = http.health[COOKIE]
    assert (cookie.ok, cookie.failed, cookie.streak, cookie.last_error) == (0, 1, 1, fc_failed)


def test_a_failing_cookie_page_cannot_rest_yahoo_before_getcrumb():
    backend = Backend().on("fc.yahoo.com", ConnectionError("Could not resolve host: fc.yahoo.com")).on(
        "getcrumb", Response(200, "good"))
    y, http = yahoo_client(backend)
    for _ in range(FAILS_TO_REST - 1):  # two earlier failed calls (Yahoo isn't resting yet)
        http.record_failure("Yahoo", HttpError("Yahoo", "HTTP 502", 502))
    assert not y.resting
    assert asyncio.run(y.crumb()) == "good"
    h = http.health["Yahoo"]
    assert (h.ok, h.failed, h.streak, h.rests) == (1, 2, 0, 0) and not y.resting
    assert http.health[COOKIE].failed == 1 and len(backend.calls_to("getcrumb")) == 1


def test_a_cookie_page_that_keeps_failing_rests_itself_not_yahoo(clock, monkeypatch):
    monkeypatch.setattr(yahoo_mod, "CRUMB_TTL", 0)  # every call fetches a fresh crumb
    backend = Backend().on("fc.yahoo.com", Response(503, "")).on("getcrumb", *(Response(200, f"c{i}")
                                                                              for i in range(1, 6)))
    y, http = yahoo_client(backend)

    async def main():
        return [await y.crumb() for _ in range(4)]

    assert asyncio.run(main()) == ["c1", "c2", "c3", "c4"]
    assert len(backend.calls_to("fc.yahoo.com")) == FAILS_TO_REST  # resting: the 4th visit wasn't made
    assert len(backend.calls_to("getcrumb")) == 4
    cookie = http.health[COOKIE]
    assert (cookie.failed, cookie.streak, cookie.rests) == (3, 3, 1)
    assert http.health["Yahoo"].ok == 4 and not http.health["Yahoo"].failing and not y.resting


def test_a_resting_yahoo_refuses_getcrumb_whatever_the_cookie_page_says():
    backend = with_crumb(None, "never")
    y, http = yahoo_client(backend)
    for _ in range(FAILS_TO_REST):
        http.record_failure("Yahoo", HttpError("Yahoo", "HTTP 502", 502))
    with pytest.raises(YahooError) as e:
        asyncio.run(y.crumb())
    assert str(e.value) == "Yahoo: Yahoo is resting after 3 failures (HTTP 502)" and e.value.status == 502
    assert not backend.calls_to("getcrumb") and http.health["Yahoo"].ok == 0


def test_crumb_is_cached_for_six_hours(clock):
    backend = with_crumb(None, "first", "second")
    y, _ = yahoo_client(backend)

    async def main():
        assert await y.crumb() == "first"
        clock.advance(CRUMB_TTL - 1)
        assert await y.crumb() == "first"
        clock.advance(2)
        assert await y.crumb() == "second"

    asyncio.run(main())
    assert len(backend.calls_to("getcrumb")) == 2 and len(backend.calls_to("fc.yahoo.com")) == 2


def test_concurrent_callers_share_one_crumb_fetch():
    backend = with_crumb(None, "shared", "other")
    y, _ = yahoo_client(backend)

    async def main():
        return await asyncio.gather(*(y.crumb() for _ in range(8)))

    assert asyncio.run(main()) == ["shared"] * 8
    assert len(backend.calls_to("getcrumb")) == 1


@pytest.mark.parametrize("text", ["", "   ", "<!DOCTYPE html><html>blocked</html>", "two words", "x" * 41])
def test_bad_crumb_text_is_rejected_and_not_kept(text):
    backend = Backend().on("fc.yahoo.com", 404).on("getcrumb", Response(200, text), Response(200, "fine"))
    y, _ = yahoo_client(backend)

    async def main():
        with pytest.raises(YahooError) as e:
            await y.crumb()
        assert "couldn't get a Yahoo crumb (HTTP 200)" in str(e.value) and e.value.status == 200
        assert await y.crumb() == "fine"

    asyncio.run(main())


def test_crumb_of_forty_characters_is_fine():
    y, _ = yahoo_client(with_crumb(None, "c" * 40))
    assert asyncio.run(y.crumb()) == "c" * 40


@pytest.mark.parametrize("status, message", [(401, "couldn't get a Yahoo crumb (HTTP 401)"),
                                             (403, "couldn't get a Yahoo crumb (HTTP 403)"),
                                             (404, "couldn't get a Yahoo crumb (HTTP 404)"),
                                             (429, "Yahoo: HTTP 429 (slow down)"),
                                             (500, "Yahoo: HTTP 500 (slow down)")])
def test_crumb_errors_keep_the_status(status, message):
    backend = Backend().on("fc.yahoo.com", 404).on("getcrumb", Response(status, "slow down"))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.crumb())
    assert e.value.status == status and str(e.value) == message


def test_a_rejected_crumb_is_refreshed_once():
    backend = with_crumb(None, "c1", "c2").on(V7, Response(401, "Invalid Crumb"), v7_answer({"AAPL": 227.5}))
    y, http = yahoo_client(backend)
    out = asyncio.run(y.quotes(["AAPL"]))
    assert out["AAPL"].price == 227.5 and out["AAPL"].source == "Yahoo" and out["AAPL"].name == "AAPL Corp"
    assert [c.query["crumb"] for c in backend.calls_to(V7)] == ["c1", "c2"]
    assert not backend.calls_to(SPARK) and not http.health["Yahoo"].failing


def test_a_forbidden_crumb_is_refreshed_once_too():
    backend = with_crumb(None, "c1", "c2").on(V7, Response(403, ""), v7_answer({"AAPL": 1.0}))
    y, _ = yahoo_client(backend)
    data = asyncio.run(y.get_json(f"{BASE}/v7/finance/quote", {"symbols": "AAPL"}, crumb=True))
    assert data["quoteResponse"]["result"][0]["symbol"] == "AAPL"
    assert len(backend.calls_to("getcrumb")) == 2


def test_a_crumb_rejected_twice_gives_up_with_401():
    backend = with_crumb(None, "c1", "c2", "c3").on(V7, Response(401, "Invalid Crumb"))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.get_json(f"{BASE}/v7/finance/quote", {"symbols": "AAPL"}, crumb=True))
    assert e.value.status == 401
    assert len(backend.calls_to(V7)) == 2 and len(backend.calls_to("getcrumb")) == 2


def _stale_crumb_backend():
    good = v7_answer({"AAPL": 1.0, "MSFT": 2.0})

    def quote(url):
        return Response(401, "Invalid Crumb") if query(url)["crumb"] == "c1" else good(url)

    return with_crumb(None, "c1", "c2", "c3", "c4").on(V7, quote).on(SPARK, spark_answer({"AAPL": [7.0],
                                                                                          "MSFT": [8.0]}))


def test_concurrent_rejections_refresh_the_crumb_without_deadlock():
    backend = _stale_crumb_backend()
    y, http = yahoo_client(backend)
    symbols = ("AAPL", "MSFT")

    async def main():
        await y.crumb()  # everyone starts with c1
        return await asyncio.wait_for(asyncio.gather(*(y.quotes([s]) for s in symbols)), 5)

    results = asyncio.run(main())
    assert [{s: q.price for s, q in r.items()} for r in results] == [{"AAPL": 1.0}, {"MSFT": 2.0}]
    assert not backend.calls_to(SPARK)
    assert len(backend.calls_to("getcrumb")) == 2  # the first fetch, then one refresh shared by both callers
    assert [c.query["crumb"] for c in backend.calls_to(V7)] == ["c1", "c1", "c2", "c2"]
    h = http.health["Yahoo"]
    assert (h.failed, h.streak) == (0, 0) and not y.resting  # an expired crumb isn't Yahoo failing


def test_many_concurrent_rejections_renew_the_crumb_without_resting_yahoo():
    """An expired crumb is routine: even many requests turned away at once share one renewal and all get their
    quotes, and the first rejections don't count against Yahoo (they used to rest it for 30 seconds)."""
    backend = _stale_crumb_backend()
    y, http = yahoo_client(backend)
    symbols = ("AAPL", "MSFT", "AAPL", "MSFT", "AAPL", "MSFT")

    async def main():
        await y.crumb()
        return await asyncio.wait_for(asyncio.gather(*(y.quotes([s]) for s in symbols)), 5)

    results = asyncio.run(main())
    assert [{s: q.price for s, q in r.items()} for r in results] == [{"AAPL": 1.0}, {"MSFT": 2.0}] * 3
    assert [c.query["crumb"] for c in backend.calls_to(V7)] == ["c1"] * 6 + ["c2"] * 6
    assert len(backend.calls_to("getcrumb")) == 2 and not backend.calls_to(SPARK)
    h = http.health["Yahoo"]
    assert (h.failed, h.streak, h.rests) == (0, 0, 0) and not y.resting
    assert not http.health[COOKIE].failing


def test_a_crumb_rejected_again_after_renewal_does_count():
    """The renewed crumb being refused too is a real failure: that one is counted."""
    backend = (with_crumb(None, "c1").on(V7, Response(401, '{"finance":{"error":{"description":"Invalid Crumb"}}}'))
               .on(SPARK, Response(200, '{"spark": {"result": []}}')))
    y, http = yahoo_client(backend)
    assert asyncio.run(y.quotes(["AAPL"])) == {}  # v7 refused twice, and the spark fallback had nothing
    assert len(backend.calls_to(V7)) == 2  # once with the cached crumb, once after renewing it
    assert http.health["Yahoo"].failed == 1


def test_a_404_with_a_crumb_does_not_show_the_crumb():
    backend = with_crumb(None, "SeCrEt").on("/quoteSummary/", Response(404, ""))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.summary("NOPE", ("price",)))
    assert e.value.status == 404 and "SeCrEt" not in str(e.value)
    assert backend.calls_to("/quoteSummary/")[0].query["crumb"] == "SeCrEt"


def test_401_without_a_crumb_is_an_error_and_fetches_no_crumb():
    backend = Backend().on(CHART, Response(401, "Unauthorized"))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("AAPL"))
    assert e.value.status == 401 and str(e.value) == "Yahoo: HTTP 401"
    assert len(backend.calls) == 1


# ----- errors -----

def test_404_is_a_not_found_error_and_yahoo_still_counts_as_working():
    backend = Backend().on(CHART, Response(404, '{"chart":{"error":{"code":"Not Found"}}}'))
    y, http = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("NOPE"))
    assert e.value.status == 404 and "not found" in str(e.value) and "/v8/finance/chart/NOPE" in str(e.value)
    assert not http.health["Yahoo"].failing and not y.resting


def test_rate_limit_text_shows_in_the_error_and_is_not_retried():
    backend = Backend().on(CHART, Response(429, "Edge: Too Many Requests"))
    y, http = yahoo_client(backend, retries=2)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("AAPL"))
    assert e.value.status == 429 and str(e.value) == "Yahoo: HTTP 429 (Edge: Too Many Requests)"
    assert len(backend.calls) == 1 and http.health["Yahoo"].failing


def test_server_error_text_shows_in_the_error_after_retries():
    backend = Backend().on(CHART, Response(500, "Internal Server Error"))
    y, _ = yahoo_client(backend, retries=2)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("AAPL"))
    assert e.value.status == 500 and str(e.value) == "Yahoo: HTTP 500 (Internal Server Error)"
    assert len(backend.calls) == 3


def test_a_non_json_answer_is_an_error_showing_the_text():
    backend = Backend().on(CHART, Response(200, "Edge: Too Many Requests"))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("AAPL"))
    assert e.value.status == 200 and "not JSON" in str(e.value) and "Edge: Too Many Requests" in str(e.value)


@pytest.mark.parametrize("status", [400, 410, 451])
def test_other_client_errors_keep_their_status(status):
    backend = Backend().on(CHART, Response(status, ""))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("AAPL"))
    assert e.value.status == status and str(e.value) == f"Yahoo: HTTP {status}"


def test_a_resting_yahoo_raises_without_asking(clock):
    backend = Backend().on(CHART, Response(503, ""))
    y, _ = yahoo_client(backend)

    async def main():
        for _ in range(3):
            with pytest.raises(YahooError):
                await y.daily("AAPL")
        assert y.resting
        with pytest.raises(YahooError) as e:
            await y.daily("AAPL")
        assert "resting" in str(e.value) and e.value.status == 503

    asyncio.run(main())
    assert len(backend.calls) == 3


@pytest.mark.parametrize("body", [{"chart": {"result": None, "error": {"code": "Not Found"}}},
                                  {"chart": {"result": [], "error": None}}, {}, None])
def test_an_empty_chart_is_an_error(body):
    backend = Backend().on(CHART, Response(200, "" if body is None else json.dumps(body)))
    y, _ = yahoo_client(backend)
    with pytest.raises(YahooError) as e:
        asyncio.run(y.daily("AAPL"))
    assert "no chart for AAPL" in str(e.value)


# ----- prices -----

def test_daily_tags_its_source_and_asks_for_all_history(clock):
    backend = Backend().on(CHART, J(chart_body()))
    y, _ = yahoo_client(backend)
    bars = asyncio.run(y.daily("AAPL"))
    assert bars.symbol == "AAPL" and bars.meta["source"] == "Yahoo" and bars.meta["currency"] == "USD"
    assert list(bars.close) == [5.0, 5.5, 6.0]  # adjusted
    assert list(bars.open) == pytest.approx([4.75, 5.25, 5.75])
    assert list(bars.t) == [T0, T0 + DAY, T0 + 2 * DAY] and list(bars.volume) == [100, 200, 300]
    call = backend.calls[0]
    assert call.path == "/v8/finance/chart/AAPL"
    assert call.query == {"interval": "1d", "includeAdjustedClose": "true", "events": "div,splits",
                          "includePrePost": "false", "period1": str(EARLIEST),
                          "period2": str(int(clock.wall + 86400))}


def test_daily_from_a_start_and_without_a_symbol_in_meta():
    backend = Backend().on(CHART, J(chart_body(symbol="")))
    y, _ = yahoo_client(backend)
    bars = asyncio.run(y.daily("MSFT", start=1_700_000_000))
    assert bars.symbol == "MSFT" and bars.meta["source"] == "Yahoo"
    assert backend.calls[0].query["period1"] == "1700000000"


def test_intraday_asks_for_range_and_is_not_adjusted():
    backend = Backend().on(CHART, J(chart_body()))
    y, _ = yahoo_client(backend)
    bars = asyncio.run(y.intraday("AAPL", range_="5d", interval="15m"))
    assert list(bars.close) == [10.0, 11.0, 12.0] and "source" not in bars.meta
    q = backend.calls[0].query
    assert q["range"] == "5d" and q["interval"] == "15m" and q["includePrePost"] == "true" and "period1" not in q


def test_spark_bars_parses_closes_and_leaves_out_unknown_symbols():
    backend = Backend().on(SPARK, spark_answer({"AAPL": [1.0, 2.0, None, 3.0], "MSFT": [], "BAD": [None, None]}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.spark_bars(["AAPL", "MSFT", "BAD", "NOPE"], range_="5y", interval="1wk"))
    assert set(out) == {"AAPL"}
    a = out["AAPL"]
    assert a.symbol == "AAPL" and list(a.close) == [1.0, 2.0, 3.0]
    assert list(a.t) == [T0, T0 + DAY, T0 + 3 * DAY]
    assert np.array_equal(a.open, a.close) and np.array_equal(a.high, a.close) and np.array_equal(a.low, a.close)
    assert not a.volume.any()
    assert backend.calls[0].query == {"symbols": "AAPL,MSFT,BAD,NOPE", "range": "5y", "interval": "1wk"}


def test_spark_bars_defaults_and_symbol_from_meta():
    def answer(url):
        return J({"spark": {"result": [spark_entry("ETH-USD", [10.0, 11.0], key=False)], "error": None}})

    backend = Backend().on(SPARK, answer)
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.spark_bars(["ETH-USD"]))
    assert list(out) == ["ETH-USD"] and list(out["ETH-USD"].close) == [10.0, 11.0]
    assert backend.calls[0].query["range"] == "1y" and backend.calls[0].query["interval"] == "1d"


@pytest.mark.parametrize("body", ["", "{}", '{"spark": null}', '{"spark": {"result": null}}',
                                  '{"spark": {"result": [{"symbol": "AAPL"}]}}',
                                  '{"spark": {"result": [{"symbol": "AAPL", "response": null}]}}'])
def test_spark_bars_with_empty_answers(body):
    y, _ = yahoo_client(Backend().on(SPARK, Response(200, body)))
    assert asyncio.run(y.spark_bars(["AAPL"])) == {}


def test_spark_bars_takes_at_most_20_symbols():
    backend = Backend().on(SPARK, spark_answer({f"S{i}": [1.0] for i in range(21)}))
    y, _ = yahoo_client(backend)
    with pytest.raises(ValueError):
        asyncio.run(y.spark_bars([f"S{i}" for i in range(21)]))
    assert not backend.calls
    assert len(asyncio.run(y.spark_bars([f"S{i}" for i in range(20)]))) == 20
    assert len(backend.calls) == 1


def test_quotes_use_v7_and_fall_back_to_spark_for_the_rest():
    backend = with_crumb().on(V7, v7_answer({"AAPL": 227.5})).on(SPARK, spark_answer({"MSFT": [400.0, 410.0]}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.quotes(["AAPL", "MSFT", "AAPL", "NOPE"]))
    assert set(out) == {"AAPL", "MSFT"}
    assert out["AAPL"].price == 227.5 and out["AAPL"].market_state == "REGULAR"
    assert out["MSFT"].price == 410.0 and out["MSFT"].prev_close == 400.0
    assert out["MSFT"].change_pct == pytest.approx(2.5) and out["MSFT"].source == "Yahoo"
    assert backend.calls_to(V7)[0].query["symbols"] == "AAPL,MSFT,NOPE"
    assert backend.calls_to(SPARK)[0].query == {"symbols": "MSFT,NOPE", "range": "1d", "interval": "1d"}


@pytest.mark.parametrize("crumb_answer", [Response(500, ""), Response(200, "<html>"), Response(429, ""),
                                          ConnectionError("down")])
def test_quotes_fall_back_to_spark_when_no_crumb(crumb_answer):
    backend = Backend().on("fc.yahoo.com", 404).on("getcrumb", crumb_answer).on(
        SPARK, spark_answer({"AAPL": [1.0, 2.0], "BTC-USD": [3.0, 4.0]}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.quotes(["AAPL", "BTC-USD"]))
    assert {s: q.price for s, q in out.items()} == {"AAPL": 2.0, "BTC-USD": 4.0}
    assert not backend.calls_to(V7)


def test_quotes_fall_back_to_spark_when_the_crumb_is_rejected_twice():
    backend = with_crumb(None, "c1", "c2").on(V7, Response(401, "")).on(SPARK, spark_answer({"AAPL": [5.0]}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.quotes(["AAPL"]))
    assert out["AAPL"].price == 5.0 and len(backend.calls_to(V7)) == 2


def test_quotes_go_in_batches_of_40():
    symbols = [f"T{i:02d}" for i in range(45)]
    backend = with_crumb().on(V7, v7_answer({s: 10.0 + i for i, s in enumerate(symbols)}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.quotes(symbols))
    assert len(out) == 45 and out["T44"].price == 54.0
    assert [len(c.query["symbols"].split(",")) for c in backend.calls_to(V7)] == [40, 5]
    assert not backend.calls_to(SPARK) and len(backend.calls_to("getcrumb")) == 1


def test_a_failing_v7_batch_keeps_the_earlier_batches_and_sparks_the_rest():
    symbols = [f"T{i:02d}" for i in range(45)]
    good = v7_answer({s: 10.0 for s in symbols})

    def quote(url):
        return Response(500, "busy") if "T44" in query(url)["symbols"] else good(url)

    backend = with_crumb().on(V7, quote).on(SPARK, spark_answer({s: [20.0] for s in symbols}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.quotes(symbols))
    assert len(out) == 45 and out["T00"].price == 10.0 and out["T39"].price == 10.0 and out["T40"].price == 20.0
    assert [c.query["symbols"] for c in backend.calls_to(SPARK)] == [",".join(symbols[40:])]


def test_quotes_of_nothing_ask_nothing():
    backend = Backend()
    y, _ = yahoo_client(backend)
    assert asyncio.run(y.quotes([])) == {} and not backend.calls


@pytest.mark.parametrize("state, ext", [("PRE", 105.0), ("PREPRE", 105.0), ("POST", 95.0), ("CLOSED", 95.0),
                                        ("REGULAR", None)])
def test_v7_quotes_carry_extended_hours_prices(state, ext):
    row = v7_row("AAPL", 100.0, 99.0, state=state, preMarketPrice=105.0, preMarketChangePercent=5.0,
                 postMarketPrice=95.0, postMarketChangePercent=-5.0, marketCap=3e12, trailingPE=None)
    backend = with_crumb().on(V7, J({"quoteResponse": {"result": [row, {"symbol": "NOPRICE"}]}})).on(
        SPARK, spark_answer({}))
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.quotes(["AAPL", "NOPRICE"]))
    q = out["AAPL"]
    assert q.price == 100.0 and q.market_state == state and q.ext_price == ext
    assert q.ext_change_pct == (None if ext is None else (5.0 if ext > 100 else -5.0))
    assert q.extra == {"marketCap": 3e12} and "NOPRICE" not in out


def test_spark_quotes_skip_a_failing_batch():
    symbols = [f"S{i:02d}" for i in range(25)]
    good = spark_answer({s: [1.0, 2.0] for s in symbols})

    def answer(url):
        return Response(404, "") if "S00" in query(url)["symbols"] else good(url)

    backend = Backend().on(SPARK, answer)
    y, _ = yahoo_client(backend)
    out = asyncio.run(y.spark_quotes(symbols))
    assert sorted(out) == symbols[20:]
    assert [len(c.query["symbols"].split(",")) for c in backend.calls_to(SPARK)] == [20, 5]


# ----- research endpoints -----

def test_summary_options_search_and_screener():
    backend = (with_crumb().on("/v10/finance/quoteSummary/", J({"quoteSummary": {"result": [{"price": {"a": 1}}]}}))
               .on("/v7/finance/options/", J({"optionChain": {"result": [{"expirationDates": [1, 2]}]}}))
               .on("/v1/finance/search", J({"quotes": [{"symbol": "AAPL"}], "news": [{"title": "x"}]}))
               .on("/v1/finance/screener/", J({"finance": {"result": [{"quotes": [{"symbol": "NVDA"}]}]}})))
    y, _ = yahoo_client(backend)

    async def main():
        assert await y.summary("AAPL", ("price", "summaryDetail")) == {"price": {"a": 1}}
        assert await y.options("AAPL", 1_800_000_000) == {"expirationDates": [1, 2]}
        assert await y.search("apple", news=3) == ([{"symbol": "AAPL"}], [{"title": "x"}])
        assert await y.screener("day_gainers", 10) == [{"symbol": "NVDA"}]

    asyncio.run(main())
    s = backend.calls_to("quoteSummary")[0]
    assert s.query == {"modules": "price,summaryDetail", "crumb": "crumb-1"}
    assert backend.calls_to("/options/")[0].query == {"date": "1800000000", "crumb": "crumb-1"}
    assert backend.calls_to("/search")[0].query == {"q": "apple", "newsCount": "3", "quotesCount": "8",
                                                    "enableFuzzyQuery": "false"}
    assert backend.calls_to("/screener/")[0].query == {"scrIds": "day_gainers", "count": "10"}


def test_research_endpoints_with_empty_answers():
    backend = (with_crumb().on("/quoteSummary/", J({"quoteSummary": {"result": None}}))
               .on("/options/", Response(200, "")).on("/search", J({}))
               .on("/screener/", J({"finance": {"result": [None]}})))
    y, _ = yahoo_client(backend)

    async def main():
        assert await y.summary("AAPL", ("price",)) == {}
        assert await y.options("AAPL") == {}
        assert await y.search("x") == ([], [])
        assert await y.screener("most_actives") == []

    asyncio.run(main())
    assert backend.calls_to("/options/")[0].query == {"crumb": "crumb-1"}


def test_client_shares_the_transport_without_closing_it():
    backend = Backend()
    y, http = yahoo_client(backend)
    assert y.http is http and not y.resting
    asyncio.run(y.close())
    assert not backend.closed


# =====================================================================================================================
# backup.py helpers
# =====================================================================================================================

@pytest.mark.parametrize("text, expected", [
    ("$1,234.50", 1234.5), ("-0.09%", -0.09), ("+1.73", 1.73), ("$0.00", 0.0), ("0", 0.0), ("1,234,567", 1234567.0),
    (" $ 12 ", 12.0), ("1e3", 1000.0), ("-$5.25", -5.25),
    ("--", None), ("-", None), ("N/A", None), ("NA", None), ("", None), ("   ", None), (None, None), ("UNCH", None),
    ("abc", None), ("1.2.3", None), ("NaN", None), ("inf", None), ("-inf%", None),
    (float("nan"), None), (float("inf"), None), (np.float64("nan"), None),
    (5, 5.0), (0, 0.0), (-3, -3.0), (2.5, 2.5), (np.int64(7), 7.0), (np.float64(1.5), 1.5),
])
def test_number_reads_nasdaq_numbers(text, expected):
    got = number(text)
    assert got == expected
    if expected is not None:
        assert type(got) is float


@pytest.mark.parametrize("yahoo, nasdaq", [
    ("AAPL", "AAPL"), ("BRK-B", "BRK.B"), ("BF-B", "BF.B"), ("^IXIC", "COMP"), ("^NDX", "NDX"),
    ("^GSPC", None), ("^DJI", None), ("^VIX", None), ("ES=F", None), ("EURUSD=X", None), ("GC=F", None),
    ("BTC-USD", None), ("SUI20947-USD", None), ("", None),
])
def test_nasdaq_symbol(yahoo, nasdaq):
    assert nasdaq_symbol(yahoo) == nasdaq


@pytest.mark.parametrize("nasdaq, yahoo", [
    ("BRK.B", "BRK-B"), ("BRK/B", "BRK-B"), ("brk.b", "BRK-B"), (" aapl ", "AAPL"), ("AAPL", "AAPL"),
    ("BF.B", "BF-B"),
    # Stocks, not indices: Nasdaq's index codes are only ever produced from Yahoo's (nasdaq_symbol).
    ("COMP", "COMP"), ("comp", "COMP"), ("NDX", "NDX"),
])
def test_from_nasdaq_symbol(nasdaq, yahoo):
    assert from_nasdaq_symbol(nasdaq) == yahoo


@pytest.mark.parametrize("symbol", ["AAPL", "BRK-B", "BF-B", "GOOGL", "COMP"])
def test_symbols_survive_the_round_trip(symbol):
    assert from_nasdaq_symbol(nasdaq_symbol(symbol)) == symbol


def test_the_stock_comp_survives_the_round_trip():
    assert nasdaq_symbol("COMP") == "COMP" and from_nasdaq_symbol(nasdaq_symbol("COMP")) == "COMP"


@pytest.mark.parametrize("index, code", [("^IXIC", "COMP"), ("^NDX", "NDX")])
def test_indices_map_only_from_yahoo_to_nasdaq(index, code):
    assert nasdaq_symbol(index) == code
    assert from_nasdaq_symbol(code) == code  # Nasdaq's code read back is the stock of that name, never the index
    assert from_nasdaq_symbol(nasdaq_symbol(index)) != index


@pytest.mark.parametrize("raw, clean", [
    ("Apple Inc. Common Stock", "Apple Inc."),
    ("Alphabet Inc. Class A Common Stock", "Alphabet Inc. Class A"),
    ("Meta Platforms, Inc. Class A Common Stock", "Meta Platforms, Inc. Class A"),
    ("NVIDIA Corporation Common Stock", "NVIDIA Corporation"),
    ("Spotify Technology S.A. Ordinary Shares", "Spotify Technology S.A."),
    ("Linde plc Ordinary Share", "Linde plc"),
    ("Futu Holdings Limited Class A Ordinary Shares", "Futu Holdings Limited"),
    ("Arm Holdings plc American Depositary Shares", "Arm Holdings plc"),
    ("Alibaba Group Holding Limited American Depositary Shares each representing eight Ordinary share",
     "Alibaba Group Holding Limited"),
    ("Baidu, Inc. ADS", "Baidu, Inc."),
    ("Example Corp - Common Shares", "Example Corp"),
    ("  Apple   Inc.   Common Stock  ", "Apple Inc."),
    ("SPDR S&P 500 ETF Trust", "SPDR S&P 500 ETF Trust"),
    ("Taiwan Semiconductor Manufacturing Company Ltd.", "Taiwan Semiconductor Manufacturing Company Ltd."),
    ("Apple Inc. Common Stock\n", "Apple Inc."), ("Apple Inc.\tCommon Stock", "Apple Inc."),
    ("Example Corp, Common Stock", "Example Corp"),
])
def test_clean_name(raw, clean):
    assert clean_name(raw) == clean


@pytest.mark.parametrize("raw, clean", [
    ("ADS-TEC Energy plc Ordinary Shares", "ADS-TEC Energy plc"),  # ADSE: the shipped directory named it ""
    ("Teads Holding Co. Common Stock", "Teads Holding Co."),  # TEAD: the shipped directory named it "Te"
    ("Crossroads Impact Corp. Common Stock", "Crossroads Impact Corp."),
    ("Threads Inc. Common Stock", "Threads Inc."),
    ("Roads ADS", "Roads"),  # a real trailing ADS after whitespace is still cut
    ("BeadsCommon Stock", "BeadsCommon Stock"),  # no whitespace before the description: nothing is cut
    ("ADS", "ADS"), ("Common Stock", "Common Stock"), ("Ordinary Shares", "Ordinary Shares"),
    ("  Common   Stock ", "Common Stock"),  # nothing left: keep what Nasdaq said (tidied)
    ("", ""), (None, ""),
])
def test_clean_name_keeps_words_that_merely_contain_ads(raw, clean):
    assert clean_name(raw) == clean


@pytest.mark.parametrize("text, expected", [
    ("2026-10-06T12:47:45.05-04:00", datetime(2026, 10, 6, 16, 47, 45, 50000, tzinfo=timezone.utc).timestamp()),
    ("2026-10-06T12:47:45.0512345-04:00",
     datetime(2026, 10, 6, 16, 47, 45, 51234, tzinfo=timezone.utc).timestamp()),
    ("2026-10-06T16:47:45.123456789Z", datetime(2026, 10, 6, 16, 47, 45, 123456, tzinfo=timezone.utc).timestamp()),
    ("2026-10-06T16:47:45+00:00", utc(2026, 10, 6, 16, 47, 45)),
    ("2026-01-06T12:00:00-05:00", utc(2026, 1, 6, 17)),
    ("2026-10-06T12:47:45", utc(2026, 10, 6, 16, 47, 45)),  # New York time, summer
    ("2026-01-06T12:00:00", utc(2026, 1, 6, 17)),  # New York time, winter
    ("2026-10-06T12:47:45.1234567", datetime(2026, 10, 6, 16, 47, 45, 123456, tzinfo=timezone.utc).timestamp()),
    ("2026-10-06 12:47:45", utc(2026, 10, 6, 16, 47, 45)),
    ("", 0.0), (None, 0.0), ("garbage", 0.0), ("2026-13-01T00:00:00", 0.0), ("10/06/2026", 0.0),
])
def test_ny_time(text, expected):
    assert _ny_time(text) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("text, expected", [
    ("10/06/2026", utc(2026, 10, 6) + OPEN_UTC), ("1/2/2026", utc(2026, 1, 2) + OPEN_UTC),
    (" 12/31/1999 ", utc(1999, 12, 31) + OPEN_UTC), ("02/29/2024", utc(2024, 2, 29) + OPEN_UTC),
    ("02/30/2026", None), ("2026-10-06", None), ("", None), ("N/A", None), (None, None), (20261006, None),
])
def test_day_t(text, expected):
    assert _day_t(text) == expected


def test_bars_from_rows_sorts_cleans_and_dedupes():
    d1, d2, d3 = utc(2026, 10, 1, 14, 30), utc(2026, 10, 2, 14, 30), utc(2026, 10, 5, 14, 30)
    rows = [
        (d3, 12.0, 11.0, 13.0, 12.5, None),  # high below close, low above: repaired
        (d2, None, None, None, 11.0, 5.0),  # only a close
        (None, 1.0, 1.0, 1.0, 1.0, 1.0),  # no time
        (d1, 10.0, 10.5, 9.5, None, 1.0),  # no close
        (d1, 10.0, 10.5, 9.5, 0.0, 1.0),  # zero close
        (d1, 10.0, 10.5, 9.5, -1.0, 1.0),  # negative close
        (d1, -2.0, 10.5, 9.5, 10.2, 7.0),  # bad open
        (d1 + 3600, 10.0, 10.6, 9.6, 10.3, 8.0),  # same day, later: replaces it for daily bars
    ]
    b = bars_from_rows("AAA", rows, "Test")
    assert list(b.t) == [d1 + 3600, d2, d3] and b.t.dtype == np.int64
    assert list(b.close) == [10.3, 11.0, 12.5] and list(b.open) == [10.0, 11.0, 12.0]
    assert list(b.high) == [10.6, 11.0, 12.5] and list(b.low) == [9.6, 11.0, 12.0]
    assert list(b.volume) == [8.0, 5.0, 0.0]
    assert b.meta == {"symbol": "AAA", "source": "Test"} and b.symbol == "AAA"
    intraday = bars_from_rows("AAA", rows, "Test", daily=False)
    assert list(intraday.t) == [d1, d1 + 3600, d2, d3]
    assert intraday.open[0] == 10.2  # a bad open becomes the close
    empty = bars_from_rows("AAA", [], "Test")
    assert len(empty) == 0 and empty.t.dtype == np.int64


# =====================================================================================================================
# Nasdaq
# =====================================================================================================================

WATCHLIST = "/quote/watchlist"


def nrow(symbol, price="$100.00", prev="$98.00", pct="+2.04%", status="Market Open", cls="STOCKS", name=None,
         volume="1,234,567", ts="2026-10-06T12:47:45.05-04:00"):
    return {"symbol": symbol, "companyName": name if name is not None else f"{symbol} Inc. Common Stock",
            "lastSalePrice": price, "previousClosePrice": prev, "percentageChange": pct, "netChange": "+2.00",
            "marketStatus": status, "assetClass": cls, "volume": volume, "lastTradeTimestampDateTime": ts}


def watchlist(known: dict):
    """known: (Nasdaq symbol, asset class) -> row. Answers only what it knows."""
    def answer(url):
        rows = []
        for key, value in parse_qsl(urlsplit(url).query):
            assert key == "symbol"
            sym, cls = value.split("|")
            assert sym == sym.lower()
            if (sym.upper(), cls) in known:
                rows.append(known[(sym.upper(), cls)])
        return J({"data": rows, "message": None, "status": {"rCode": 200}})
    return answer


def nasdaq_client(backend, etfs=(), retries=0):
    http = Http(backend=backend, retries=retries, sleep=Sleeper())
    return Nasdaq(http, etfs=lambda s: s in set(etfs)), http


def test_quote_from_row_during_the_session():
    row = nrow("AAPL", price="$227.50", prev="$225.00", pct="+9.99%", name="Apple Inc. Common Stock",
               volume="45,123,456")
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.symbol == "AAPL" and q.name == "Apple Inc." and q.price == 227.5 and q.prev_close == 225.0
    assert q.change_pct == pytest.approx((227.5 / 225 - 1) * 100)  # from the prices, not Nasdaq's rounded text
    assert q.change == pytest.approx(2.5) and q.volume == 45123456.0 and q.market_state == "REGULAR"
    assert q.time == pytest.approx(utc(2026, 10, 6, 16, 47, 45) + 0.05) and q.quote_type == "EQUITY"
    assert q.ext_price is None and q.ext_change_pct is None and q.source == "Nasdaq" and q.currency == "USD"


@pytest.mark.parametrize("status", ["Pre Market", "pre-market", " PRE MARKET "])
def test_quote_from_row_before_the_open_behaves_like_yahoo(status):
    q = Nasdaq.quote_from_row(nrow("AAPL", price="$230.00", prev="$225.00", status=status), "AAPL")
    assert q.market_state == "PRE" and q.price == 225.0 and q.change_pct == 0.0 and q.time == 0.0
    assert q.ext_price == 230.0 and q.ext_change_pct == pytest.approx((230 / 225 - 1) * 100)
    assert q.change == 0.0


@pytest.mark.parametrize("status, state", [("Closed", "CLOSED"), ("Market Closed", "CLOSED"),
                                           (" market closed ", "CLOSED"), ("Market Open", "REGULAR"), ("", ""),
                                           (None, ""), ("Halted", "")])
def test_quote_from_row_market_states(status, state):
    q = Nasdaq.quote_from_row(nrow("AAPL", status=status), "AAPL")
    assert q.market_state == state and q.price == 100.0 and q.prev_close == 98.0
    assert q.change_pct == pytest.approx((100 / 98 - 1) * 100)  # the regular close against the previous close
    assert q.ext_price is None and q.ext_change_pct is None
    assert q.time == pytest.approx(utc(2026, 10, 6, 16, 47, 45) + 0.05)


@pytest.mark.parametrize("status", ["After Hours", "after-hours", " AFTER HOURS "])
def test_quote_from_row_after_hours_splits_the_close_from_the_late_trade(status):
    """After hours Nasdaq's last sale is the late trade and netChange is its move from today's close."""
    row = dict(nrow("AAPL", price="$231.00", prev="$225.00", pct="+1.30%", status=status,
                    ts="2026-10-06T17:45:12.5-04:00"), netChange="+3.00")
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.market_state == "POST" and q.price == 228.0 and q.prev_close == 225.0
    assert q.change_pct == pytest.approx((228 / 225 - 1) * 100) and q.change == pytest.approx(3.0)
    assert q.ext_price == 231.0 and q.ext_change_pct == pytest.approx((231 / 228 - 1) * 100)
    assert q.time == ny(2026, 10, 6, 16) == utc(2026, 10, 6, 20)  # the regular close, on the trade's day
    assert q.source == "Nasdaq" and q.name == "AAPL Inc." and q.volume == 1234567.0


def test_quote_from_row_after_hours_with_the_late_trade_below_the_close():
    row = dict(nrow("AAPL", price="$95.50", prev="$97.00", status="After Hours",
                    ts="2026-01-06T19:59:00-05:00"), netChange="-2.50")
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.price == 98.0 and q.change_pct == pytest.approx((98 / 97 - 1) * 100)
    assert q.ext_price == 95.5 and q.ext_change_pct == pytest.approx((95.5 / 98 - 1) * 100)
    assert q.time == ny(2026, 1, 6, 16) == utc(2026, 1, 6, 21)  # winter: 16:00 EST


def test_quote_from_row_after_hours_without_a_late_move():
    row = dict(nrow("AAPL", price="$100.00", prev="$98.00", status="After Hours"), netChange="0.00")
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.price == 100.0 and q.ext_price == 100.0 and q.ext_change_pct == 0.0
    assert q.change_pct == pytest.approx((100 / 98 - 1) * 100) and q.time == utc(2026, 10, 6, 20)


def test_quote_from_row_after_hours_without_a_trade_time_uses_today_in_new_york(clock):
    clock.wall = utc(2026, 10, 7, 2, 30)  # 22:30 in New York on the 6th
    row = dict(nrow("AAPL", status="After Hours", ts=None), netChange="+1.00")
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.price == 99.0 and q.time == utc(2026, 10, 6, 20)


def test_quote_from_row_after_hours_without_a_previous_close():
    row = dict(nrow("NEW", price="$12.00", prev="N/A", pct="+4.00%", status="After Hours"), netChange="+2.00")
    q = Nasdaq.quote_from_row(row, "NEW")
    assert q.price == 10.0 and q.prev_close is None and q.change_pct is None and q.change is None
    assert q.ext_price == 12.0 and q.ext_change_pct == pytest.approx(20.0)


@pytest.mark.parametrize("net", [None, "N/A", "--", "", "UNCH"])
def test_quote_from_row_after_hours_without_net_change_behaves_as_before(net):
    row = dict(nrow("AAPL", status="After Hours"), netChange=net)
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.market_state == "POST" and q.price == 100.0 and q.prev_close == 98.0
    assert q.change_pct == pytest.approx((100 / 98 - 1) * 100)
    assert q.ext_price is None and q.ext_change_pct is None
    assert q.time == pytest.approx(utc(2026, 10, 6, 16, 47, 45) + 0.05)  # the trade's own time


@pytest.mark.parametrize("net", ["+100.00", "+250.00"])
def test_quote_from_row_after_hours_with_a_change_that_leaves_no_close(net):
    q = Nasdaq.quote_from_row(dict(nrow("AAPL", status="After Hours"), netChange=net), "AAPL")
    assert q.price == 100.0 and q.ext_price is None and q.change_pct == pytest.approx((100 / 98 - 1) * 100)


def test_quote_from_row_before_the_open_ignores_net_change():
    row = dict(nrow("AAPL", price="$230.00", prev="$225.00", status="Pre Market"), netChange="+5.00")
    q = Nasdaq.quote_from_row(row, "AAPL")
    assert q.price == 225.0 and q.change_pct == 0.0 and q.time == 0.0
    assert q.ext_price == 230.0 and q.ext_change_pct == pytest.approx((230 / 225 - 1) * 100)


def test_quote_from_row_while_open_and_closed_ignores_net_change():
    for status in ("Market Open", "Market Closed"):
        q = Nasdaq.quote_from_row(dict(nrow("AAPL", status=status), netChange="+7.00"), "AAPL")
        assert q.price == 100.0 and q.ext_price is None and q.change_pct == pytest.approx((100 / 98 - 1) * 100)


@pytest.mark.parametrize("price", ["N/A", "--", "", None, "$0.00", "-5", "abc"])
def test_quote_from_row_without_a_price_is_none(price):
    assert Nasdaq.quote_from_row(nrow("AAPL", price=price), "AAPL") is None


def test_quote_from_row_with_a_zero_or_missing_previous_close():
    q = Nasdaq.quote_from_row(nrow("NEW", prev="$0.00", pct="+3.5%"), "NEW")
    assert q.price == 100.0 and q.prev_close == 0.0 and q.change_pct == 3.5 and q.change is None
    q = Nasdaq.quote_from_row(nrow("NEW", prev="N/A", pct="-1.25%"), "NEW")
    assert q.prev_close is None and q.change_pct == -1.25
    q = Nasdaq.quote_from_row(nrow("NEW", prev="N/A", pct="N/A"), "NEW")
    assert q.change_pct is None
    # Before the open without a previous close there is nothing to move to "pre-market".
    q = Nasdaq.quote_from_row(nrow("NEW", prev="$0.00", pct="+3.5%", status="Pre Market"), "NEW")
    assert q.price == 100.0 and q.ext_price is None and q.market_state == "PRE"


def test_quote_from_row_kinds_names_and_odd_fields():
    assert Nasdaq.quote_from_row(nrow("SPY", cls="ETF"), "SPY").quote_type == "ETF"
    assert Nasdaq.quote_from_row(nrow("COMP", cls="INDEX"), "^IXIC").quote_type == "INDEX"
    assert Nasdaq.quote_from_row(nrow("X", cls=None), "X").quote_type == "EQUITY"
    q = Nasdaq.quote_from_row({"lastSalePrice": "$5"}, "ZZ")
    assert q.name == "ZZ" and q.prev_close is None and q.change_pct is None and q.volume is None
    assert q.time == 0.0 and q.market_state == ""
    q = Nasdaq.quote_from_row(nrow("X", volume="--", ts="not a time", name=""), "X")
    assert q.volume is None and q.time == 0.0 and q.name == "X"


def test_supports_and_asset_class():
    n, _ = nasdaq_client(Backend(), etfs=("SPY",))
    assert n.supports("AAPL") and n.supports("BRK-B") and n.supports("^IXIC") and n.supports("^NDX")
    assert not n.supports("^GSPC") and not n.supports("BTC-USD") and not n.supports("ES=F") and not n.supports("")
    assert n.asset_class("^IXIC") == "index" and n.asset_class("SPY") == "etf" and n.asset_class("AAPL") == "stocks"
    assert Nasdaq(Http(backend=Backend())).asset_class("SPY") == "stocks"  # no directory: everything is a stock


def test_quotes_go_in_batches_of_20():
    symbols = [f"T{i:02d}" for i in range(25)]
    backend = Backend().on(WATCHLIST, watchlist({(s, "stocks"): nrow(s, price=f"${10 + i}") for i, s in
                                                 enumerate(symbols)}))
    n, _ = nasdaq_client(backend)
    out = asyncio.run(n.quotes(symbols))
    assert sorted(out) == symbols and out["T24"].price == 34.0 and out["T00"].source == "Nasdaq"
    assert [len(c.pairs) for c in backend.calls] == [20, 5]
    assert backend.calls[0].pairs[:2] == [("symbol", "t00|stocks"), ("symbol", "t01|stocks")]
    for c in backend.calls:
        assert c.url.startswith(f"{NASDAQ}{WATCHLIST}?") and c.headers == NASDAQ_HEADERS


def test_quotes_retry_the_other_asset_class_and_remember_unknown_symbols(clock):
    known = {("AAPL", "stocks"): nrow("AAPL"), ("SPY", "etf"): nrow("SPY", cls="ETF"),
             ("VOO", "stocks"): nrow("VOO", price="$500"),  # the directory says ETF; Nasdaq disagrees
             ("ARKK", "etf"): nrow("ARKK", price="$50", cls="ETF"),  # not in the directory's ETFs
             ("BRK.B", "stocks"): nrow("BRK.B", price="$450"), ("COMP", "index"): nrow("COMP", price="18,000.12",
                                                                                     cls="INDEX")}
    backend = Backend().on(WATCHLIST, watchlist(known))
    n, _ = nasdaq_client(backend, etfs=("SPY", "VOO"))
    symbols = ["AAPL", "SPY", "VOO", "ARKK", "BRK-B", "^IXIC", "ZZZZ", "BTC-USD", "^GSPC", "AAPL"]

    async def main():
        out = await n.quotes(symbols)
        assert sorted(out) == sorted(["AAPL", "SPY", "VOO", "ARKK", "BRK-B", "^IXIC"])
        assert out["BRK-B"].price == 450.0 and out["^IXIC"].price == 18000.12 and out["^IXIC"].quote_type == "INDEX"
        assert out["VOO"].price == 500.0 and out["ARKK"].quote_type == "ETF"
        first, retry = backend.calls
        assert first.pairs == [("symbol", v) for v in ("aapl|stocks", "spy|etf", "voo|etf", "arkk|stocks",
                                                       "brk.b|stocks", "comp|index", "zzzz|stocks")]
        assert retry.pairs == [("symbol", v) for v in ("voo|stocks", "arkk|etf", "zzzz|etf")]
        assert not n.supports("ZZZZ") and n.supports("AAPL") and n.supports("VOO")
        assert await n.quotes(["ZZZZ"]) == {} and len(backend.calls) == 2  # not asked again
        clock.advance(UNSUPPORTED_TTL - 1)
        assert not n.supports("ZZZZ")
        clock.advance(2)
        assert n.supports("ZZZZ")

    asyncio.run(main())


def test_a_missing_index_is_not_retried_as_another_class():
    backend = Backend().on(WATCHLIST, watchlist({}))
    n, _ = nasdaq_client(backend)
    assert asyncio.run(n.quotes(["^IXIC", "^NDX"])) == {}
    assert len(backend.calls) == 1 and backend.calls[0].pairs == [("symbol", "comp|index"), ("symbol", "ndx|index")]
    assert not n.supports("^IXIC")


def test_quotes_ignore_rows_they_did_not_ask_for():
    def answer(url):
        return J({"data": [nrow("aapl", price="$1"), nrow("MSFT", price="$2"), {"symbol": None}, {},
                           nrow("TSLA", price="N/A")]})

    backend = Backend().on(WATCHLIST, answer)
    n, _ = nasdaq_client(backend)
    out = asyncio.run(n.quotes(["AAPL", "TSLA"]))
    assert list(out) == ["AAPL"] and out["AAPL"].price == 1.0


@pytest.mark.parametrize("body", ["", "{}", '{"data": null, "status": {"rCode": 400}}', '{"data": []}'])
def test_quotes_with_empty_answers(body):
    backend = Backend().on(WATCHLIST, Response(200, body))
    n, _ = nasdaq_client(backend)
    assert asyncio.run(n.quotes(["AAPL"])) == {}
    assert len(backend.calls) == 2  # stocks, then etf


def test_quotes_with_nothing_nasdaq_carries_make_no_request():
    backend = Backend()
    n, _ = nasdaq_client(backend)
    assert asyncio.run(n.quotes(["BTC-USD", "^GSPC", "ES=F", ""])) == {}
    assert asyncio.run(n.quotes([])) == {}
    assert not backend.calls


@pytest.mark.parametrize("answer, error, failing", [
    (Response(500, ""), "HTTP 500", True), (Response(403, "denied"), "Nasdaq: HTTP 403", True),
    (Response(429, ""), "HTTP 429", True), (ConnectionError("down"), "ConnectionError: down", True),
    (Response(200, "<html>maintenance</html>"), "not JSON (HTTP 200): '<html>maintenance</html>'", False),
    (Response(404, "{}"), "Nasdaq: HTTP 404", False),
])
def test_quote_failures_are_skipped_and_do_not_mark_symbols_unknown(caplog, answer, error, failing):
    backend = Backend().on(WATCHLIST, answer)
    n, http = nasdaq_client(backend)
    with caplog.at_level("INFO", logger="marketbot.backup"):
        assert asyncio.run(n.quotes(["AAPL", "MSFT"])) == {}  # logged, not raised
    assert f"Nasdaq quotes failed for 2 symbols: {error}" in caplog.messages
    assert n.supports("AAPL") and n.supports("MSFT")  # nothing was learned about them
    assert len(backend.calls) == 1  # a failed batch isn't retried as the other asset class either
    assert http.health["Nasdaq"].failing == failing


def test_a_failing_batch_is_skipped_and_the_others_kept():
    symbols = [f"T{i:02d}" for i in range(25)]
    good = watchlist({(s, "stocks"): nrow(s, price=f"${10 + i}") for i, s in enumerate(symbols) if s != "T24"})

    def answer(url):
        asked = [v for _, v in parse_qsl(urlsplit(url).query)]
        return Response(503, "busy") if "t00|stocks" in asked else good(url)

    backend = Backend().on(WATCHLIST, answer)
    n, _ = nasdaq_client(backend)
    out = asyncio.run(n.quotes(symbols))
    assert sorted(out) == symbols[20:24] and out["T23"].price == 33.0
    assert [c.pairs for c in backend.calls] == [
        [("symbol", f"{s.lower()}|stocks") for s in symbols[:20]],
        [("symbol", f"{s.lower()}|stocks") for s in symbols[20:]],
        [("symbol", "t24|etf")],  # only what the answering batch didn't know is retried
    ]
    assert all(n.supports(s) for s in symbols[:20])  # the failed batch: nothing learned
    assert not n.supports("T24")  # asked as both classes and unknown to both


def test_a_failing_retry_pass_keeps_the_quotes_already_found():
    good = watchlist({("AAPL", "stocks"): nrow("AAPL")})

    def answer(url):
        asked = [v for _, v in parse_qsl(urlsplit(url).query)]
        return Response(429, "Too Many Requests") if "zzzz|etf" in asked else good(url)

    backend = Backend().on(WATCHLIST, answer)
    n, _ = nasdaq_client(backend)
    out = asyncio.run(n.quotes(["AAPL", "ZZZZ"]))
    assert list(out) == ["AAPL"] and out["AAPL"].price == 100.0
    assert n.supports("ZZZZ")  # the retry failed: nothing was learned about it
    assert [c.pairs for c in backend.calls] == [[("symbol", "aapl|stocks"), ("symbol", "zzzz|stocks")],
                                                [("symbol", "zzzz|etf")]]


def test_a_resting_nasdaq_answers_nothing_and_learns_nothing(clock):
    backend = Backend().on(WATCHLIST, watchlist({("AAPL", "stocks"): nrow("AAPL")}))
    n, http = nasdaq_client(backend)
    for _ in range(FAILS_TO_REST):
        http.record_failure("Nasdaq", HttpError("Nasdaq", "HTTP 403", 403))

    async def main():
        assert await n.quotes(["AAPL", "ZZZZ"]) == {}
        assert not backend.calls and n.supports("AAPL") and n.supports("ZZZZ")
        clock.advance(REST_SECONDS[0] + 1)
        out = await n.quotes(["AAPL", "ZZZZ"])
        assert list(out) == ["AAPL"] and not n.supports("ZZZZ")  # answering again: ZZZZ really is unknown

    asyncio.run(main())


HISTORY_ROWS = [
    {"date": "10/06/2026", "close": "$227.50", "volume": "45,123,456", "open": "$225.00", "high": "$228.00",
     "low": "$224.50"},
    {"date": "10/05/2026", "close": "$225.00", "volume": "--", "open": "N/A", "high": "$224.00", "low": "$226.00"},
    {"date": "N/A", "close": "$1.00", "volume": "1", "open": "$1", "high": "$1", "low": "$1"},
    {"date": "10/02/2026", "close": "--", "volume": "1", "open": "$1", "high": "$1", "low": "$1"},
    {"date": "10/01/2026", "close": "$0.00", "volume": "1", "open": "$1", "high": "$1", "low": "$1"},
    {},
    {"date": "09/30/2026", "close": "$220.00", "volume": "30,000,000", "open": "$219.00", "high": "$221.00",
     "low": "$218.00"},
]


def history(by_class: dict):
    def answer(url):
        rows = by_class.get(query(url)["assetclass"])
        if rows is None:
            return J({"data": None, "message": None, "status": {"rCode": 400}})
        return J({"data": {"symbol": "X", "totalRecords": len(rows), "tradesTable": {"rows": rows}}})
    return answer


def test_daily_parses_newest_first_rows_and_skips_bad_ones():
    backend = Backend().on("/historical", history({"stocks": HISTORY_ROWS}))
    n, _ = nasdaq_client(backend)
    b = asyncio.run(n.daily("AAPL"))
    assert list(b.t) == [_day_t("09/30/2026"), _day_t("10/05/2026"), _day_t("10/06/2026")]
    assert list(b.close) == [220.0, 225.0, 227.5] and list(b.open) == [219.0, 225.0, 225.0]
    assert list(b.high) == [221.0, 225.0, 228.0] and list(b.low) == [218.0, 225.0, 224.5]
    assert list(b.volume) == [30_000_000.0, 0.0, 45_123_456.0]
    assert b.symbol == "AAPL" and b.meta["source"] == "Nasdaq"
    call = backend.calls[0]
    assert call.path == "/api/quote/AAPL/historical" and call.headers == NASDAQ_HEADERS
    assert call.query["assetclass"] == "stocks" and call.query["limit"] == "9999" and len(backend.calls) == 1


def test_daily_index_rows_without_dollars_or_volume():
    rows = [{"date": "10/06/2026", "close": "18,137.85", "volume": "--", "open": "18,099.00", "high": "18,200.10",
             "low": "18,050.00"},
            {"date": "10/05/2026", "close": "18,000.00", "volume": "--", "open": "--", "high": "--", "low": "--"}]
    backend = Backend().on("/historical", history({"index": rows}))
    n, _ = nasdaq_client(backend)
    b = asyncio.run(n.daily("^IXIC"))
    assert list(b.close) == [18000.0, 18137.85] and list(b.volume) == [0.0, 0.0] and b.symbol == "^IXIC"
    assert list(b.high) == [18000.0, 18200.10] and list(b.open) == [18000.0, 18099.0]
    assert backend.calls[0].path == "/api/quote/COMP/historical" and backend.calls[0].query["assetclass"] == "index"


def test_daily_tries_the_other_asset_class():
    backend = Backend().on("/historical", history({"stocks": [], "etf": HISTORY_ROWS}))
    n, _ = nasdaq_client(backend)
    assert len(asyncio.run(n.daily("ARKK"))) == 3
    assert [c.query["assetclass"] for c in backend.calls] == ["stocks", "etf"]

    backend = Backend().on("/historical", history({"stocks": HISTORY_ROWS}))  # etf: data null
    n, _ = nasdaq_client(backend, etfs=("VOO",))
    assert len(asyncio.run(n.daily("VOO"))) == 3
    assert [c.query["assetclass"] for c in backend.calls] == ["etf", "stocks"]


def test_daily_without_history_is_a_404():
    backend = Backend().on("/historical", history({"stocks": []}))
    n, _ = nasdaq_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(n.daily("ZZZZ"))
    assert e.value.status == 404 and len(backend.calls) == 2

    backend = Backend().on("/historical", history({}))
    n, _ = nasdaq_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(n.daily("^NDX"))
    assert e.value.status == 404 and len(backend.calls) == 1  # an index has no other class to try
    assert backend.calls[0].path == "/api/quote/NDX/historical"


def test_daily_with_only_bad_rows_is_empty():
    backend = Backend().on("/historical", history({"stocks": HISTORY_ROWS[2:6]}))
    n, _ = nasdaq_client(backend)
    assert len(asyncio.run(n.daily("AAPL"))) == 0


def test_daily_asks_from_the_start_but_no_more_than_ten_years():
    backend = Backend().on("/historical", history({"stocks": HISTORY_ROWS}))
    n, _ = nasdaq_client(backend)

    def ten_years_ago():
        return (datetime.now(timezone.utc) - timedelta(days=3653)).strftime("%Y-%m-%d")

    async def main():
        await n.daily("AAPL", start=utc(2025, 1, 2, 23))
        before = ten_years_ago()
        await n.daily("AAPL")
        await n.daily("AAPL", start=utc(1990, 1, 1))
        await n.daily("AAPL", start=0)
        return before, ten_years_ago()

    before, after = asyncio.run(main())
    dates = [c.query["fromdate"] for c in backend.calls]
    assert dates[0] == "2025-01-02"
    assert all(d in (before, after) for d in dates[1:])


def test_daily_and_intraday_refuse_what_nasdaq_does_not_carry():
    backend = Backend()
    n, _ = nasdaq_client(backend)

    async def main():
        for call in (n.daily("BTC-USD"), n.daily("^GSPC"), n.intraday("ES=F")):
            with pytest.raises(HttpError):
                await call

    asyncio.run(main())
    assert not backend.calls


def test_daily_failure_status_is_kept():
    backend = Backend().on("/historical", Response(503, ""))
    n, http = nasdaq_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(n.daily("AAPL"))
    assert e.value.status == 503 and http.health["Nasdaq"].failing


def chart_point(wall: datetime, y):
    """Nasdaq's x: New York wall-clock time written as if it were UTC, in milliseconds."""
    return {"x": int(wall.replace(tzinfo=timezone.utc).timestamp() * 1000), "y": y, "z": {"value": str(y)}}


@pytest.mark.parametrize("day, offset_hours", [
    (datetime(2026, 10, 6), 4),  # EDT
    (datetime(2026, 12, 1), 5),  # EST
    (datetime(2026, 3, 6), 5),  # the Friday before clocks go forward
    (datetime(2026, 3, 9), 4),  # the Monday after
    (datetime(2026, 11, 2), 5),  # the Monday after clocks go back
])
def test_intraday_wall_clock_times_become_real_times(day, offset_hours):
    points = [chart_point(day.replace(hour=4), 99.0), chart_point(day.replace(hour=9, minute=30), "$100.50"),
              chart_point(day.replace(hour=15, minute=59), 101.25)]
    backend = Backend().on("/chart", J({"data": {"symbol": "AAPL", "previousClose": "$99.50", "chart": points}}))
    n, _ = nasdaq_client(backend)
    bars, prev = asyncio.run(n.intraday("AAPL"))
    base = int(day.replace(tzinfo=timezone.utc).timestamp())
    assert list(bars.t) == [base + (4 + offset_hours) * 3600, base + (9 + offset_hours) * 3600 + 1800,
                            base + (15 + offset_hours) * 3600 + 59 * 60]
    assert bars.t[1] == ny(day.year, day.month, day.day, 9, 30)
    assert list(bars.close) == [99.0, 100.5, 101.25]
    assert np.array_equal(bars.open, bars.close) and np.array_equal(bars.high, bars.close)
    assert np.array_equal(bars.low, bars.close) and not bars.volume.any()
    assert prev == 99.5 and bars.meta["source"] == "Nasdaq"
    assert backend.calls[0].path == "/api/quote/AAPL/chart" and backend.calls[0].query == {"assetclass": "stocks"}


def test_intraday_skips_bad_points_and_dedupes():
    day = datetime(2026, 10, 6)
    good = chart_point(day.replace(hour=10), 5.0)
    again = dict(good, y=6.0)
    points = [{"x": None, "y": 1.0}, dict(good, y=None), dict(good, y="N/A"), {"y": 2.0}, good, again]
    backend = Backend().on("/chart", J({"data": {"chart": points}}))
    n, _ = nasdaq_client(backend, etfs=("QQQ",))
    bars, prev = asyncio.run(n.intraday("QQQ"))
    assert list(bars.close) == [6.0] and prev is None
    assert backend.calls[0].query == {"assetclass": "etf"}


@pytest.mark.parametrize("body", ["", "{}", '{"data": null}', '{"data": {"chart": null}}'])
def test_intraday_with_empty_answers(body):
    backend = Backend().on("/chart", Response(200, body))
    n, _ = nasdaq_client(backend)
    bars, prev = asyncio.run(n.intraday("^NDX"))
    assert len(bars) == 0 and prev is None
    assert backend.calls[0].path == "/api/quote/NDX/chart" and backend.calls[0].query == {"assetclass": "index"}


def test_screener_and_nasdaq100():
    rows = [{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "lastsale": "$227.50"}, {"symbol": "BRK/B"}]
    ndx = [{"symbol": "AAPL"}, {"symbol": "BRK.B"}, {"symbol": ""}, {"name": "no symbol"}, {"symbol": "googl"}]
    backend = (Backend().on("/screener/stocks", J({"data": {"rows": rows}}))
               .on("/list-type/nasdaq100", J({"data": {"data": {"rows": ndx}}})))
    n, _ = nasdaq_client(backend)

    async def main():
        assert await n.screener() == rows
        assert await n.nasdaq100() == ["AAPL", "BRK-B", "GOOGL"]

    asyncio.run(main())
    s = backend.calls_to("/screener/stocks")[0]
    assert s.query == {"tableonly": "true", "download": "true"} and s.headers == NASDAQ_HEADERS
    assert backend.calls_to("nasdaq100")[0].path == "/api/quote/list-type/nasdaq100"


@pytest.mark.parametrize("body", ["", "{}", '{"data": null}', '{"data": {"rows": null}}'])
def test_screener_and_nasdaq100_with_empty_answers(body):
    backend = Backend().on("/screener/", Response(200, body)).on("/list-type/", Response(200, body))
    n, _ = nasdaq_client(backend)

    async def main():
        assert await n.screener() == []
        assert await n.nasdaq100() == []

    asyncio.run(main())


@pytest.mark.parametrize("status", [404, 400, 401, 403, 500])
def test_screener_errors_raise_with_the_status(status):
    backend = Backend().on("/screener/", Response(status, ""))
    n, http = nasdaq_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(n.screener())
    assert e.value.status == status
    assert http.health["Nasdaq"].failing == (status in (401, 403, 500))


# =====================================================================================================================
# Coinbase
# =====================================================================================================================

@pytest.mark.parametrize("symbol, product", [
    ("BTC-USD", "BTC-USD"), ("ETH-USD", "ETH-USD"), ("SUI20947-USD", "SUI-USD"), ("HYPE32196-USD", "HYPE-USD"),
    ("BFC7817-USD", "BFC-USD"), ("1INCH-USD", "1INCH-USD"), ("TON11419-USD", "TON-USD"),
    ("UNI7083-USD", "UNI-USD"), ("PEPE24478-USD", "PEPE-USD"),
    ("AAPL", None), ("BTC-EUR", None), ("^BTC-USD", None), ("-USD", None), ("BTCUSD", None),
    # An all-digit base is a bare CoinMarketCap id, not a ticker Coinbase could list.
    ("12345-USD", None), ("38590-USD", None), ("1-USD", None),
])
def test_coinbase_product(symbol, product):
    assert coinbase_product(symbol) == product


@pytest.mark.parametrize("symbol", ["API3-USD", "C98-USD", "X2Y2-USD", "ABC123-USD", "00X-USD"])
def test_coinbase_product_keeps_tickers_that_end_in_digits(symbol):
    assert coinbase_product(symbol) == symbol  # fewer than four digits after a letter belong to the ticker


def coinbase_client(backend, retries=0):
    http = Http(backend=backend, retries=retries, sleep=Sleeper())
    return Coinbase(http), http


NOW = 1_791_300_000  # the clock fixture's wall time: 2026-10-06 15:20 UTC
MIDNIGHT = utc(2026, 10, 6)
# Coinbase's candles: [time, low, high, open, close, base volume], newest first.
TODAY = [MIDNIGHT, 59000, 62000.5, 60000.0, 61000, 1234.5]
YESTERDAY = [MIDNIGHT - DAY, 58000, 60500, 59500, 60000, 2000.0]


def day_candles(*rows, status=200):
    return Response(status, json.dumps([list(r) for r in rows]))


def test_todays_utc_candle_becomes_the_quote(clock):
    assert (NOW, MIDNIGHT) == (clock.wall, NOW - NOW % DAY)
    backend = Backend().on("/candles", day_candles(TODAY, YESTERDAY))
    cb, http = coinbase_client(backend)
    q = asyncio.run(cb.quote("BTC-USD"))
    assert (q.symbol, q.name, q.price, q.prev_close) == ("BTC-USD", "BTC", 61000.0, 60000.0)
    assert q.change_pct == pytest.approx((61000 / 60000 - 1) * 100) and q.change == 1000.0  # since midnight UTC
    assert q.day_high == 62000.5 and q.day_low == 59000.0 and q.volume == pytest.approx(1234.5 * 61000)  # dollars
    assert q.time == clock.wall and q.quote_type == "CRYPTOCURRENCY" and q.source == "Coinbase"
    assert q.extra == {} and q.market_state == "" and q.currency == "USD"  # no 24-hour window any more
    assert all(type(v) is float for v in (q.price, q.prev_close, q.day_high, q.day_low, q.volume))
    call, = backend.calls
    assert call.url == (f"{COINBASE}/products/BTC-USD/candles?granularity=86400"
                        "&start=2026-10-06T00%3A00%3A00%2B00%3A00&end=2026-10-06T15%3A20%3A00%2B00%3A00")
    assert call.query == {"granularity": "86400", "start": "2026-10-06T00:00:00+00:00",
                          "end": "2026-10-06T15:20:00+00:00"}
    assert not http.health["Coinbase"].failing and http.health["Coinbase"].ok == 1


def test_the_candle_for_today_is_found_in_any_order(clock):
    later = [MIDNIGHT + DAY, 1, 1, 1, 1, 1]  # a stray row from the future isn't today's either
    cb, _ = coinbase_client(Backend().on("/candles", day_candles(YESTERDAY, later, TODAY)))
    q = asyncio.run(cb.quote("ETH-USD"))
    assert q.price == 61000.0 and q.prev_close == 60000.0 and q.name == "ETH"


def test_the_utc_day_starts_at_midnight(clock):
    clock.wall = MIDNIGHT + DAY - 1  # 23:59:59: still the same UTC day
    backend = Backend().on("/candles", day_candles(TODAY))
    cb, _ = coinbase_client(backend)
    assert asyncio.run(cb.quote("BTC-USD")).price == 61000.0
    assert backend.calls[0].query["start"] == "2026-10-06T00:00:00+00:00"
    assert backend.calls[0].query["end"] == "2026-10-06T23:59:59+00:00"

    clock.wall = MIDNIGHT + DAY + 1  # 00:00:01 the next day: yesterday's candle is no longer today's
    backend = Backend().on("/candles", day_candles(TODAY))
    cb, _ = coinbase_client(backend)
    assert asyncio.run(cb.quote("BTC-USD")) is None
    assert backend.calls[0].query == {"granularity": "86400", "start": "2026-10-07T00:00:00+00:00",
                                      "end": "2026-10-07T00:00:01+00:00"}

    tomorrow = [MIDNIGHT + DAY, 61000, 61500, 61000, 61400, 3.0]
    cb, _ = coinbase_client(Backend().on("/candles", day_candles(tomorrow, TODAY)))
    q = asyncio.run(cb.quote("BTC-USD"))
    assert q.price == 61400.0 and q.prev_close == 61000.0 and q.time == MIDNIGHT + DAY + 1


@pytest.mark.parametrize("answer", [day_candles(YESTERDAY), day_candles(), Response(200, ""),
                                    Response(200, "null"), J({"message": "no candles"}),
                                    day_candles(TODAY[:5], [MIDNIGHT]), J([{"time": MIDNIGHT, "close": 1.0}])])
def test_no_candle_for_today_is_no_quote(clock, answer):
    backend = Backend().on("/candles", answer)
    cb, http = coinbase_client(backend)
    assert asyncio.run(cb.quote("BTC-USD")) is None  # e.g. just after midnight, before the day's first trade
    assert len(backend.calls) == 1 and cb.supports("BTC-USD") and not http.health["Coinbase"].failing


def test_short_and_odd_rows_are_skipped(clock):
    rows = [TODAY[:5], {"time": MIDNIGHT}, "x", None, TODAY]
    cb, _ = coinbase_client(Backend().on("/candles", J(rows)))
    assert asyncio.run(cb.quote("BTC-USD")).price == 61000.0


def test_yahoo_style_ids_map_to_the_coinbase_product(clock):
    backend = Backend().on("/candles", day_candles(TODAY))
    cb, _ = coinbase_client(backend)
    q = asyncio.run(cb.quote("SUI20947-USD"))
    assert q.symbol == "SUI20947-USD" and q.name == "SUI" and q.price == 61000.0
    assert backend.calls[0].path == "/products/SUI-USD/candles"

    backend = Backend().on("/candles", day_candles(TODAY))
    cb, _ = coinbase_client(backend)
    q = asyncio.run(cb.quote("API3-USD"))
    assert q.symbol == "API3-USD" and q.name == "API3" and backend.calls[0].path == "/products/API3-USD/candles"


@pytest.mark.parametrize("candle, price, prev, pct", [
    ([MIDNIGHT, 59000, 62000.5, 60000, None, 1.0], None, None, None),
    ([MIDNIGHT, 59000, 62000.5, 60000, 0, 1.0], None, None, None),
    ([MIDNIGHT, 59000, 62000.5, 60000, -5, 1.0], None, None, None),
    ([MIDNIGHT, 59000, 62000.5, 60000, "abc", 1.0], None, None, None),
    ([MIDNIGHT, 59000, 62000.5, 0, 61000, 1.0], 61000.0, 0.0, None),
    ([MIDNIGHT, 59000, 62000.5, None, 61000, 1.0], 61000.0, None, None),
    ([MIDNIGHT, "59000", "62000.5", "60000", "61000", "2"], 61000.0, 60000.0, (61000 / 60000 - 1) * 100),
])
def test_candles_with_missing_numbers(clock, candle, price, prev, pct):
    cb, _ = coinbase_client(Backend().on("/candles", day_candles(candle)))
    q = asyncio.run(cb.quote("BTC-USD"))
    if price is None:
        assert q is None
    else:
        assert q.price == price and q.prev_close == prev
        if pct is None:
            assert q.change_pct is None and q.change is None
        else:
            assert q.change_pct == pytest.approx(pct) and q.change == pytest.approx(price - prev)


@pytest.mark.parametrize("volume", [None, 0, "--"])
def test_a_candle_without_volume_has_zero_volume(clock, volume):
    candle = TODAY[:5] + [volume]
    cb, _ = coinbase_client(Backend().on("/candles", day_candles(candle)))
    q = asyncio.run(cb.quote("BTC-USD"))
    assert q.volume == 0.0 and q.price == 61000.0


def test_a_candle_without_high_or_low(clock):
    cb, _ = coinbase_client(Backend().on("/candles", day_candles([MIDNIGHT, None, None, 60000, 61000, 1.0])))
    q = asyncio.run(cb.quote("BTC-USD"))
    assert q.day_high is None and q.day_low is None and q.price == 61000.0


def test_quote_of_something_coinbase_cannot_list_asks_nothing():
    backend = Backend()
    cb, _ = coinbase_client(backend)
    for symbol in ("AAPL", "^GSPC", "BTC-EUR", "12345-USD"):
        assert asyncio.run(cb.quote(symbol)) is None and not cb.supports(symbol)
    assert not backend.calls and cb.supports("BTC-USD")


@pytest.mark.parametrize("status", [400, 404])
def test_unknown_products_are_remembered(clock, status):
    backend = Backend().on("/candles", Response(status, '{"message": "NotFound"}'))
    cb, http = coinbase_client(backend)

    async def main():
        with pytest.raises(HttpError) as e:
            await cb.quote("SUI20947-USD")
        assert e.value.status == 404 and str(e.value) == "Coinbase doesn't list SUI-USD"
        assert e.value.source == "Coinbase"
        assert not cb.supports("SUI20947-USD") and not cb.supports("SUI-USD") and cb.supports("BTC-USD")
        assert await cb.quotes(["SUI-USD", "SUI20947-USD"]) == {}
        assert len(backend.calls) == 1
        clock.advance(UNSUPPORTED_TTL - 1)
        assert not cb.supports("SUI-USD")
        clock.advance(2)
        assert cb.supports("SUI-USD")

    asyncio.run(main())
    assert not http.health["Coinbase"].failing  # it answered


def _by_product(answers: dict):
    def answer(url):
        return answers[urlsplit(url).path.split("/")[2]]
    return answer


def test_quotes_keep_what_answered(clock):
    backend = Backend().on("/candles", _by_product({
        "BTC-USD": day_candles(TODAY, YESTERDAY), "ETH-USD": Response(500, "oops"), "NOPE-USD": Response(404, ""),
        "DOGE-USD": Response(429, ""), "XRP-USD": Response(200, "not json"), "SOL-USD": day_candles(YESTERDAY),
        "ADA-USD": Response(403, "forbidden")}))
    cb, _ = coinbase_client(backend)
    asked = ["BTC-USD", "ETH-USD", "NOPE-USD", "DOGE-USD", "XRP-USD", "SOL-USD", "ADA-USD", "AAPL", "BTC-USD"]
    out = asyncio.run(cb.quotes(asked))
    assert list(out) == ["BTC-USD"] and out["BTC-USD"].price == 61000.0
    assert sorted(c.path for c in backend.calls) == sorted(
        f"/products/{p}/candles" for p in ("BTC-USD", "ETH-USD", "NOPE-USD", "DOGE-USD", "XRP-USD", "SOL-USD",
                                           "ADA-USD"))
    assert not cb.supports("NOPE-USD")
    for p in ("ETH-USD", "DOGE-USD", "XRP-USD", "SOL-USD", "ADA-USD"):
        assert cb.supports(p)  # failures and a quiet day aren't "unknown"


def test_quotes_of_yahoo_ids_keep_the_yahoo_symbol(clock):
    backend = Backend().on("/candles", day_candles(TODAY))
    cb, _ = coinbase_client(backend)
    out = asyncio.run(cb.quotes(["SUI20947-USD", "TON11419-USD", "12345-USD"]))
    assert sorted(out) == ["SUI20947-USD", "TON11419-USD"] and out["TON11419-USD"].name == "TON"
    assert sorted(c.path for c in backend.calls) == ["/products/SUI-USD/candles", "/products/TON-USD/candles"]


@pytest.mark.parametrize("answer, message, status", [
    (Response(503, ""), "HTTP 503", 503), (Response(500, "oops"), "HTTP 500 (oops)", 500),
    (Response(429, "slow down"), "HTTP 429 (slow down)", 429), (Response(403, "denied"), "Coinbase: HTTP 403", 403),
    (Response(401, ""), "Coinbase: HTTP 401", 401), (Response(410, ""), "Coinbase: HTTP 410", 410),
    (ConnectionError("down"), "ConnectionError: down", None),
])
def test_other_coinbase_failures_raise_and_are_not_an_unknown_product(clock, answer, message, status):
    cb, _ = coinbase_client(Backend().on("/candles", answer))
    with pytest.raises(HttpError) as e:
        asyncio.run(cb.quote("BTC-USD"))
    assert e.value.status == status and str(e.value) == message and cb.supports("BTC-USD")


def test_a_non_json_candle_answer_raises(clock):
    cb, _ = coinbase_client(Backend().on("/candles", Response(200, "<html>maintenance</html>")))
    with pytest.raises(HttpError) as e:
        asyncio.run(cb.quote("BTC-USD"))
    assert e.value.status == 200 and "not JSON" in str(e.value) and cb.supports("BTC-USD")


def candle_server(first_t, last_t, step, inclusive=False, values=None):
    """Coinbase's candles: [time, low, high, open, close, volume], newest first, for times in [start, end)."""
    values = values or (lambda t: 100.0 + (t - first_t) / step)

    def answer(url):
        q = query(url)
        assert int(q["granularity"]) == step
        s = int(datetime.fromisoformat(q["start"]).timestamp())
        e = int(datetime.fromisoformat(q["end"]).timestamp())
        assert e > s and (e - s) // step <= 300
        times = [t for t in range(first_t, last_t + 1, step) if s <= t and (t <= e if inclusive else t < e)]
        rows = []
        for t in reversed(times):
            c = values(t)
            rows.append([t, c - 1.5, c + 1.0, c - 0.5, c, 2.0])
        return J(rows)
    return answer


def page_bounds(call: Call) -> tuple[int, int]:
    q = call.query
    return int(datetime.fromisoformat(q["start"]).timestamp()), int(datetime.fromisoformat(q["end"]).timestamp())


def test_candles_page_back_300_at_a_time():
    T, H = utc(2026, 1, 1), 3600
    backend = Backend().on("/candles", candle_server(T, T + 699 * H, H))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.candles("ETH-USD", H, T, T + 700 * H))
    assert [page_bounds(c) for c in backend.calls] == [(T + 400 * H, T + 700 * H), (T + 100 * H, T + 400 * H),
                                                       (T, T + 100 * H)]
    assert all(c.path == "/products/ETH-USD/candles" and c.query["granularity"] == "3600" for c in backend.calls)
    assert backend.calls[0].query["start"] == datetime.fromtimestamp(T + 400 * H, timezone.utc).isoformat()
    assert len(bars) == 700 and list(bars.t) == list(range(T, T + 700 * H, H))
    assert bars.close[0] == 100.0 and bars.close[-1] == 799.0
    assert np.allclose(bars.open, bars.close - 0.5) and np.allclose(bars.high, bars.close + 1.0)
    assert np.allclose(bars.low, bars.close - 1.5) and np.allclose(bars.volume, 2.0 * bars.close)  # dollars
    assert bars.meta["source"] == "Coinbase" and bars.symbol == "ETH-USD"


def test_candles_overlapping_pages_do_not_repeat_bars():
    T, H = utc(2026, 1, 1), 3600
    backend = Backend().on("/candles", candle_server(T, T + 699 * H, H, inclusive=True))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.candles("ETH-USD", H, T, T + 700 * H))
    assert len(bars) == 700 and len(set(bars.t.tolist())) == 700


def test_candles_stop_at_the_first_empty_page():
    T, H = utc(2026, 1, 1), 3600
    backend = Backend().on("/candles", candle_server(T + 500 * H, T + 699 * H, H))  # listed at T + 500h
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.candles("NEW-USD", H, T, T + 700 * H))
    assert len(backend.calls) == 2 and len(bars) == 200 and bars.t[0] == T + 500 * H


def test_candles_skip_short_rows_and_without_any_raise_404():
    T, H = utc(2026, 1, 1), 3600
    backend = Backend().on("/candles", J([[T + H, 1.0, 2.0, 1.5, 1.8], [T, 1.0, 2.0, 1.5, 1.8, 10.0], []]))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.candles("BTC-USD", H, T, T + 10 * H))
    assert list(bars.t) == [T] and bars.volume[0] == pytest.approx(18.0)

    backend = Backend().on("/candles", J([]))
    cb, _ = coinbase_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(cb.candles("BTC-USD", H, T, T + 10 * H))
    assert e.value.status == 404 and len(backend.calls) == 1

    backend = Backend()
    cb, _ = coinbase_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(cb.candles("AAPL", H, T, T + 10 * H))
    assert e.value.status == 404 and not backend.calls
    with pytest.raises(HttpError):
        asyncio.run(cb.candles("BTC-USD", H, T, T))  # an empty range asks nothing and has nothing
    assert not backend.calls


def test_candles_for_an_unlisted_product_mark_it_unknown():
    backend = Backend().on("/candles", Response(404, '{"message":"NotFound"}'))
    cb, _ = coinbase_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(cb.candles("NOPE-USD", DAY, utc(2026, 1, 1), utc(2026, 2, 1)))
    assert e.value.status == 404 and not cb.supports("NOPE-USD")


def test_candles_failing_mid_way_raise():
    T, H = utc(2026, 1, 1), 3600
    good = candle_server(T, T + 699 * H, H)
    backend = Backend().on("/candles", good, Response(500, "busy"))
    cb, _ = coinbase_client(backend)
    with pytest.raises(HttpError) as e:
        asyncio.run(cb.candles("ETH-USD", H, T, T + 700 * H))
    assert e.value.status == 500 and cb.supports("ETH-USD")


def test_daily_goes_back_to_the_listing(clock):
    now = int(clock.wall)
    today = now - now % DAY
    listed = today - 400 * DAY
    backend = Backend().on("/candles", candle_server(listed, today, DAY))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.daily("SOL-USD"))
    assert len(bars) == 401 and bars.t[0] == listed and bars.t[-1] == today
    assert [page_bounds(c) for c in backend.calls] == [(now - 299 * DAY, now + DAY),
                                                       (now - 599 * DAY, now - 299 * DAY),
                                                       (now - 899 * DAY, now - 599 * DAY)]
    assert all(c.query["granularity"] == "86400" for c in backend.calls)


def test_daily_from_a_start_and_never_before_2015(clock):
    now = int(clock.wall)
    today = now - now % DAY
    backend = Backend().on("/candles", candle_server(utc(2015, 1, 1), today, DAY))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.daily("BTC-USD", start=now - 10 * DAY))
    assert [page_bounds(c) for c in backend.calls] == [(now - 10 * DAY, now + DAY)]
    assert len(bars) == 10

    backend = Backend().on("/candles", candle_server(utc(2015, 1, 1), today, DAY))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.daily("BTC-USD", start=0))
    assert page_bounds(backend.calls[-1])[0] == utc(2015, 1, 1)
    assert bars.t[0] == utc(2015, 1, 1) and bars.t[-1] == today and len(bars) == (today - utc(2015, 1, 1)) // DAY + 1


def test_intraday_granularity_and_days(clock):
    now = int(clock.wall)
    backend = Backend().on("/candles", candle_server(now - 10 * DAY, now, 300))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.intraday("BTC-USD"))
    assert [page_bounds(c) for c in backend.calls] == [(now - DAY, now)]
    assert backend.calls[0].query["granularity"] == "300"
    assert len(bars) == 288 and bars.t[-1] == now - 300 and bars.t[0] == now - DAY  # many bars per day are kept

    backend = Backend().on("/candles", candle_server(now - 10 * DAY, now, 900))
    cb, _ = coinbase_client(backend)
    bars = asyncio.run(cb.intraday("BTC-USD", days=5, granularity=900))
    assert [page_bounds(c) for c in backend.calls] == [(now - 270_000, now), (now - 5 * DAY, now - 270_000)]
    assert len(bars) == 480 and backend.calls[0].query["granularity"] == "900"
