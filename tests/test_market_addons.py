"""The shared pieces the add-on features stand on: the keyed API client and the feature loader."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from marketbot import addons  # noqa: E402
from marketbot.apis import Api, ApiError, env_key  # noqa: E402
from marketbot.http import Http, Response  # noqa: E402


class Backend:
    name = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def get(self, url, headers, timeout, proxy):
        self.calls.append((url, headers))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self):
        pass


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


async def _no_sleep(_):
    pass


def api(*answers, key="k-secret-123", clock=None, **kw):
    clock = clock or Clock()
    backend = Backend(*answers)
    http = Http(backend, sleep=_no_sleep)
    a = Api("Svc", http, "https://svc.test/v1", key, clock=clock, sleep=clock.sleep, wall=lambda: 1.76e9 + clock.t,
            **kw)
    return a, backend, http


def ok(data):
    return Response(200, json.dumps(data))


def test_env_key_takes_the_first_set_name_and_strips_quotes():
    env = {"A": "  ", "B": ' "abc" ', "C": "zzz"}
    assert env_key("A", "B", "C", env=env) == "abc" and env_key("X", env=env) == ""


def test_key_goes_in_a_header_or_a_param():
    a, backend, _ = api(ok({"x": 1}), key_header="X-Token")
    assert asyncio.run(a.get("/quote", {"symbol": "AAPL"})) == {"x": 1}
    url, headers = backend.calls[0]
    assert url == "https://svc.test/v1/quote?symbol=AAPL" and headers["X-Token"] == "k-secret-123"
    a, backend, _ = api(ok({}), key_param="apikey")
    asyncio.run(a.get("quote"))
    assert backend.calls[0][0].endswith("quote?apikey=k-secret-123")


def test_no_key_means_disabled_unless_the_service_needs_none():
    a, backend, _ = api(ok({}), key="")
    assert not a.enabled and a.status_line() == "no key"
    with pytest.raises(ApiError) as e:
        asyncio.run(a.get("x"))
    assert e.value.kind == "key" and not backend.calls
    free, backend, _ = api(ok([1]), key="", needs_key=False)
    assert free.enabled and asyncio.run(free.get("x")) == [1]


def test_rate_limit_is_ours_first():
    clock = Clock()
    a, backend, _ = api(ok({}), clock=clock, limits=((2, 60.0),))
    asyncio.run(a.get("x"))
    asyncio.run(a.get("x"))
    with pytest.raises(ApiError) as e:
        asyncio.run(a.get("x"))
    assert e.value.kind == "budget" and len(backend.calls) == 2
    asyncio.run(a.get("x", wait=120))  # waits for room
    assert len(backend.calls) == 3 and clock.t >= 1060


def test_a_budget_miss_on_one_limit_gives_back_the_other_slots():
    a, backend, _ = api(ok({}), limits=((5, 1.0), (1, 86400.0)))
    asyncio.run(a.get("x"))
    with pytest.raises(ApiError):
        asyncio.run(a.get("x"))
    assert a.limiters[0].used() == 1 and a.limiters[1].used() == 1


@pytest.mark.parametrize("status, body, kind", [
    (401, '{"error": "Invalid API key"}', "key"),
    (403, '{"error": "You don\'t have access to this resource."}', "plan"),
    (402, "Payment required", "plan"),
    (404, "not found", "missing"),
    (400, '{"error": "bad symbol"}', "bad"),
    (200, "<html>oops</html>", "bad"),
])
def test_errors_are_sorted_by_kind(status, body, kind):
    a, _, _ = api(Response(status, body))
    with pytest.raises(ApiError) as e:
        asyncio.run(a.get("x"))
    assert e.value.kind == kind
    assert a.key_rejected is (kind == "key")
    if kind == "key":
        assert not a.enabled and a.status_line() == "⚠️ key rejected"


def test_429_backs_off_all_limits():
    a, _, _ = api(Response(429, "slow down"), limits=((60, 60.0), (100, 86400.0)))
    with pytest.raises(ApiError) as e:
        asyncio.run(a.get("x"))
    assert e.value.kind == "rate" and a.wait_time() > 0 and "rate limited" in a.last_error


def test_the_key_never_shows_in_errors():
    a, _, _ = api(Response(400, '{"error": "bad request for apikey=k-secret-123 and k-secret-123"}'))
    with pytest.raises(ApiError) as e:
        asyncio.run(a.get("x"))
    assert "k-secret-123" not in str(e.value)
    a, _, _ = api(OSError("boom k-secret-123"), key_param="apikey")
    with pytest.raises(ApiError) as e:
        asyncio.run(a.get("x"))
    assert "k-secret-123" not in str(e.value) and "k-secret-123" not in (a.last_error or "")


def test_daily_limits_survive_a_restart(tmp_path):
    clock = Clock()
    state = tmp_path / "apis.json"
    a, _, _ = api(ok({}), clock=clock, limits=((10, 60.0), (3, 86400.0)), state_file=state)
    for _ in range(3):
        asyncio.run(a.get("x"))
    clock.t += 3600
    again, _, _ = api(ok({}), clock=clock, limits=((10, 60.0), (3, 86400.0)), state_file=state)
    assert again.limiters[1].used() == 3 and again.limiters[0].used() == 0
    with pytest.raises(ApiError):
        asyncio.run(again.get("x"))
    assert "3/3 in 24h" in again.status_line()


# ----- loading add-ons -----

class Good(addons.Feature):
    name = "good"
    help_group = "✨ Extras"

    def jobs(self):
        return [("good_job", 60, self.run)]

    async def run(self):
        pass

    def help(self):
        return [("good", "does good things")]

    def status(self):
        return ["Good: fine"]


class Broken(addons.Feature):
    name = "broken"

    def __init__(self, bot):
        raise RuntimeError("no")


def test_a_broken_addon_is_left_out_and_the_rest_load(monkeypatch):
    from marketbot.addons import registry
    monkeypatch.setattr(registry, "FEATURES", [Broken, Good])
    loaded = addons.load(SimpleNamespace())
    assert [f.name for f in loaded] == ["good"]


def test_addons_plug_into_jobs_help_and_status(tmp_path, monkeypatch):
    from marketbot.addons import registry
    from marketbot.ai import NewsAI
    from marketbot.bot import MarketBot
    monkeypatch.setattr(registry, "FEATURES", [Good])

    class FakeEngine:
        data = SimpleNamespace()
        models = {}

        async def close(self):
            pass

    bot = MarketBot(tmp_path, engine=FakeEngine(), ai=NewsAI(readers=[]))
    assert ("good_job", 60, bot.features[0].run) in bot.schedule()
    from marketbot.commands import register_commands
    register_commands(bot)
    from tests.live_rehearsal import Interaction

    async def fetch_commands():
        return []

    bot.tree.fetch_commands = fetch_commands
    it = Interaction(1)
    asyncio.run(next(c for c in bot.tree.get_commands() if c.name == "help").callback(it))
    fields = {f.name: f.value for f in it.out[0][1]["embed"].fields}
    assert fields["✨ Extras"] == "`/good` does good things"
