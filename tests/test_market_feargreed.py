"""Fear & Greed: CNN's stock index (with the bot's estimate standing in), the crypto index, zone alerts and the
places they show."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

from marketbot import embeds as E, feargreed as fgm  # noqa: E402
from marketbot.feargreed import CNNFearGreed, Gauge, crypto_gauge, estimate_gauge, moved_zone, parse_cnn, zone  # noqa: E402
from marketbot.http import Http, Response  # noqa: E402

NOW = 1_791_390_000.0  # 2026-10-07, a Wednesday afternoon
DAY = 86400


def cnn_payload(score=43.9, days=252):
    """Shaped like CNN's real answer (trimmed)."""
    hist = [{"x": (NOW - (days - i) * DAY) * 1000, "y": 30 + i % 40, "rating": "fear"} for i in range(days)]
    hist.append({"x": NOW * 1000, "y": score, "rating": "fear"})
    data = {"fear_and_greed": {"score": score, "rating": "fear", "timestamp": "2026-10-07T16:00:00+00:00",
                               "previous_close": 47.2, "previous_1_week": 30.26, "previous_1_month": 45.23,
                               "previous_1_year": 51.86},
            "fear_and_greed_historical": {"timestamp": NOW * 1000, "score": score, "rating": "fear", "data": hist}}
    for key, value in (("market_momentum_sp500", 58.4), ("market_momentum_sp125", 58.4),
                       ("stock_price_strength", 1.8), ("stock_price_breadth", 2.4), ("put_call_options", 30),
                       ("market_volatility_vix", 50), ("market_volatility_vix_50", 50), ("junk_bond_demand", 93.4),
                       ("safe_haven_demand", 72.4)):
        data[key] = {"timestamp": NOW * 1000, "score": value, "rating": "neutral", "data": []}
    return data


def gauge(score=44.0, market="Stocks", source="CNN", **kw):
    return Gauge(market, source, score, NOW, **kw)


# ----- reading CNN -----

def test_cnn_answer_is_read():
    g = parse_cnn(cnn_payload(), now=NOW)
    assert (g.market, g.source, g.score, g.label) == ("Stocks", "CNN", 43.9, "Fear")
    assert (g.close, g.week, g.month, g.year) == (47.2, 30.26, 45.23, 51.86)
    assert g.at == 1791388800.0 and g.official
    assert len(g.history) == 253 and g.history[-1] == (NOW, 43.9)
    assert all(a[0] < b[0] for a, b in zip(g.history, g.history[1:]))
    assert [p[0] for p in g.parts] == ["Market momentum", "Stock price strength", "Stock price breadth",
                                       "Put and call options", "Market volatility", "Safe haven demand",
                                       "Junk bond demand"]
    assert g.parts[-1][2] == 93.4


def test_cnn_junk_is_skipped_not_believed():
    data = cnn_payload()
    data["fear_and_greed"].update(previous_close="47", previous_1_week=float("nan"), previous_1_month=140,
                                  previous_1_year=True, timestamp="yesterday")
    data["fear_and_greed_historical"]["data"] += [{"x": "soon", "y": 50}, {"x": NOW * 1000, "y": None},
                                                  {"x": (NOW + 30 * DAY) * 1000, "y": 50}, "junk",
                                                  {"x": -5, "y": 50}]
    data["put_call_options"] = {"score": "30"}
    data["junk_bond_demand"] = None
    g = parse_cnn(data, now=NOW)
    assert (g.close, g.week, g.month, g.year) == (None, None, None, None)
    assert g.at == NOW  # an unreadable timestamp falls back to now
    assert len(g.history) == 253 and max(t for t, _ in g.history) <= NOW
    assert [p[0] for p in g.parts] == ["Market momentum", "Stock price strength", "Stock price breadth",
                                       "Market volatility", "Safe haven demand"]


@pytest.mark.parametrize("data", [None, [], {}, {"fear_and_greed": None}, {"fear_and_greed": {"score": None}},
                                  {"fear_and_greed": {"score": "44"}}, {"fear_and_greed": {"score": 101}},
                                  {"fear_and_greed": {"score": -1}}, {"fear_and_greed": {"score": float("inf")}}])
def test_cnn_without_a_reading_is_refused(data):
    with pytest.raises(ValueError):
        parse_cnn(data, now=NOW)


def test_a_future_timestamp_is_capped_at_now():
    data = cnn_payload()
    data["fear_and_greed"]["timestamp"] = "2030-01-01T00:00:00Z"
    assert parse_cnn(data, now=NOW).at == NOW


# ----- the crypto index -----

def test_crypto_readings_back_a_day_week_month_and_year():
    times = [NOW - (400 - i) * DAY for i in range(401)]
    values = [float(i % 100) for i in range(401)]
    g = crypto_gauge(times, values)
    assert (g.market, g.source, g.score) == ("Crypto", "alternative.me", 0.0)
    assert (g.close, g.week, g.month, g.year) == (99.0, 93.0, 70.0, 35.0)
    assert len(g.history) == 367 and g.history[-1] == (NOW, 0.0)


def test_crypto_gaps_give_no_reading_rather_than_a_wrong_one():
    times = [NOW - 40 * DAY, NOW - 3 * DAY, NOW]
    g = crypto_gauge(times, [20.0, 30.0, 40.0])
    assert (g.close, g.week, g.month, g.year) == (None, None, None, None)


def test_crypto_junk_values_are_skipped():
    g = crypto_gauge([NOW - DAY, NOW, NOW + 1], [50.0, float("nan"), None])
    assert g.score == 50.0 and g.at == NOW - DAY
    assert crypto_gauge([], []) is None and crypto_gauge([NOW], [float("nan")]) is None


def test_the_estimate_stands_in_but_says_so():
    g = estimate_gauge(38.0, {"Momentum": 0.4, "VIX": 0.36, "Broken": float("nan")}, NOW)
    assert (g.source, g.score, g.label, g.official) == ("bot's estimate", 38.0, "Fear", False)
    assert [(n, round(v)) for n, _, v in g.parts] == [("Momentum", 40), ("VIX", 36)]
    assert estimate_gauge(None, {}, NOW) is None and estimate_gauge(float("nan"), {}, NOW) is None


# ----- zones -----

@pytest.mark.parametrize("value, expected", [(0, 0), (24.9, 0), (25, 1), (44.9, 1), (45, 2), (55, 2), (55.1, 3),
                                             (75, 3), (75.1, 4), (100, 4)])
def test_zones_follow_the_labels(value, expected):
    assert zone(value) == expected


@pytest.mark.parametrize("before, value, after", [
    (None, 10, None),  # nothing to compare with yet
    (1, 24, None), (1, 23.1, None), (1, 22.9, 0),  # into extreme fear only 2 points past the edge
    (1, 46, None), (1, 47.1, 2),  # into neutral
    (2, 56, None), (2, 57.5, 3), (3, 54, None), (3, 52.9, 2),
    (3, 76, None), (3, 77.5, 4), (4, 74, None), (4, 72.9, 3),
    (1, 80, 4), (4, 10, 0),  # jumps over zones
    (2, 50, None), (0, 0, None), (4, 100, None),
])
def test_moves_need_a_clear_crossing(before, value, after):
    assert moved_zone(before, value) == after


# ----- fetching CNN -----

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
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _no_sleep(_):
    pass


def ok(score=43.9):
    return Response(200, json.dumps(cnn_payload(score)))


def test_cnn_is_asked_like_a_browser_and_cached():
    backend = Backend(ok(43.9), ok(60.0))
    clock = Clock()
    cnn = CNNFearGreed(Http(backend, sleep=_no_sleep), clock, market_open=lambda: True)
    assert asyncio.run(cnn.get()).score == 43.9
    url, headers = backend.calls[0]
    assert url == fgm.CNN_URL and headers["Referer"] == fgm.CNN_PAGE
    clock.t += fgm.CNN_TTL - 1
    assert asyncio.run(cnn.get()).score == 43.9 and len(backend.calls) == 1
    clock.t += 2
    assert asyncio.run(cnn.get()).score == 60.0 and len(backend.calls) == 2


def test_cnn_down_keeps_a_recent_reading_then_gives_up():
    backend = Backend(ok(43.9), Response(418, "I'm a teapot"))
    clock = Clock()
    http = Http(backend, sleep=_no_sleep)
    cnn = CNNFearGreed(http, clock, market_open=lambda: False)
    assert asyncio.run(cnn.get()).score == 43.9
    clock.t += fgm.CNN_TTL + 1
    assert asyncio.run(cnn.get()).score == 43.9  # a reading from minutes ago beats none
    assert http.health["CNN"].failing and "418" in http.health["CNN"].last_error
    calls = len(backend.calls)
    clock.t += fgm.CNN_RETRY - 1
    asyncio.run(cnn.get())
    assert len(backend.calls) == calls  # not asked again straight away
    clock.t += 6 * 3600
    assert asyncio.run(cnn.get()) is None  # hours old: no longer shown as CNN's reading


@pytest.mark.parametrize("answer, error", [
    (Response(200, "<html>blocked</html>"), "unreadable answer"),
    (Response(200, json.dumps({"fear_and_greed": {"score": "n/a"}})), "unreadable answer"),
    (Response(403, "Forbidden"), "HTTP 403"),
    (Response(503, "down"), "HTTP 503"),
    (OSError("connection reset"), "OSError"),
])
def test_cnn_failures_are_recorded_and_return_nothing(answer, error):
    http = Http(Backend(answer), sleep=_no_sleep)
    assert asyncio.run(CNNFearGreed(http, Clock(), market_open=lambda: True).get()) is None
    assert http.health["CNN"].failing and error in http.health["CNN"].last_error


def test_while_the_market_is_open_an_old_cnn_reading_gives_way_to_the_estimate():
    backend = Backend(ok(43.9), Response(403, "Forbidden"))
    clock = Clock()
    cnn = CNNFearGreed(Http(backend, sleep=_no_sleep), clock, market_open=lambda: True)
    asyncio.run(cnn.get())
    clock.t += fgm.KEEP_OPEN - 60
    assert asyncio.run(cnn.get()).score == 43.9
    clock.t += fgm.CNN_RETRY + 61
    assert asyncio.run(cnn.get()) is None  # half an hour of a frozen number is enough while it's moving


@pytest.mark.parametrize("value, stocks, crypto", [
    (25, "Fear", "Extreme Fear"), (45, "Neutral", "Fear"), (46, "Neutral", "Fear"), (47, "Neutral", "Neutral"),
    (54, "Neutral", "Neutral"), (55, "Neutral", "Greed"), (75, "Greed", "Greed"), (76, "Extreme Greed", "Extreme Greed"),
])
def test_each_index_uses_its_publishers_zones(value, stocks, crypto):
    assert gauge(value).label == stocks
    assert gauge(value, "Crypto", "alternative.me").label == crypto
    assert E.fear_greed_label(value, crypto=True) == crypto


def test_crypto_zone_moves_use_alternative_me_edges():
    assert moved_zone(3, 45, "Crypto") == 1  # 45 is Fear for alternative.me, two points past its 47-54 Neutral
    assert moved_zone(3, 45, "Stocks") == 2
    assert moved_zone(1, 48, "Crypto") is None and moved_zone(1, 49, "Crypto") == 2


# ----- alerts in the trends channel -----

class FakeEngine:
    def __init__(self):
        self.data = SimpleNamespace(quotes=None)
        self.models = {}

    async def close(self):
        pass


def make_bot(tmp_path):
    from marketbot.ai import NewsAI
    from marketbot.bot import MarketBot
    bot = MarketBot(tmp_path, engine=FakeEngine(), ai=NewsAI(readers=[]))
    bot.sent = []

    async def send(cid, post):
        bot.sent.append((cid, post))
        return SimpleNamespace(id=1)

    bot.send = send
    bot.channels.set(5, "trends", 9)
    bot.channels.set(6, "trends", 9)
    bot.channels.update(6, alerts=False)
    bot.channels.set(1, "stocks", 9)
    return bot


def macro_with(stock=None, crypto=None):
    return SimpleNamespace(stock_fg=stock, crypto_fg=crypto, mood=None)


def test_zone_alerts_post_once_per_clear_move(tmp_path):
    bot = make_bot(tmp_path)
    for score, posts in ((44, 0), (23.5, 0), (22.0, 1), (21.0, 1), (24.0, 1), (27.5, 2), (40, 2), (47.5, 3)):
        bot.macro_cache = macro_with(gauge(score))
        asyncio.run(bot.fear_greed_alerts())
        assert len(bot.sent) == posts, score
    assert {cid for cid, _ in bot.sent} == {5}  # the trends channel with alerts on only
    titles = [p.embeds[0].title for _, p in bot.sent]
    assert titles == ["😱 Stocks Fear & Greed is now Extreme Fear", "😟 Stocks Fear & Greed is now Fear",
                      "😐 Stocks Fear & Greed is now Neutral"]
    assert "▼ from Fear" in bot.sent[0][1].embeds[0].description


def test_each_index_has_its_own_zone(tmp_path):
    bot = make_bot(tmp_path)
    bot.macro_cache = macro_with(gauge(50), gauge(50, "Crypto", "alternative.me"))
    asyncio.run(bot.fear_greed_alerts())
    bot.macro_cache = macro_with(gauge(50), gauge(80, "Crypto", "alternative.me"))
    asyncio.run(bot.fear_greed_alerts())
    assert [p.embeds[0].title for _, p in bot.sent] == ["🤑 Crypto Fear & Greed is now Extreme Greed"]
    assert bot.state.get("fear_greed_zone", "stocks") == 2 and bot.state.get("fear_greed_zone", "crypto") == 4


def test_the_estimate_never_alerts(tmp_path):
    bot = make_bot(tmp_path)
    bot.macro_cache = macro_with(gauge(50))
    asyncio.run(bot.fear_greed_alerts())
    bot.macro_cache = macro_with(gauge(5, source="bot's estimate"))
    asyncio.run(bot.fear_greed_alerts())
    assert not bot.sent and bot.state.get("fear_greed_zone", "stocks") == 2


@pytest.mark.parametrize("stored", ["Fear", 7, -1, True, 2.5, [1]])
def test_a_broken_stored_zone_is_reset_without_posting(tmp_path, stored):
    bot = make_bot(tmp_path)
    bot.state.set("fear_greed_zone", "stocks", stored)
    bot.macro_cache = macro_with(gauge(10))
    asyncio.run(bot.fear_greed_alerts())
    assert not bot.sent and bot.state.get("fear_greed_zone", "stocks") == 0


def test_no_macro_yet_means_no_alerts(tmp_path):
    bot = make_bot(tmp_path)
    bot.macro_cache = None
    asyncio.run(bot.fear_greed_alerts())
    assert not bot.sent


# ----- where it shows -----

def full_stock_gauge():
    return parse_cnn(cnn_payload(), now=NOW)


def test_feargreed_embed_with_both_indexes():
    crypto = crypto_gauge([NOW - (400 - i) * DAY for i in range(401)], [float(i % 100) for i in range(401)])
    e = E.fear_greed_embed(full_stock_gauge(), crypto, {"n": 503, "up": 0.52, "median": 0.01, "all_up": 0.54})
    names = [f.name for f in e.fields]
    assert names == ["📈 US stocks · CNN", "What's driving it", "🪙 Crypto · alternative.me"]
    assert "**44 Fear** 😟" in e.fields[0].value and "Previous close **47**" in e.fields[0].value
    assert e.fields[1].value.count("\n") == 6 and "Junk bond demand" in e.fields[1].value
    assert "Yesterday **99**" in e.fields[2].value and "52% of 503 days" in e.fields[2].value
    assert e.title == "😟 Fear & Greed" and len(e) <= 6000


def test_feargreed_embed_with_the_estimate_or_nothing():
    est = estimate_gauge(30.0, {"Momentum (S&P vs 125-day avg)": 0.3}, NOW)
    e = E.fear_greed_embed(est, None)
    assert e.fields[0].name == "📈 US stocks · bot's estimate" and "CNN isn't answering" in e.fields[0].value
    empty = E.fear_greed_embed(None, None)
    assert not empty.fields and "Neither index" in empty.description


def test_boards_show_cnn_or_say_it_is_an_estimate():
    from marketbot.universe import FUTURES, INDICES, MACRO
    board = E.stocks_board({}, INDICES, FUTURES, MACRO, [], 61.0, NOW, full_stock_gauge())
    assert "Fear & Greed: **44 Fear** 😟 (CNN · prev close 47)" in board.description
    board = E.stocks_board({}, INDICES, FUTURES, MACRO, [], 61.0, NOW, estimate_gauge(61.0, {}, NOW))
    assert "Market mood: **61/100 Greed** (bot's estimate)" in board.description
    board = E.stocks_board({}, INDICES, FUTURES, MACRO, [], None, NOW)
    assert "mood" not in board.description.lower()


def test_trends_line():
    crypto = gauge(71, "Crypto", "alternative.me")
    assert E.fear_greed_line(gauge(44), crypto) == "Fear & Greed: stocks **44 Fear** 😟 · crypto **71 Greed** 😀"
    assert E.fear_greed_line(gauge(44, source="bot's estimate"), None) == \
        "Fear & Greed: stocks **44 Fear** 😟 (estimate)"
    assert E.fear_greed_line(None, None) == ""


def test_macro_dashboard_prefers_cnn():
    from marketbot.engine import Macro
    m = Macro({}, None, None, 61.0, {"Momentum": 0.6}, 71.0, 70.0, None, None, full_stock_gauge(), None)
    e = E.macro_embed(m, None)
    assert e.fields[0].name == "Stock market Fear & Greed (CNN)" and "(week ago 30)" in e.fields[0].value
    assert "Market momentum: 58" in e.fields[0].value and "/feargreed" in e.footer.text
    m.stock_fg = estimate_gauge(61.0, {"Momentum": 0.6}, NOW)
    assert E.macro_embed(m, None).fields[0].name == "Stock market mood"


def test_the_chart_draws_with_one_or_both_series():
    from marketbot import charts
    stocks = full_stock_gauge().history
    crypto = crypto_gauge([NOW - (400 - i) * DAY for i in range(401)], [float(i % 100) for i in range(401)]).history
    for s, c in ((stocks, crypto), (stocks, []), ([], crypto), (stocks[:2], crypto[:1])):
        png = charts.fear_greed_chart(s, c, "Fear & Greed · the past year", "sub")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_premarket_brief_shows_cnn():
    from marketbot import briefs

    async def quotes(symbols):
        return {}

    bot = SimpleNamespace(engine=SimpleNamespace(data=SimpleNamespace(quotes=quotes)), quotes={},
                          macro_cache=SimpleNamespace(stock_fg=full_stock_gauge()), recent_news=[])

    async def no_outlooks(*a, **k):
        return []

    orig = briefs._outlooks
    briefs._outlooks = no_outlooks
    try:
        posts = asyncio.run(briefs.premarket(bot, []))
    finally:
        briefs._outlooks = orig
    field = next(f for f in posts[0].embeds[0].fields if f.name == "Fear & Greed")
    assert field.value == "**44 Fear** 😟 (CNN · prev close 47 · week ago 30)"
