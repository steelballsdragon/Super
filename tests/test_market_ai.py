"""The AI news readers: Claude, and the free plans of Groq and Gemini (paced, rate limited, failing over)."""

import asyncio
import json
import time

import pytest

pytest.importorskip("numpy")

from marketbot import ai  # noqa: E402
from marketbot.feeds import Headline  # noqa: E402
from marketbot.news import analyse  # noqa: E402

START = 1_760_000_000.0  # a weekday afternoon, UTC


def h(title, summary=""):
    return Headline("id-" + title[:30], title, summary, "https://x.test/a", "Test", time.time(), "stocks", 1.0, ())


def news(n):
    return [analyse(h(f"Fed cuts rates by 25 basis points, story {i}")) for i in range(n)]


class Clock:
    """Fake time: sleeping moves it forward."""

    def __init__(self, t=START):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def item(i, **kw):
    out = {"id": str(i), "relevant": True, "event": "Fed", "takeaway": f"Takeaway {i}.", "importance": 66,
           "confidence": "Medium", "polarity": "good",
           "impacts": [{"asset": "SPX", "ticker": "", "direction": "up", "move_low": 0.2, "move_high": 0.6}]}
    out.update(kw)
    return out


def answer(n, tokens=2500, finish="stop", content=None):
    body = {"choices": [{"index": 0, "finish_reason": finish, "message": {
        "role": "assistant", "content": content if content is not None else json.dumps(
            {"items": [item(i) for i in range(n)]})}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": tokens - 1200, "total_tokens": tokens}}
    return 200, {}, json.dumps(body)


class Service:
    """A fake OpenAI-style API: answers come from `script` (a list of replies or callables), in order."""

    def __init__(self, *script, models=None):
        self.script = list(script)
        self.calls = []
        self.models = models

    async def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return 200, {}, json.dumps({"object": "list", "data": [{"id": m} for m in self.models or []]})
        reply = self.script.pop(0) if self.script else answer(body["messages"][1]["content"].count('"headline"'))
        if callable(reply):
            reply = reply(body)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def reader(plan=ai.GROQ, service=None, clock=None, key="gsk_secret123", model=None):
    clock = clock or Clock()
    return ai.OpenAIReader(plan, key, model, request=service or Service(), clock=clock)


def desk(*readers, clock):
    return ai.NewsAI(readers=list(readers), sleep=clock.sleep, clock=clock)


# ----- which readers turn on -----

def test_keys_pick_the_readers_in_order_claude_groq_gemini():
    pytest.importorskip("anthropic")
    env = {"ANTHROPIC_API_KEY": "sk-ant", "GROQ_API_KEY": " gsk_1 ", "GEMINI_API_KEY": "AIza1", "GEMINI_MODEL": "gemini-x"}
    d = ai.NewsAI(env=env)
    assert [r.name for r in d.readers] == ["Claude", "Groq", "Gemini"]
    assert d.readers[1].key == "gsk_1" and d.readers[1].model == "openai/gpt-oss-120b"
    assert d.readers[2].model == "gemini-x" and d.enabled
    assert [s.free for s in d.statuses()] == [False, True, True]


def test_no_keys_means_the_keyword_model():
    d = ai.NewsAI(env={"GROQ_API_KEY": "  ", "GEMINI_API_KEY": ""})
    assert not d.enabled and d.statuses() == []
    assert asyncio.run(d.review(news(3))) == 0


def test_free_readers_need_no_anthropic_key():
    d = ai.NewsAI(api_key="", env={"GROQ_API_KEY": "gsk_1"})
    assert [r.name for r in d.readers] == ["Groq"]


def test_a_bad_daily_cap_falls_back_to_the_default():
    assert ai.NewsAI(env={"NEWS_AI_DAILY_CALLS": "lots"}).daily_calls == ai.DAILY_CALLS
    assert ai.NewsAI(env={"NEWS_AI_DAILY_CALLS": "20"}).daily_calls == 20


def test_unrelated_google_keys_are_not_used():
    assert not ai.NewsAI(env={"GOOGLE_API_KEY": "AIza-maps-key"}).enabled


# ----- the request -----

def test_groq_request_is_structured_and_authorised():
    clock = Clock()
    service = Service(answer(3))
    r = reader(service=service, clock=clock)
    batch = news(3)
    assert asyncio.run(desk(r, clock=clock).review(batch)) == 3
    method, url, headers, body = service.calls[0]
    assert (method, url) == ("POST", "https://api.groq.com/openai/v1/chat/completions")
    assert headers["Authorization"] == "Bearer gsk_secret123"
    assert body["model"] == "openai/gpt-oss-120b" and body["reasoning_effort"] == "low"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == ai.SCHEMA
    assert body["messages"][0] == {"role": "system", "content": ai.SYSTEM}
    assert body["max_completion_tokens"] == ai.GROQ.max_tokens
    assert all(a.source == "ai" and a.note.startswith("Takeaway") for a in batch)
    assert r.status.calls_today == 1 and r.status.last_error is None and r.status.last_ok
    assert r.pacer.typical == 2500 and r.pacer.tokens_today == 2500


def test_gemini_uses_its_openai_endpoint():
    clock = Clock()
    service = Service(answer(2))
    r = reader(ai.GEMINI, service, clock, key="AIzaKey")
    asyncio.run(desk(r, clock=clock).review(news(2)))
    assert service.calls[0][1] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert service.calls[0][3]["model"] == "gemini-3.5-flash-lite"


def test_most_important_headlines_go_first_in_free_sized_batches():
    clock = Clock()
    service = Service()
    r = reader(service=service, clock=clock)
    batch = news(7)
    for i, a in enumerate(batch):
        a.importance = 40 + i
    asyncio.run(desk(r, clock=clock).review(batch))
    first = service.posts()[0][3]["messages"][1]["content"]
    assert first.count('"headline"') == ai.GROQ.batch == 5
    assert "story 6" in first and "story 0" not in first
    assert len(service.posts()) == 2


# ----- pacing under the free limits -----

def test_groq_calls_are_paced_by_tokens_per_minute():
    clock = Clock()
    service = Service(*[answer(5, tokens=4000) for _ in range(4)])
    r = reader(service=service, clock=clock)
    batch = news(20)
    assert asyncio.run(desk(r, clock=clock).review(batch)) == 10
    # 8,000 tokens a minute less a 10% margin: after a 4,000-token call the next waits for it to age out, and a
    # third would wait past the run's time, so the last 10 headlines keep the rules' read.
    assert len(service.posts()) == 2 and clock.slept == [60]
    assert sum(a.source == "ai" for a in batch) == 10


def test_smaller_calls_go_back_to_back():
    clock = Clock()
    service = Service(*[answer(5, tokens=2000) for _ in range(3)])
    r = reader(service=service, clock=clock)
    asyncio.run(desk(r, clock=clock).review(news(15)))
    assert len(service.posts()) == 3 and clock.slept == []  # 3 x 2,000 tokens fit in a minute's 7,200


def test_pacing_never_holds_the_news_past_the_run_budget():
    clock = Clock()
    service = Service(*[answer(5, tokens=7000) for _ in range(10)])
    r = reader(service=service, clock=clock)
    batch = news(50)
    changed = asyncio.run(desk(r, clock=clock).review(batch))
    assert clock.t - START <= ai.REVIEW_SECONDS + 1
    assert changed == 5 * len(service.posts()) < 50
    assert sum(a.source == "ai" for a in batch) == changed  # the rest keep the rules' read


def test_pacer_counts_requests_per_minute():
    clock = Clock()
    p = ai.Pacer(ai.Limits(rpm=10), clock)
    for _ in range(8):
        assert p.wait(10) == 0
        p.finish(p.start(10), 10)
        clock.t += 1
    assert p.wait(10) == 0
    p.finish(p.start(10), 10)  # the 9th: 10 a minute less the margin
    assert p.wait(10) == pytest.approx(52)  # until the first ages out
    clock.t += 52
    assert p.wait(10) == 0


def test_pacer_stops_for_the_day_and_resets_at_midnight_utc():
    clock = Clock()
    p = ai.Pacer(ai.Limits(rpd=10, tpd=100_000), clock)
    for _ in range(9):
        p.finish(p.start(1000), 1000)
    assert p.wait(1000) is None  # 10 a day less the 10% margin
    clock.t = (int(START) // 86400 + 1) * 86400 + 5
    assert p.wait(1000) == 0 and p.calls_today == 0


def test_pacer_stops_before_the_daily_token_limit():
    clock = Clock()
    p = ai.Pacer(ai.Limits(tpd=10_000), clock)
    p.finish(p.start(4000), 8500)
    assert p.wait(1000) is None and p.tokens_today == 8500


def test_pacer_lets_an_oversized_call_through_alone():
    clock = Clock()
    p = ai.Pacer(ai.Limits(tpm=1000), clock)
    assert p.wait(5000) == 0
    p.finish(p.start(5000), 5000)
    assert 59 < p.wait(5000) <= 60


def test_pacer_ignores_nonsense_usage():
    p = ai.Pacer(ai.Limits(), Clock())
    entry = p.start(4000)
    for bad in (None, "12", -5, float("nan"), True):
        p.finish(entry, bad)
    assert entry[1] == 4000 and p.typical == 0


def test_daily_cap_applies_to_free_readers_too():
    clock = Clock()
    service = Service()
    r = reader(service=service, clock=clock)
    d = ai.NewsAI(readers=[r], daily_calls=2, sleep=clock.sleep, clock=clock)
    asyncio.run(d.review(news(30)))
    assert len(service.posts()) == 2 and r.calls_today() == 2


# ----- rate limits and failures -----

def test_429_pauses_for_as_long_as_asked_then_carries_on():
    clock = Clock()
    service = Service((429, {"retry-after": "7"}, json.dumps({"error": {"message": "Rate limit reached"}})),
                      answer(3))
    r = reader(service=service, clock=clock)
    batch = news(3)
    assert asyncio.run(desk(r, clock=clock).review(batch)) == 3
    assert len(service.posts()) == 2 and clock.slept == [pytest.approx(7)]
    assert r.status.last_error is None and r.limited == 0


def test_long_429_leaves_the_rest_to_the_rules_and_says_so():
    clock = Clock()
    groq_msg = ("Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): Limit 200000, Used "
                "199000, Requested 4000. Please try again in 7m12.48s.")
    service = Service((429, {}, json.dumps({"error": {"message": groq_msg, "type": "tokens"}})))
    r = reader(service=service, clock=clock)
    batch = news(3)
    assert asyncio.run(desk(r, clock=clock).review(batch)) == 0
    assert len(service.posts()) == 1 and not clock.slept
    assert r.status.last_error == "rate limited · paused 7 min"
    assert r.wait() == pytest.approx(432.48)
    assert all(a.source != "ai" for a in batch)


def test_repeated_429s_back_off_further_each_time():
    clock = Clock()
    service = Service(*[(429, {"retry-after": "1"}, "{}") for _ in range(3)])
    r = reader(service=service, clock=clock)
    pauses = []
    for _ in range(3):
        with pytest.raises(ai.ReaderError) as e:
            asyncio.run(r.read("x"))
        pauses.append(e.value.pause)
    assert pauses == [5, 10, 20] and all(e is not None for e in pauses)


def test_429_without_a_hint_pauses_half_a_minute():
    r = reader(service=Service((429, {}, "slow down")))
    with pytest.raises(ai.ReaderError) as e:
        asyncio.run(r.read("x"))
    assert e.value.pause == 30 and e.value.again


def test_another_reader_takes_over_while_one_is_rate_limited():
    clock = Clock()
    groq = Service((429, {"retry-after": "3600"}, "{}"))
    gemini = Service(answer(3))
    g, m = reader(ai.GROQ, groq, clock), reader(ai.GEMINI, gemini, clock, key="AIza")
    batch = news(3)
    assert asyncio.run(desk(g, m, clock=clock).review(batch)) == 3
    assert len(groq.posts()) == 1 and len(gemini.posts()) == 1
    assert "rate limited" in g.status.last_error and m.status.last_error is None


def test_a_reader_that_fails_sits_out_the_rest_of_the_run():
    clock = Clock()
    service = Service((503, {}, "<html>Service Unavailable</html>"))
    r = reader(service=service, clock=clock)
    assert asyncio.run(desk(r, clock=clock).review(news(12))) == 0
    assert len(service.posts()) == 1 and r.status.last_error == "HTTP 503"
    assert r.wait() == 60


def test_rejected_key_rests_an_hour_and_is_never_echoed():
    for status, body in ((401, {"error": {"message": "Invalid API Key gsk_secret123"}}),
                         (400, [{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                           "status": "INVALID_ARGUMENT"}}])):
        clock = Clock()
        r = reader(service=Service((status, {}, json.dumps(body))), clock=clock)
        asyncio.run(desk(r, clock=clock).review(news(2)))
        assert r.status.last_error == f"key rejected (HTTP {status})"
        assert r.wait() == 3600 and not r.plain


def test_error_text_never_carries_the_key():
    clock = Clock()
    r = reader(service=Service((500, {}, json.dumps({"error": {"message": "bad header Bearer gsk_secret123"}}))),
               clock=clock)
    asyncio.run(desk(r, clock=clock).review(news(1)))
    assert "gsk_secret123" not in r.status.last_error and "HTTP 500" in r.status.last_error


def test_network_errors_are_reported_and_rested():
    clock = Clock()
    r = reader(service=Service(OSError("connection reset")), clock=clock)
    asyncio.run(desk(r, clock=clock).review(news(2)))
    assert r.status.last_error == "couldn't reach Groq (OSError)" and r.wait() == 60


def test_refused_structured_output_falls_back_to_plain_json():
    clock = Clock()
    service = Service((400, {}, json.dumps({"error": {"message": "response_format json_schema is not supported"}})),
                      answer(2))
    r = reader(service=service, clock=clock)
    assert asyncio.run(desk(r, clock=clock).review(news(2))) == 2
    first, second = service.posts()[0][3], service.posts()[1][3]
    assert first["response_format"]["type"] == "json_schema" and "reasoning_effort" in first
    assert second["response_format"] == {"type": "json_object"} and "reasoning_effort" not in second
    assert second["messages"][1]["content"].endswith(ai.PLAIN_JSON) and "JSON" in ai.PLAIN_JSON
    assert r.plain and r.status.calls_today == 2
    asyncio.run(desk(r, clock=clock).review(news(1)))
    assert service.posts()[2][3]["response_format"] == {"type": "json_object"}


def test_a_retired_model_is_swapped_for_the_closest_one():
    clock = Clock()
    service = Service((404, {}, json.dumps({"error": {"message": "The model `gemini-3.5-flash-lite` does not exist"}})),
                      answer(2),
                      models=["models/gemini-2.5-flash-lite", "models/gemini-4.1-flash-lite",
                              "models/gemini-4.2-flash-lite-preview-11-2026", "models/gemini-4.1-flash-tts",
                              "models/text-embedding-004", "models/gemini-4.1-flash"])
    r = reader(ai.GEMINI, service, clock, key="AIza")
    assert asyncio.run(desk(r, clock=clock).review(news(2))) == 2
    assert r.model == "gemini-4.1-flash-lite"
    assert service.calls[1][:2] == ("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models")
    assert service.posts()[1][3]["model"] == "gemini-4.1-flash-lite"


def test_a_404_with_no_replacement_is_reported():
    clock = Clock()
    service = Service((404, {}, json.dumps({"error": {"message": "model not found"}})), models=[])
    r = reader(service=service, clock=clock)
    asyncio.run(desk(r, clock=clock).review(news(2)))
    assert r.status.last_error == "model openai/gpt-oss-120b isn't available (HTTP 404)"
    assert r.model == "openai/gpt-oss-120b" and r.wait() == 3600 and not r.plain


GROQ_RETIRED = {"error": {"message": "The model `openai/gpt-oss-120b` has been decommissioned and is no longer "
                                    "supported. Please refer to https://console.groq.com/docs/deprecations for a "
                                    "recommendation on which model to use instead.",
                         "type": "invalid_request_error", "code": "model_decommissioned"}}


def test_groq_retiring_the_model_with_a_400_switches_models_not_formats():
    clock = Clock()
    service = Service((400, {}, json.dumps(GROQ_RETIRED)), answer(2),
                      models=["openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b", "whisper-large-v3"])
    r = reader(service=service, clock=clock)
    assert asyncio.run(desk(r, clock=clock).review(news(2))) == 2
    assert r.model == "openai/gpt-oss-20b" and not r.plain
    assert service.posts()[1][3]["response_format"]["type"] == "json_schema"
    assert not r.searched  # a later retirement gets looked up again


def test_a_retired_model_found_by_its_message_alone():
    clock = Clock()
    body = {"error": {"message": "The model `x` does not exist or you do not have access to it."}}
    r = reader(service=Service((400, {}, json.dumps(body)), models=[]), clock=clock)
    asyncio.run(desk(r, clock=clock).review(news(1)))
    assert r.status.last_error == "model openai/gpt-oss-120b isn't available (HTTP 400)" and not r.plain


def test_a_schema_complaint_is_not_mistaken_for_a_retired_model():
    clock = Clock()
    body = {"error": {"message": "Invalid schema for response_format: property `ticker` does not exist in required"}}
    service = Service((400, {}, json.dumps(body)), answer(1))
    r = reader(service=service, clock=clock)
    assert asyncio.run(desk(r, clock=clock).review(news(1))) == 1
    assert r.plain and not service.calls[1][0] == "GET"


def test_endless_429s_never_overflow_the_backoff():
    r = reader(service=Service(*[(429, {"retry-after": "37"}, "{}") for _ in range(3)]))
    r.limited = 5000
    for _ in range(3):
        with pytest.raises(ai.ReaderError) as e:
            asyncio.run(r.read("x"))
        assert e.value.pause == 5 * 2 ** 11 and e.value.again


def test_a_bug_in_a_reader_never_stops_the_news():
    clock = Clock()

    class Broken(ai.Reader):
        async def read(self, prompt):
            raise RuntimeError("boom")

    broken = Broken("Broken", "m", clock)
    gemini = Service(answer(3))
    m = reader(ai.GEMINI, gemini, clock, key="AIza")
    batch = news(3)
    assert asyncio.run(desk(broken, m, clock=clock).review(batch)) == 3
    assert broken.status.last_error == "unexpected error (RuntimeError)" and broken.wait() == 600
    assert all(a.source == "ai" for a in batch)


def test_a_bad_answer_never_stops_the_news(monkeypatch):
    clock = Clock()
    r = reader(service=Service(answer(1)), clock=clock)
    monkeypatch.setattr(ai, "fold", lambda batch, items: 1 / 0)
    assert asyncio.run(desk(r, clock=clock).review(news(1))) == 0


def test_status_says_when_a_reader_is_out_for_the_day():
    clock = Clock()
    service = Service()
    r = reader(service=service, clock=clock)
    d = desk(r, clock=clock)
    assert d.statuses()[0].resting is None
    r.pacer.tokens_today = 179_000
    r.pacer.day = time.strftime("%Y-%m-%d", time.gmtime(clock.t))
    assert d.statuses()[0].resting == "used the free plan's daily allowance · back at 00:00 UTC"
    assert asyncio.run(d.review(news(2))) == 0 and not service.posts()
    clock.t = (int(START) // 86400 + 1) * 86400 + 1
    assert d.statuses()[0].resting is None
    capped = ai.NewsAI(readers=[r], daily_calls=1, sleep=clock.sleep, clock=clock)
    asyncio.run(capped.review(news(1)))
    assert capped.statuses()[0].resting.startswith("reached 1 calls (NEWS_AI_DAILY_CALLS)")


def test_cached_prompt_tokens_dont_count_against_groq_limits():
    clock = Clock()
    status, headers, text = answer(2, tokens=2500)
    body = json.loads(text)
    body["usage"]["prompt_tokens_details"] = {"cached_tokens": 600}
    r = reader(service=Service((status, headers, json.dumps(body))), clock=clock)
    asyncio.run(desk(r, clock=clock).review(news(2)))
    assert r.pacer.tokens_today == 1900


def test_a_cut_off_answer_is_an_error():
    clock = Clock()
    r = reader(service=Service(answer(2, finish="length", content='{"items": [{"id": "0"')), clock=clock)
    batch = news(2)
    assert asyncio.run(desk(r, clock=clock).review(batch)) == 0
    assert r.status.last_error == "answer cut off" and all(a.source != "ai" for a in batch)


def test_unreadable_answers_are_errors():
    for reply in ((200, {}, "<html>gateway</html>"), (200, {}, json.dumps({"choices": []})),
                  answer(1, content="I can't help with that.")):
        clock = Clock()
        r = reader(service=Service(reply), clock=clock)
        asyncio.run(desk(r, clock=clock).review(news(1)))
        assert r.status.last_error == "unreadable answer"


def test_cancelling_the_news_job_cancels_the_call():
    clock = Clock()
    r = reader(service=Service(asyncio.CancelledError()), clock=clock)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(desk(r, clock=clock).review(news(1)))


# ----- reading messy answers -----

def test_answers_in_code_blocks_or_prose_are_read():
    items = {"items": [item(0)]}
    for text in (json.dumps(items), "```json\n" + json.dumps(items) + "\n```",
                 "Here you go: " + json.dumps(items) + " Hope that helps.", json.dumps([item(0)])):
        assert ai.parse_items(text)[0]["id"] == "0"
    for text in ("", "no json here", '{"other": 1}', "null"):
        with pytest.raises(ValueError):
            ai.parse_items(text)


def test_loose_items_are_cleaned_before_use():
    raw = {"id": 1, "relevant": "true", "event": None, "takeaway": " Hot CPI. ", "importance": "85",
           "confidence": "high", "polarity": "BAD",
           "impacts": [{"asset": "spx", "ticker": None, "direction": "Down", "move_low": "0.5%", "move_high": "1.2%"},
                       {"asset": "UST10", "direction": "sideways", "move_low": "8bp"},
                       {"asset": "MOON", "direction": "up", "move_low": 1, "move_high": 2},
                       {"asset": "TICKER", "ticker": "nvda!", "direction": "up", "move_low": 1e9, "move_high": 2},
                       {"asset": "BTC", "direction": "up"}, "junk"]}
    item = ai.clean(raw)
    assert item["id"] == "1" and item["relevant"] is True and item["importance"] == 85
    assert item["confidence"] == "High" and item["polarity"] == "bad" and item["takeaway"] == "Hot CPI."
    assert [(i["asset"], i["direction"], i["move_low"], i["move_high"]) for i in item["impacts"]] == [
        ("SPX", "down", 0.5, 1.2), ("UST10", "either", 8, 8), ("TICKER", "up", 50, 2)]
    assert item["impacts"][2]["ticker"] == "NVDA"
    assert ai.clean({"relevant": True}) is None and ai.clean("x") is None and ai.clean({"id": [1]}) is None
    assert "importance" not in ai.clean({"id": "0", "importance": "very"})
    assert ai.clean({"id": "0", "relevant": "false"})["relevant"] is False


def test_fold_ignores_unknown_duplicate_and_negative_ids():
    batch = news(2)
    items = [item(1, takeaway="first"), item(1, takeaway="second"), item(-1), item(7), item("0.0"),
             {"id": "x"}, None, item(1.5)]
    assert ai.fold(batch, items) == 2
    assert batch[1].note == "first" and batch[0].note == "Takeaway 0.0."
    assert ai.fold(batch, "not a list") == 0


def test_an_irrelevant_read_demotes_the_story():
    batch = news(1)
    ai.fold(batch, [item(0, relevant=False, importance=95)])
    assert batch[0].importance <= 15 and batch[0].impacts == []


def test_missing_importance_keeps_the_rules_score():
    batch = news(1)
    before = batch[0].importance
    raw = item(0)
    del raw["importance"]
    ai.fold(batch, [raw])
    assert batch[0].importance == before and batch[0].source == "ai"


@pytest.mark.parametrize("headers, text, seconds", [
    ({"retry-after": "12"}, "", 12), ({"retry-after": "0.5"}, "", 0.5),
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, "", None),
    ({}, '{"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}', 37),
    ({}, "Please try again in 7m12.48s.", 432.48), ({}, "Please try again in 1h2m3s.", 3723),
    ({}, "Please try again in 450ms.", 0.45), ({}, "Please try again in 2.5s.", 2.5),
    ({}, "Please try again in 3m.", 180), ({}, "slow down", None), ({}, "try again in a bit", None),
])
def test_retry_after(headers, text, seconds):
    assert ai.retry_after(headers, text) == (pytest.approx(seconds) if seconds is not None else None)


def test_pick_model_prefers_stable_and_newest():
    ids = ["openai/gpt-oss-20b", "openai/gpt-oss-safeguard-20b", "meta-llama/llama-prompt-guard-2-86m",
           "qwen/qwen3.8-27b", "whisper-large-v3"]
    assert ai.pick_model(ids, ai.GROQ.prefer) == "openai/gpt-oss-20b"
    assert ai.pick_model(["whisper-large-v3"], ai.GROQ.prefer) is None
    assert ai.pick_model(["gemini-3.8-flash", "gemini-3.8-flash-image", "gemini-3.8-flash-live"],
                         ai.GEMINI.prefer) == "gemini-3.8-flash"


def test_number_reads_loose_values():
    assert [ai.number(v) for v in (1, "1.5", "-0.4%", "+8bp", " 12 bps ", "1e3", "nan", float("inf"), True,
                                    None, "0.4-0.6", [1], 10 ** 400)] == [1, 1.5, -0.4, 8, 12, None, None, None, None, None,
                                                               None, None, None]


def test_duration():
    assert [ai.duration(s) for s in (7, 432, 3600, 21600)] == ["7s", "7 min", "60 min", "6.0 h"]


# ----- the real HTTP client -----

def test_real_client_posts_json_and_lowercases_headers(monkeypatch):
    pytest.importorskip("aiohttp")
    from aiohttp import web

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return web.json_response(json.loads(answer(1)[2]), headers={"Retry-After": "3", "X-Ratelimit-Remaining-Tokens": "100"})

    async def run():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        plan = ai.Plan("Local", f"http://127.0.0.1:{port}/v1", "X", "Y", "m", ("m",), 5, 100, 100, ai.Limits())
        r = ai.OpenAIReader(plan, "k1")
        try:
            status, headers, text = await r._request("POST", f"{plan.url}/chat/completions", r._headers(), {"a": 1})
            items = await r.read("prompt")
        finally:
            await r.close()
            await runner.cleanup()
        return status, headers, items, r

    status, headers, items, r = asyncio.run(run())
    assert status == 200 and headers["retry-after"] == "3" and seen["auth"] == "Bearer k1"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert items[0]["id"] == "0" and r._session is None


# ----- any feature can ask a short structured question -----

WHY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
              "additionalProperties": False}


def test_complete_returns_the_first_readers_answer_with_the_callers_schema():
    clock = Clock()
    service = Service(answer(0, content=json.dumps({"summary": "Chips fell with the sector."})))
    r = reader(service=service, clock=clock)
    out = asyncio.run(desk(r, clock=clock).complete("You explain moves.", "NVDA -4%", WHY_SCHEMA, "why"))
    assert out == {"summary": "Chips fell with the sector."}
    body = service.posts()[0][3]
    assert body["messages"][0]["content"] == "You explain moves."
    assert body["response_format"]["json_schema"] == {"name": "why", "strict": True, "schema": WHY_SCHEMA}
    assert r.status.calls_today == 1 and r.status.last_error is None


def test_complete_fails_over_and_then_gives_up_quietly():
    clock = Clock()
    groq = Service((503, {}, "down"))
    gemini = Service(answer(0, content='```json\n{"summary": "ok"}\n```'))
    g, m = reader(ai.GROQ, groq, clock), reader(ai.GEMINI, gemini, clock, key="AIza")
    assert asyncio.run(desk(g, m, clock=clock).complete("s", "p", WHY_SCHEMA)) == {"summary": "ok"}
    assert g.status.last_error == "HTTP 503 (down)"
    nothing = Service((200, {}, json.dumps({"choices": [{"message": {"content": "[1, 2]"}, "finish_reason": "stop"}]})))
    r = reader(service=nothing, clock=clock)
    assert asyncio.run(desk(r, clock=clock).complete("s", "p", WHY_SCHEMA)) is None
    assert r.status.last_error == "unreadable answer"
    assert asyncio.run(ai.NewsAI(readers=[]).complete("s", "p", WHY_SCHEMA)) is None


def test_complete_doesnt_wait_long_for_a_paused_reader():
    clock = Clock()
    r = reader(service=Service(answer(0, content='{"summary": "x"}')), clock=clock)
    r.pause(30)
    assert asyncio.run(desk(r, clock=clock).complete("s", "p", WHY_SCHEMA, wait=5)) is None
    assert asyncio.run(desk(r, clock=clock).complete("s", "p", WHY_SCHEMA, wait=40)) == {"summary": "x"}
    assert clock.slept == [pytest.approx(30)]


def test_plain_json_fallback_describes_the_callers_schema():
    clock = Clock()
    service = Service((400, {}, json.dumps({"error": {"message": "json_schema not supported"}})),
                      answer(0, content='{"summary": "plain"}'))
    r = reader(service=service, clock=clock)
    assert asyncio.run(desk(r, clock=clock).complete("s", "p", WHY_SCHEMA)) == {"summary": "plain"}
    assert '"summary"' in service.posts()[1][3]["messages"][1]["content"]


def test_parse_object():
    assert ai.parse_object('Sure: {"a": 1} done') == {"a": 1}
    for bad in ("[1]", "", "nope", "null"):
        with pytest.raises(ValueError):
            ai.parse_object(bad)
