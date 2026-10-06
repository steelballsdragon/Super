import asyncio
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("matplotlib")

from marketbot import bot as botmod, embeds as E  # noqa: E402
from marketbot.ai import NewsAI  # noqa: E402
from marketbot.bot import MarketBot, code_version, crossed, data_folder, live_seconds, move_steps, on_railway  # noqa: E402
from marketbot.channels import ChannelStore  # noqa: E402
from marketbot.engine import Engine, compute_outlook, options_view, scan_all, with_live  # noqa: E402
from marketbot.feeds import Headline  # noqa: E402
from marketbot.hours import NEW_YORK, holidays, is_trading_day, market_open  # noqa: E402
from marketbot.news import analyse  # noqa: E402
from marketbot.record import PredictionBook  # noqa: E402
from marketbot.universe import CRYPTO, STOCKS, market_of, normalize, short, tag  # noqa: E402
from marketbot.storage import StateStore  # noqa: E402
from tests.market_helpers import DAY, from_closes, quote, walk  # noqa: E402


# ----- symbols, calendar, channels -----

def test_symbols_from_what_people_type():
    assert normalize("btc") == "BTC-USD" and normalize("$nvda") == "NVDA" and normalize("s&p") == "^GSPC"
    assert normalize("ethusdt") == "ETH-USD" and normalize("gold") == "GC=F" and normalize("sol/usd") == "SOL-USD"
    assert market_of("BTC-USD") == CRYPTO and market_of("AAPL") == STOCKS and market_of("X", "CRYPTOCURRENCY") == CRYPTO
    assert short("SUI20947-USD") == "SUI" and tag("^IXIC") == "Nasdaq" and tag("AAPL") == "AAPL"


def test_nyse_holidays():
    assert date(2026, 4, 3) in holidays(2026)  # Good Friday
    assert date(2026, 11, 26) in holidays(2026)  # Thanksgiving
    assert date(2026, 7, 3) in holidays(2026)  # July 4th falls on a Saturday
    assert date(2027, 12, 31) not in holidays(2027)  # a Saturday New Year's Day isn't observed in December
    assert not is_trading_day(date(2026, 10, 10)) and is_trading_day(date(2026, 10, 6))
    assert market_open(datetime(2026, 10, 6, 10, 0, tzinfo=NEW_YORK))
    assert not market_open(datetime(2026, 10, 6, 16, 30, tzinfo=NEW_YORK))


def test_channel_store_round_trip(tmp_path: Path):
    store = ChannelStore(tmp_path / "c.json")
    store.set(1, "stocks", 9)
    store.update(1, watchlist=("AAPL", "TSLA"), alerts=False)
    store.set(2, "news")
    again = ChannelStore(tmp_path / "c.json")
    assert again.get(1).symbols() == ["AAPL", "TSLA"] and again.get(1).alerts is False
    assert again.get(2).symbols() == [] and len(again.of_kind("news")) == 1
    again.set(1, "crypto")  # a new kind starts from that market's list
    assert again.get(1).symbols()[0] == "BTC-USD"
    assert again.remove(2) and not again.remove(2)


# ----- the prediction record -----

def test_forecasts_and_breakouts_are_graded(tmp_path: Path):
    book = PredictionBook(StateStore(tmp_path / "r.json"))
    t0 = 1_700_000_000
    book.add_forecast("AAA", STOCKS, 100.0, {5: 0.6, 20: 0.4, 60: 0.7}, {5: 0.55, 20: 0.58}, "Bullish", now=t0)
    book.add_forecast("AAA", STOCKS, 100.0, {5: 0.6}, {5: 0.55}, "Bullish", now=t0 + 60)  # same day: ignored
    assert book.add_breakout("AAA", STOCKS, 1, 105.0, 100.0, 0.4, 0.3, "coil_resistance", now=t0)
    assert not book.add_breakout("AAA", STOCKS, 1, 106.0, 100.0, 0.4, 0.3, "coil_resistance", now=t0 + 10)
    bars = from_closes([100 + i for i in range(40)], symbol="AAA", start=t0 - 5 * DAY)
    assert book.grade({"AAA": bars}, now=t0 + 3 * DAY) == 1  # the breakout cleared 105 early
    assert book.grade({"AAA": bars}, now=t0 + 30 * DAY) == 2  # both forecasts are due by now
    s = book.summary()
    assert s["forecasts"][5]["n"] == 1 and s["forecasts"][5]["hit"] == 1.0  # said 60% up, it rose
    assert s["forecasts"][20]["hit"] == 0.0  # said 40%, it rose
    assert s["breakouts"]["n"] == 1 and s["breakouts"]["hit"] == 1.0
    assert book.pending_symbols() == set()


# ----- engine pieces -----

def test_live_quote_becomes_todays_bar():
    b = walk(300)
    later = with_live(b, quote(price=123.0, t=float(b.t[-1]) + DAY + 3600))
    assert len(later) == len(b) + 1 and later.close[-1] == 123.0
    same = with_live(b, quote(price=124.0, t=float(b.t[-1]) + 3600))
    assert len(same) == len(b) and same.close[-1] == 124.0 and b.close[-1] != 124.0
    assert with_live(b, quote(t=float(b.t[-1]) - 5 * DAY)) is b


def test_options_view_off_hours_uses_volume_and_straddle():
    calls = [{"strike": k, "bid": 0, "ask": 0, "lastPrice": max(100 - k, 0) + 2, "impliedVolatility": 1e-5,
              "openInterest": 0, "volume": 10 if k != 110 else 500} for k in (90, 100, 110)]
    puts = [{"strike": k, "bid": 0, "ask": 0, "lastPrice": max(k - 100, 0) + 2, "impliedVolatility": 1e-5,
             "openInterest": 0, "volume": 10 if k != 90 else 300} for k in (90, 100, 110)]
    now = 1_700_000_000
    ov = options_view(calls, puts, 100.0, now + 30 * DAY, now, 0.25)
    assert ov.atm_strike == 100 and ov.expected_move == 4
    assert 0.1 < ov.atm_iv < 0.3  # from the straddle, since Yahoo's IV is blank
    assert ov.call_wall == 110 and ov.put_wall == 90 and ov.put_call_oi is None


def test_outlook_without_a_model_still_forecasts():
    b = walk(2500, symbol="AAA")
    o = compute_outlook("AAA", "Test", STOCKS, b, [walk(2500, seed=5, symbol="REF")], None, None, None)
    assert set(o.up) == {5, 20, 60} and all(0 < p < 1 for p in o.up.values())
    assert -100 <= o.score <= 100 and o.label and not o.model_ready and o.cone is not None
    e = E.outlook_embed(o)
    assert len(e) <= 6000 and any("Chance of being higher" in f.name for f in e.fields)


def test_outlook_and_scan_with_a_model():
    from marketbot.model import train
    model = train(STOCKS, [walk(3000, seed=s, symbol=f"S{s}") for s in range(2)])
    b = walk(1500, seed=7, symbol="AAA")
    o = compute_outlook("AAA", "Test", STOCKS, b, [], None, model, None)
    assert o.model_ready and 0 <= o.breakout_up <= 1 and o.drivers
    hits = scan_all([("AAA", b, None), ("BBB", walk(900, seed=8, symbol="BBB"), None)], {STOCKS: model}, 1)
    assert {h.symbol for h in hits} == {"AAA", "BBB"}
    assert hits == sorted(hits, key=lambda h: -h.pressure)
    assert len(E.scan_embed(hits, STOCKS)) <= 6000


# ----- the bot (Discord faked out) -----

class FakeYahoo:
    def __init__(self, quotes):
        self._quotes = quotes

    async def quotes(self, symbols):
        return {s: q for s, q in self._quotes.items() if s in symbols}

    async def close(self):
        pass


class FakeEngine:
    def __init__(self, quotes):
        self.yahoo = FakeYahoo(quotes)
        self.models = {}

    async def close(self):
        pass


def make_bot(tmp_path, quotes=None):
    bot = MarketBot(tmp_path, engine=FakeEngine(quotes or {}), ai=NewsAI(api_key=""))
    bot.sent = []

    async def send(cid, post):
        bot.sent.append((cid, post))
        return SimpleNamespace(id=1)

    async def show_board(cid, embed):
        bot.sent.append((cid, "board"))

    bot.send = send
    bot.show_board = show_board
    return bot


def test_commands_are_registered(tmp_path):
    from marketbot.commands import register_commands
    bot = make_bot(tmp_path)
    register_commands(bot)
    names = {c.name for c in bot.tree.get_commands()}
    assert {"setup", "channel", "settings", "watchlist", "price", "chart", "forecast", "research", "breakouts",
            "news", "history", "macro", "movers", "compare", "backtest", "alert", "alerts", "record", "brief",
            "status", "update", "help"} <= names


def test_move_alert_lines():
    assert move_steps("^GSPC", STOCKS)[0] == 1.0 and move_steps("NVDA", STOCKS)[0] == 3.0
    assert move_steps("BTC-USD", CRYPTO)[0] == 5.0
    assert crossed(5.5, (3, 5, 7.5)) == 5 and crossed(-2.0, (3, 5)) == 0
    assert live_seconds(None) == 60 and live_seconds("5") == 30


def test_big_moves_alert_once_per_line(tmp_path):
    q = quote("NVDA", price=110, change=5.6)
    bot = make_bot(tmp_path, {"NVDA": q})
    bot.channels.set(1, STOCKS)
    bot.channels.update(1, watchlist=("NVDA",))
    bot.quotes = {"NVDA": q}
    asyncio.run(bot.check_moves())
    asyncio.run(bot.check_moves())
    assert len(bot.sent) == 1 and "5.6%" in bot.sent[0][1].embeds[0].title
    bot.quotes["NVDA"] = quote("NVDA", price=112, change=8.0)
    asyncio.run(bot.check_moves())
    assert len(bot.sent) == 2  # crossed the next line (7.5%)
    bot.quotes["NVDA"] = quote("NVDA", price=112, change=8.0, state="PRE")
    bot.channels.update(1, watchlist=("NVDA",))
    asyncio.run(bot.check_moves())
    assert len(bot.sent) == 2  # pre-market quotes still show yesterday's change: no alert


def test_crypto_fast_move_alert(tmp_path):
    bot = make_bot(tmp_path)
    bot.channels.set(2, CRYPTO)
    bot.channels.update(2, watchlist=("BTC-USD",))
    now = time.time()
    from collections import deque
    bot.trail["BTC-USD"] = deque([(now - 3600, 100.0), (now - 1800, 101.0), (now, 103.0)])
    bot.quotes = {"BTC-USD": quote("BTC-USD", price=103.0, change=1.0, quote_type="CRYPTOCURRENCY")}
    asyncio.run(bot.check_moves())
    titles = [p.embeds[0].title for _, p in bot.sent]
    assert any("last hour" in t for t in titles)


def test_price_alerts_fire_once(tmp_path):
    bot = make_bot(tmp_path)
    bot.state.set("price_alerts", "a1", {"channel": 5, "user": 42, "symbol": "AAPL", "target": 200.0, "above": True})
    bot.quotes = {"AAPL": quote("AAPL", price=199.0)}
    asyncio.run(bot.check_price_alerts())
    assert not bot.sent
    bot.quotes = {"AAPL": quote("AAPL", price=201.0)}
    asyncio.run(bot.check_price_alerts())
    asyncio.run(bot.check_price_alerts())
    assert len(bot.sent) == 1 and bot.sent[0][1].content == "<@42>"


def test_news_desk_posts_by_level_and_records_calls(tmp_path):
    bot = make_bot(tmp_path, {s: quote(s, price=100.0) for s in ("^GSPC", "^IXIC", "^RUT", "^TNX", "DX-Y.NYB", "GC=F",
                                                                "BTC-USD", "ETH-USD")})
    bot.channels.set(3, "news")
    bot.channels.set(4, "news")
    bot.channels.update(4, news_level="major")
    now = time.time()
    stories = [Headline(f"id{i}", t, "", "https://x.test", "Src", now - 60, "stocks") for i, t in enumerate([
        "US CPI rises more than expected in September, core inflation accelerates",
        "Retailer opens a new store downtown",
        "Is Apple Stock a Buy Now?",
    ])]

    class News:
        async def latest(self):
            return stories

        async def close(self):
            pass

    bot.news = News()
    bot._first_news = False
    asyncio.run(bot.job_news())
    posted = [(cid, p.embeds[0].title) for cid, p in bot.sent]
    assert any(cid == 3 and "CPI" in title for cid, title in posted)
    assert any(cid == 4 and "CPI" in title for cid, title in posted)
    assert not any("Retailer" in t or "Apple" in t for _, t in posted)
    assert bot.impacts.summary()["pending"] >= 3
    bot.sent.clear()
    asyncio.run(bot.job_news())  # already seen: nothing new
    assert not bot.sent


def test_first_run_does_not_flood_the_news_channel(tmp_path):
    bot = make_bot(tmp_path, {"^GSPC": quote("^GSPC")})
    bot.channels.set(3, "news")
    bot.channels.update(3, news_level="all")
    now = time.time()
    stories = [Headline(f"id{i}", f"Fed cuts rates by {i} basis points", "", "", "S", now - 60, "stocks") for i in range(10)]
    bot.news = SimpleNamespace(latest=lambda: asyncio.sleep(0, stories), close=lambda: asyncio.sleep(0))
    asyncio.run(bot.job_news())
    assert len(bot.sent) <= 3


def test_briefs_post_once_per_day(tmp_path):
    bot = make_bot(tmp_path)
    ny = datetime(2026, 10, 6, 9, 5, tzinfo=NEW_YORK)
    assert bot._due(1, "premarket", ny, 9, 0, 75)
    assert not bot._due(1, "premarket", ny, 9, 0, 75)
    assert not bot._due(1, "close", ny, 16, 10)  # not yet
    late = datetime(2026, 10, 6, 12, 0, tzinfo=NEW_YORK)
    assert not bot._due(2, "premarket", late, 9, 0, 75)  # missed the window: skipped, not posted late


def test_scheduler_runs_jobs_and_records_failures(tmp_path):
    bot = make_bot(tmp_path)
    ran = []

    async def ok():
        ran.append("ok")

    async def boom():
        raise RuntimeError("source down")

    async def run():
        await bot._run("ok", ok)
        await bot._run("boom", boom)
    asyncio.run(run())
    assert ran == ["ok"] and bot.health["ok"].last_ok and "source down" in bot.health["boom"].last_error


def test_prune_drops_old_entries(tmp_path):
    bot = make_bot(tmp_path)
    old = time.time() - 10 * 86400
    bot.state.set("news_seen", "x", old)
    bot.state.set("moves", "1|AAPL|2020-01-01", 3.0)
    bot.state.set("setup_alerts", "1|AAPL|ath", old - 40 * 86400)
    bot.state.set("news_seen", "y", time.time())
    asyncio.run(bot.job_prune())
    assert bot.state.items("news_seen") == [("y", bot.state.get("news_seen", "y"))]
    assert not bot.state.items("moves") and not bot.state.items("setup_alerts")


def test_boards_and_embeds_fit_discord_limits():
    from marketbot.universe import DEFAULT_CRYPTO, DEFAULT_STOCKS, FUTURES, INDICES, MACRO
    quotes = {s: quote(s, price=123.45, change=-1.2, state="PRE", ext_price=124.0, ext_change_pct=0.4)
              for s in DEFAULT_STOCKS + [a.symbol for a in INDICES + FUTURES + MACRO]}
    e = E.stocks_board(quotes, INDICES, FUTURES, MACRO, DEFAULT_STOCKS * 2, 55.0, time.time())
    assert len(e) <= 6000 and all(len(f.value) <= 1024 for f in e.fields)
    cq = {s: quote(s, price=0.000123, change=12.0, quote_type="CRYPTOCURRENCY") for s in DEFAULT_CRYPTO}
    c = E.crypto_board(cq, DEFAULT_CRYPTO * 2, None, 12.0, {}, time.time())
    assert len(c) <= 6000
    a = analyse(Headline("x", "Fed cuts rates " + "very " * 80, "word " * 200, "https://x.test/" + "a" * 500, "S",
                         time.time(), "stocks"))
    n = E.news_embed(a)
    assert len(n) <= 6000 and len(n.title) <= 256


def test_update_command_matches_the_installer():
    script = (Path(__file__).resolve().parent.parent / "deploy" / "marketbot-system-setup.sh").read_text()
    assert "marketbot-update.service" in script
    assert botmod.UPDATE_COMMAND[-1] == "marketbot-update.service"


def test_engine_reads_saved_models(tmp_path):
    from marketbot.model import train
    eng = Engine(tmp_path)
    eng.models[STOCKS] = train(STOCKS, [walk(2500, seed=1, symbol="S1")])
    eng._save_models()
    again = Engine(tmp_path)
    assert STOCKS in again.models and again.models_stale()  # crypto still missing


def test_data_folder_setting():
    assert data_folder({}) == "market-data"
    assert data_folder({"MARKET_DATA_DIR": "/data/m"}) == "/data/m"
    # A server first set up for ScoreBot keeps using its writable data folder.
    assert data_folder({"DATA_FILE": "/var/lib/scorebot/subscriptions.json"}) == "/var/lib/scorebot"
    assert data_folder({"DATA_FILE": "/x/s.json", "MARKET_DATA_DIR": "/y"}) == "/y"
    # On Railway, an attached volume is found without any setting.
    assert data_folder({"RAILWAY_VOLUME_MOUNT_PATH": "/data"}) == "/data"
    assert data_folder({"RAILWAY_VOLUME_MOUNT_PATH": "/data", "MARKET_DATA_DIR": "/y"}) == "/y"


def test_railway_detection_and_version(monkeypatch):
    assert on_railway({"RAILWAY_PROJECT_ID": "p"}) and not on_railway({})
    monkeypatch.setattr(botmod.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=""))
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "0123456789abcdef")
    assert code_version() == "0123456"


def test_sigterm_closes_the_bot(tmp_path):
    import os
    import signal
    bot = make_bot(tmp_path)
    closed = []

    async def close():
        closed.append(True)

    bot.close = close

    async def run():
        bot.stop_on_sigterm()
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(50):
            if closed:
                break
            await asyncio.sleep(0.01)
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
    asyncio.run(run())
    assert closed == [True]
