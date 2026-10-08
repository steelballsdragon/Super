"""/why (explaining a move), the Finnhub client and the CoinGecko key."""

import asyncio
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("numpy")

from marketbot.addons import why as W  # noqa: E402
from marketbot.apis.finnhub import Finnhub  # noqa: E402
from marketbot.feeds import Headline  # noqa: E402
from marketbot.http import Http, Response  # noqa: E402
from marketbot.news import analyse  # noqa: E402
from marketbot.yahoo import Bars  # noqa: E402
from tests.market_helpers import DAY, quote  # noqa: E402

T0 = 1_700_000_000


def bars_from_returns(r, symbol):
    close = 100 * np.cumprod(1 + np.concatenate([[0.0], r]))
    t = T0 + np.arange(len(close), dtype=np.int64) * DAY
    return Bars(symbol, t, close, close, close, close, np.full(len(close), 1e6))


def market_sector_stock(n=300, b_m=1.3, b_s=0.8, seed=3):
    rng = np.random.default_rng(seed)
    m = rng.normal(0, 0.01, n)
    sec_extra = rng.normal(0, 0.006, n)
    x = m + sec_extra
    noise = rng.normal(0, 0.002, n)
    s = b_m * m + b_s * (x - m) + noise
    return bars_from_returns(s, "NVDA"), bars_from_returns(m, "^GSPC"), bars_from_returns(x, "SMH")


# ----- the maths -----

def test_betas_recover_the_true_sensitivities():
    stock, market, sector = market_sector_stock()
    b_m, b_s = W.fit_betas(stock, market, sector)
    assert b_m == pytest.approx(1.3, abs=0.05) and b_s == pytest.approx(0.8, abs=0.08)
    b_only, none = W.fit_betas(stock, market)
    assert b_only == pytest.approx(1.3, abs=0.1) and none is None


def test_betas_need_enough_common_days():
    stock, market, sector = market_sector_stock(n=40)
    assert W.fit_betas(stock, market, sector) == (None, None)


def test_betas_only_use_days_both_traded():
    stock, market, _ = market_sector_stock()
    shifted = Bars("^GSPC", market.t + 10_000 * DAY, market.open, market.high, market.low, market.close, market.volume)
    assert W.fit_betas(stock, shifted) == (None, None)


def test_betas_survive_junk_prices():
    stock, market, _ = market_sector_stock()
    stock.close[50] = np.nan
    b_m, _ = W.fit_betas(stock, market)
    assert b_m is not None and np.isfinite(b_m)


def test_decompose_adds_up():
    p = W.decompose(-4.0, -1.0, -2.5, 1.2, 0.9)
    assert p["market"] == pytest.approx(-1.2) and p["sector"] == pytest.approx(-1.35)
    assert sum(p.values()) == pytest.approx(-4.0)
    assert W.decompose(-4.0, -1.0, None, None, None) == {"market": -1.0, "own": -3.0}
    assert W.decompose(2.0, None, None, None, None) == {"own": 2.0}


# ----- the words -----

def why(**kw):
    base = dict(symbol="NVDA", name="NVIDIA", market="stocks", move=-4.0, session="today", benchmark="^GSPC",
                benchmark_move=-1.0, sector="SMH", sector_move=-2.5, market_beta=1.2, sector_beta=0.9)
    base.update(kw)
    w = W.Why(**base)
    if "parts" not in kw:
        w.parts = W.decompose(w.move, w.benchmark_move, w.sector_move, w.market_beta, w.sector_beta)
    return w


def test_template_blames_the_market_when_it_explains_most():
    w = why(move=-1.3, sector_move=-1.1, parts=None)
    w.parts = W.decompose(-1.3, -1.0, -1.1, 1.2, 0.9)
    text, driver = W.template_summary(w)
    assert driver == "market" and "mostly moving with the market" in text and "The S&P 500 (-1.0%)" in text


def test_template_names_the_headline_for_a_company_move():
    w = why(move=-8.0)
    w.headlines = [W.Headline("US widens chip export ban to more countries", "Reuters", time.time() - 3600)]
    text, driver = W.template_summary(w)
    assert driver == "company news" and "export ban" in text and "remaining" in text


def test_template_prefers_earnings_and_admits_when_unclear():
    w = why(move=9.0)
    w.events = ["Earnings reported <t:1:R>: EPS beat estimates by 12.0%"]
    assert W.template_summary(w)[1] == "earnings"
    assert W.template_summary(why(move=9.0))[1] == "unclear"


def test_template_for_an_index_lists_sectors():
    w = W.Why("^GSPC", "S&P 500", "stocks", 1.2, "today", sectors=[("XLK", 2.1), ("XLE", -0.8)])
    text, _ = W.template_summary(w)
    assert "Leading: Tech (+2.1%)" in text and "lagging: Energy (-0.8%)" in text


def test_embed_fits_discord_with_long_everything():
    w = why(move=-8.0, volume_ratio=2.34, ext_move=-1.1)
    w.headlines = [W.Headline("x" * 400, "Reuters", time.time(), "https://x.test/" + "a" * 300)] * 8
    w.events = ["e" * 900] * 5
    w.summary = "s" * 5000
    e = W.why_embed(w)
    assert len(e) <= 6000 and all(len(f.value) <= 1024 for f in e.fields)
    assert e.title == "🤔 Why is NVIDIA (NVDA) down 8.0% today?" or e.title.startswith("🤔 Why is")
    w.events = ["Earnings due <t:1:R>"]
    assert "Volume 2.3× its 3-month average" in W.why_embed(w).fields[-1].value


# ----- gathering the facts (bot faked) -----

class FakeData:
    def __init__(self, quotes, summary=None):
        self._quotes = quotes
        self._summary = summary or {}
        self.http = Http(SimpleNamespace(name="x", get=None, close=None))

    async def quotes(self, symbols):
        return {s: q for s, q in self._quotes.items() if s in symbols}

    async def summary(self, symbol, modules):
        if isinstance(self._summary, Exception):
            raise self._summary
        return self._summary


class FakeCache:
    def __init__(self, bars):
        self.bars = bars

    async def daily(self, symbol):
        if symbol not in self.bars:
            raise LookupError(symbol)
        return self.bars[symbol]


class FakeAI:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []
        self.enabled = answer is not None

    async def complete(self, system, prompt, schema, name="answer", wait=5.0):
        self.asked.append(json.loads(prompt))
        return self.answer

    def statuses(self):
        return [SimpleNamespace(name="Groq", last_ok=time.time(), last_error=None)]


def make_desk(tmp_path, quotes, bars, summary=None, ai_answer=None, news=()):
    async def run(fn, *a):
        return fn(*a)

    engine = SimpleNamespace(data=FakeData(quotes, summary), cache=FakeCache(bars), run=run)
    bot = SimpleNamespace(engine=engine, data_dir=tmp_path, recent_news=list(news), ai=FakeAI(ai_answer))
    desk = W.WhyDesk(bot)
    desk.finnhub = None
    return desk, bot


def test_explain_a_stock_end_to_end(tmp_path):
    stock, market, sector = market_sector_stock()
    quotes = {"NVDA": quote("NVDA", 180, -4.0, volume=4e8, avg_volume=2e8),
              "^GSPC": quote("^GSPC", 6000, -1.0), "SMH": quote("SMH", 300, -2.5)}
    news = [analyse(Headline("n1", "Nvidia falls after US widens chip export curbs", "", "https://x.test/1",
                             "Reuters", time.time() - 1800, "stocks", 1.0, ("NVDA",)))]
    desk, bot = make_desk(tmp_path, quotes, {"NVDA": stock, "^GSPC": market, "SMH": sector},
                          summary={"assetProfile": {"sector": "Technology", "industry": "Semiconductors"}},
                          ai_answer={"summary": "Nvidia fell on new export curbs, more than chips overall.",
                                     "driver": "company news", "confidence": "Medium"}, news=news)
    w = asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks"))
    assert w.sector == "SMH" and w.market_beta == pytest.approx(1.3, abs=0.05)
    assert sum(w.parts.values()) == pytest.approx(-4.0)
    assert w.volume_ratio == pytest.approx(2.0) and w.headlines[0].title.startswith("Nvidia falls")
    assert w.summary.startswith("Nvidia fell") and w.by == "Groq" and w.driver == "company news"
    facts = bot.ai.asked[0]
    assert facts["symbol"] == "NVDA" and facts["sector"] == "Semiconductors" and facts["headlines"]


def test_explain_without_ai_or_profile_uses_the_template(tmp_path):
    stock, market, _ = market_sector_stock()
    quotes = {"NVDA": quote("NVDA", 180, -4.0), "^GSPC": quote("^GSPC", 6000, -1.0)}
    desk, _ = make_desk(tmp_path, quotes, {"NVDA": stock, "^GSPC": market}, summary=RuntimeError("yahoo down"))
    w = asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks"))
    assert w.sector == "" and "sector" not in w.parts and w.by == ""
    assert w.summary.startswith("NVIDIA is down 4.0% today.")


def test_explain_a_coin_against_bitcoin(tmp_path):
    coin, btc, _ = market_sector_stock()
    quotes = {"SOL-USD": quote("SOL-USD", 150, 6.0), "BTC-USD": quote("BTC-USD", 100000, 2.0)}
    desk, _ = make_desk(tmp_path, quotes, {"SOL-USD": coin, "BTC-USD": btc})
    w = asyncio.run(desk.explain("SOL-USD", "Solana", "crypto"))
    assert w.session == "in 24h" and w.benchmark == "BTC-USD" and w.volume_ratio is None
    assert "Bitcoin (+2.0%)" in w.summary
    assert W.why_embed(w).fields[0].value.startswith("`Bitcoin ")


def test_explain_an_index_by_its_sectors(tmp_path):
    quotes = {"^GSPC": quote("^GSPC", 6000, 1.2), **{s: quote(s, 100, v) for s, v in
                                                       (("XLK", 2.0), ("XLE", -1.0), ("XLF", 0.5))}}
    desk, _ = make_desk(tmp_path, quotes, {})
    w = asyncio.run(desk.explain("^GSPC", "S&P 500", "stocks"))
    assert [s for s, _ in w.sectors] == ["XLK", "XLF", "XLE"] and not w.parts
    assert W.why_embed(w).fields[0].name == "Sectors"


def test_explain_gives_up_without_a_price(tmp_path):
    desk, _ = make_desk(tmp_path, {"^GSPC": quote("^GSPC", 6000, -1.0)}, {})
    assert asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks")) is None
    desk, _ = make_desk(tmp_path, {"NVDA": quote("NVDA", 100, float("nan"))}, {})
    assert asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks")) is None


def test_after_hours_says_last_session_and_shows_the_extended_move(tmp_path):
    stock, market, _ = market_sector_stock()
    quotes = {"NVDA": quote("NVDA", 180, -4.0, state="POST", ext_price=175, ext_change_pct=-2.8),
              "^GSPC": quote("^GSPC", 6000, -1.0, state="POST")}
    desk, _ = make_desk(tmp_path, quotes, {"NVDA": stock, "^GSPC": market}, summary={})
    w = asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks"))
    assert w.session == "last session" and w.ext_move == -2.8


def test_a_missing_history_still_explains_with_beta_one(tmp_path):
    quotes = {"NVDA": quote("NVDA", 180, -4.0), "^GSPC": quote("^GSPC", 6000, -1.0)}
    desk, _ = make_desk(tmp_path, quotes, {}, summary={})
    w = asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks"))
    assert w.market_beta is None and w.parts == {"market": -1.0, "own": -3.0}


# ----- Finnhub -----

class Backend:
    name = "fake"

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, headers, timeout, proxy):
        self.calls.append((url, headers))
        for needle, answer in self.routes.items():
            if needle in url:
                return answer
        return Response(404, "{}")

    async def close(self):
        pass


async def _no_sleep(_):
    pass


def test_finnhub_reads_free_endpoints_with_the_key_in_a_header(tmp_path):
    now = int(time.time())
    backend = Backend({
        "company-news": Response(200, json.dumps([{"datetime": now - 100, "headline": "Old"},
                                                  {"datetime": now, "headline": "New", "source": "CNBC"}, "junk"])),
        "stock/recommendation": Response(200, json.dumps([{"period": "2026-09-01", "buy": 1},
                                                          {"period": "2026-10-01", "buy": 3}])),
        "stock/earnings": Response(200, json.dumps([{"period": "2026-06-30", "surprisePercent": 4.2}])),
        "calendar/earnings": Response(200, json.dumps({"earningsCalendar": [{"symbol": "AAPL", "date": "2026-10-30"}]})),
        "insider-transactions": Response(200, json.dumps({"data": [{"name": "X", "transactionCode": "P"}]})),
        "profile2": Response(200, json.dumps({"finnhubIndustry": "Semiconductors"})),
    })
    f = Finnhub(Http(backend, sleep=_no_sleep), key="fh-key", state_file=tmp_path / "apis.json")
    assert [n["headline"] for n in asyncio.run(f.company_news("NVDA"))] == ["New", "Old"]
    assert backend.calls[0][1]["X-Finnhub-Token"] == "fh-key" and "fh-key" not in backend.calls[0][0]
    assert asyncio.run(f.recommendation("NVDA"))[0]["period"] == "2026-10-01"
    assert asyncio.run(f.earnings("NVDA"))[0]["surprisePercent"] == 4.2
    assert asyncio.run(f.profile("NVDA"))["finnhubIndustry"] == "Semiconductors"
    from datetime import date
    assert asyncio.run(f.earnings_calendar(date(2026, 10, 1), date(2026, 10, 31)))[0]["symbol"] == "AAPL"
    assert asyncio.run(f.insider_transactions("NVDA"))[0]["transactionCode"] == "P"
    calls = len(backend.calls)
    asyncio.run(f.company_news("NVDA"))
    assert len(backend.calls) == calls  # cached


def test_finnhub_odd_answers_become_empty_lists(tmp_path):
    backend = Backend({"": Response(200, json.dumps({"error": "weird"}))})
    f = Finnhub(Http(backend, sleep=_no_sleep), key="k")
    assert asyncio.run(f.company_news("X")) == [] and asyncio.run(f.recommendation("X")) == []
    assert asyncio.run(f.insider_transactions("X")) == [] and asyncio.run(f.profile("X")) == {"error": "weird"}


def test_finnhub_without_a_key_is_off():
    f = Finnhub(Http(Backend({}), sleep=_no_sleep), key="")
    assert not f.enabled and f.status_line() == "no key"


# ----- the CoinGecko key -----

class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body
        self.request_info = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body)

    def raise_for_status(self):
        if self.status >= 400:
            import aiohttp
            raise aiohttp.ClientResponseError(None, (), status=self.status)


class FakeSession:
    closed = False

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_coingecko_key_is_sent_within_the_daily_budget(tmp_path, monkeypatch):
    from marketbot import sources as S
    session = FakeSession(FakeResp(200, '{"ok": 1}'))
    src = S.Sources(session, cg_key="CG-demo", state_file=tmp_path / "apis.json")
    assert asyncio.run(src._get(f"{S.COINGECKO}/global")) == {"ok": 1}
    assert session.calls[0][1] == {S.CG_HEADER: "CG-demo"}
    asyncio.run(src._get("https://api.alternative.me/fng/"))
    assert session.calls[1][1] == {}  # other services never see it
    src.cg_budget.calls = 1  # budget used up: keyless
    asyncio.run(src._get(f"{S.COINGECKO}/global"))
    assert session.calls[2][1] == {} and "1/1" not in src.coingecko_line()
    again = S.Sources(FakeSession(FakeResp(200, "{}")), cg_key="CG-demo", state_file=tmp_path / "apis.json")
    assert again.cg_budget.used() == 1  # remembered across a restart


def test_a_refused_coingecko_key_is_dropped_and_the_call_retried_keyless():
    from marketbot import sources as S
    session = FakeSession(FakeResp(401, '{"status": {"error_code": 10002, "error_message": "Invalid key"}}'),
                          FakeResp(200, '{"ok": 2}'))
    src = S.Sources(session, cg_key="bad")
    assert asyncio.run(src._get(f"{S.COINGECKO}/global")) == {"ok": 2}
    assert src.cg_key_rejected and session.calls[1][1] == {} and "rejected" in src.coingecko_line()


def test_a_plan_error_keeps_the_key():
    from marketbot import sources as S
    session = FakeSession(FakeResp(401, '{"status": {"error_code": 10005, "error_message": "Pro only"}}'))
    src = S.Sources(session, cg_key="good")
    import aiohttp
    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(src._get(f"{S.COINGECKO}/coins/x/history"))
    assert not src.cg_key_rejected


def test_no_coingecko_key_is_fine():
    from marketbot import sources as S
    src = S.Sources(FakeSession(FakeResp(200, "{}")), cg_key="")
    asyncio.run(src._get(f"{S.COINGECKO}/global"))
    assert "no key" in src.coingecko_line()


# ----- review fixes: real mentions only, honest earnings, the right news window -----

@pytest.mark.parametrize("name, title, hit", [
    ("Bank of America Corporation", "Bank of Japan raises rates for the first time in a year", False),
    ("Bank of America Corporation", "Bank of America beats on trading revenue", True),
    ("Advanced Micro Devices, Inc.", "Advanced Energy lifts guidance", False),
    ("Advanced Micro Devices, Inc.", "Advanced Micro Devices unveils new chips", True),
    ("Target Corporation", "Goldman raises Nvidia price target to $250", False),
    ("Strategy Inc Class A", "Investors rethink their strategy as yields climb", False),
    ("The Graph", "Ether slips as the dollar firms", False),
    ("NVIDIA Corporation", "Nvidia falls after US widens chip export curbs", True),
    ("Apple Inc.", "Apple's iPhone sales top estimates", True),
    ("The Trade Desk, Inc.", "Trade war fears hit stocks", False),
    ("The Trade Desk, Inc.", "The Trade Desk slumps on weak guidance", True),
    ("Meta Platforms, Inc.", "Meta to cut 5% of staff", True),
    ("NEAR Protocol", "Bitcoin trades near $120,000", False),
])
def test_company_names_match_only_real_mentions(name, title, hit):
    pat = W.name_pattern(name)
    assert (pat is not None and pat.search(title) is not None) is hit


@pytest.mark.parametrize("tick, title, hit", [
    ("LOW", "Treasury yields fall to a three-month low", False), ("LOW", "Lowe's ($LOW) cuts forecast", True),
    ("ALL", "Stocks hit an all-time high", False), ("NOW", "Traders now see two cuts", False),
    ("NVDA", "NVDA slides as export curbs widen", True), ("NVDA", "nvda", False), ("TRUMP", "Trump says tariffs", False),
    ("BAC", "BAC upgraded at Barclays", True),
])
def test_tickers_match_in_capitals_or_as_cashtags(tick, title, hit):
    assert W.ticker_hit(title, tick) is hit


def test_bank_of_america_is_not_blamed_on_the_bank_of_japan(tmp_path):
    stock, market, _ = market_sector_stock()
    quotes = {"BAC": quote("BAC", 40, -5.0), "^GSPC": quote("^GSPC", 6000, -0.2)}
    news = [analyse(Headline("n1", "Bank of Japan raises rates for the first time in a year", "", "u", "Reuters",
                             time.time() - 600, "macro", 1.0, ()))]
    desk, _ = make_desk(tmp_path, quotes, {"BAC": stock, "^GSPC": market}, summary={}, news=news)
    w = asyncio.run(desk.explain("BAC", "Bank of America Corporation", "stocks"))
    assert not w.headlines and "Bank of Japan" not in w.summary and w.driver == "unclear"


def test_upcoming_earnings_are_context_not_the_cause(tmp_path):
    stock, market, _ = market_sector_stock()
    q = quote("NVDA", 180, -8.0)
    q.extra["earningsTimestamp"] = time.time() + 36 * 3600
    news = [analyse(Headline("n1", "Nvidia falls after US widens chip export curbs", "", "u", "Reuters",
                             time.time() - 1800, "stocks", 1.0, ("NVDA",)))]
    desk, _ = make_desk(tmp_path, {"NVDA": q, "^GSPC": quote("^GSPC", 6000, -0.5)},
                        {"NVDA": stock, "^GSPC": market}, summary={}, news=news)
    w = asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks"))
    assert w.driver == "company news" and "export curbs" in w.summary and "Earnings due" in w.summary
    assert w.summary.index("export curbs") < w.summary.index("Earnings due")


def test_an_old_report_isnt_todays_reason(tmp_path):
    stock, market, _ = market_sector_stock()
    q = quote("NVDA", 180, -8.0)
    q.extra["earningsTimestamp"] = time.time() - 6 * 86400
    desk, _ = make_desk(tmp_path, {"NVDA": q, "^GSPC": quote("^GSPC", 6000, -0.5)},
                        {"NVDA": stock, "^GSPC": market}, summary={})
    w = asyncio.run(desk.explain("NVDA", "NVIDIA", "stocks"))
    assert not any(e.startswith("Earnings") for e in w.events) and w.driver == "unclear"


def test_the_surprise_comes_from_that_reports_row(tmp_path):
    desk, _ = make_desk(tmp_path, {}, {})
    t = time.time() - 3600
    from datetime import datetime
    day = datetime.fromtimestamp(t, W.NEW_YORK).date().isoformat()

    class FH:
        enabled = True

        async def earnings_calendar(self, start, end, symbol=""):
            return [{"symbol": "NVDA", "date": day, "epsActual": 2.2, "epsEstimate": 2.0}]

    desk.finnhub = FH()
    assert asyncio.run(desk.report_surprise("NVDA", t)) == pytest.approx(10.0)

    class NotYet(FH):
        async def earnings_calendar(self, start, end, symbol=""):
            return [{"symbol": "NVDA", "date": day, "epsActual": None, "epsEstimate": 2.0}]

    desk.finnhub = NotYet()
    assert asyncio.run(desk.report_surprise("NVDA", t)) is None


def test_weekend_view_keeps_fridays_headlines(tmp_path):
    friday_close = time.time() - 40 * 3600
    news = [analyse(Headline("n1", "Nvidia falls after US widens chip export curbs", "", "u", "Reuters",
                             friday_close - 5 * 3600, "stocks", 1.0, ("NVDA",)))]
    desk, _ = make_desk(tmp_path, {}, {}, news=news)
    found = asyncio.run(desk.headlines("NVDA", "NVIDIA", "stocks", False, session_end=friday_close))
    assert found and found[0].title.startswith("Nvidia falls")
    assert not asyncio.run(desk.headlines("NVDA", "NVIDIA", "stocks", False))  # measured from now: too old


def test_a_failed_profile_lookup_is_retried_soon(tmp_path):
    desk, bot = make_desk(tmp_path, {}, {}, summary=RuntimeError("yahoo down"))
    assert asyncio.run(desk.sector_of("NVDA")) == ""
    expires = desk._sector["NVDA"][0]
    assert expires - time.monotonic() < 1000  # a quarter of an hour, not a week
    bot.engine.data._summary = {"assetProfile": {"sector": "Technology", "industry": "Semiconductors"}}
    desk._sector["NVDA"] = (time.monotonic() - 1, "")
    assert asyncio.run(desk.sector_of("NVDA")) == "SMH"


def test_a_move_against_the_market_isnt_called_the_markets():
    w = why(move=0.1, sector_move=None, sector="", parts=None)
    w.parts = W.decompose(0.1, -0.3, None, 1.0, None)
    text, driver = W.template_summary(w)
    assert "mostly moving with the market" not in text and driver == "unclear" and "-0.0" not in text
