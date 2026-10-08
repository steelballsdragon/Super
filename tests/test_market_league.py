"""The league: paper trades (fills, cost basis, orders waiting for the open), prediction calls (graded when due,
stock calls made after hours starting at the open), the standings and the weekly post, and the commands."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from marketbot.addons import league as L  # noqa: E402
from marketbot.hours import market_open as real_open  # noqa: E402
from marketbot.yahoo import Quote  # noqa: E402
from tests.live_rehearsal import Interaction  # noqa: E402
from tests.test_market_bot import make_bot  # noqa: E402


def q(symbol, price):
    return Quote(symbol, symbol, price, price, 0.0)


def test_buys_and_sells_keep_the_cost_basis():
    p = L.new_player("Ann", 5000.0)
    assert L.fill(p, {"side": "buy", "symbol": "NVDA", "dollars": 1000.0}, 100.0) == \
        "Bought 10 NVDA at $100.00 ($1,000.00)"
    L.fill(p, {"side": "buy", "symbol": "NVDA", "dollars": 2000.0}, 200.0)
    assert p["positions"]["NVDA"] == {"shares": 20.0, "cost": 3000.0} and p["cash"] == 97_000
    assert L.fill(p, {"side": "sell", "symbol": "NVDA", "shares": 5.0}, 300.0).startswith("Sold 5 NVDA")
    assert p["positions"]["NVDA"]["cost"] == pytest.approx(2250.0) and p["cash"] == 98_500
    L.fill(p, {"side": "sell", "symbol": "NVDA", "shares": None}, 300.0)  # all
    assert p["positions"] == {} and p["cash"] == 103_000
    assert L.fill(p, {"side": "sell", "symbol": "NVDA", "shares": None}, 300.0) is None
    assert L.fill(p, {"side": "buy", "symbol": "BTC-USD", "dollars": 1e9}, 50_000.0).startswith("Bought 2.06 BTC")
    assert p["cash"] == 0 and L.fill(p, {"side": "buy", "symbol": "X", "dollars": 10.0}, 1.0) is None


def test_standings_and_callers():
    a, b = L.new_player("Ann", 5000.0), L.new_player("Bob", 5000.0)
    L.fill(a, {"side": "buy", "symbol": "NVDA", "dollars": 50_000.0}, 100.0)
    rows = L.standings({"1": a, "2": b}, {"NVDA": 120.0}, 5500.0)
    assert [(r["name"], round(r["ret"], 2), round(r["bench"], 2)) for r in rows] == [("Ann", 0.1, 0.1),
                                                                                     ("Bob", 0.0, 0.1)]
    calls = [{"uid": "1", "name": "Ann", "result": r} for r in (0.02, -0.01, 0.03)] + \
            [{"uid": "2", "name": "Bob", "result": 0.05}, {"uid": "2", "name": "Bob", "result": None}]
    assert [(c["name"], c["wins"], c["n"]) for c in L.callers(calls)] == [("Ann", 2, 3)]  # Bob has 1 graded
    e = L.league_embed(rows, calls)
    assert "🥇 **Ann** +10.00%" in e.fields[0].value and "S&P +10.00% since joining" in e.fields[0].value
    assert "2/3 right (67%)" in e.fields[1].value
    assert "Nobody's playing yet" in L.league_embed([], []).description


def test_call_return_and_tradable():
    assert L.call_return({"price": 100.0, "direction": "up"}, 105.0) == pytest.approx(0.05)
    assert L.call_return({"price": 100.0, "direction": "down"}, 105.0) == pytest.approx(-0.05)
    assert L.tradable("NVDA") and L.tradable("BTC-USD")
    assert not any(L.tradable(s) for s in ("^GSPC", "GC=F", "EURUSD=X"))
    sunday = datetime(2026, 10, 11, 12, 0, tzinfo=L.NEW_YORK)
    assert L.fills_now("BTC-USD", sunday) and not L.fills_now("NVDA", sunday)


def make_league(tmp_path, prices):
    from marketbot.channels import ChannelStore
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    async def quotes(symbols):
        return {s: q(s, prices[s]) for s in symbols if s in prices}

    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(7, "league")
    due = set()

    def _due(cid, kind, local, hour, minute, window_min=120):
        key = (cid, kind, local.date())
        start = local.replace(hour=hour, minute=minute)
        if key in due or not start <= local:
            return False
        due.add(key)
        return True

    bot = SimpleNamespace(engine=SimpleNamespace(data=SimpleNamespace(quotes=quotes)), data_dir=tmp_path,
                          channels=channels, send=send, _due=_due)
    return L.League(bot), sent


def at(monkeypatch, *when):
    class Clock(L.datetime):
        @classmethod
        def now(cls, tz=None):
            return L.datetime(*when, tzinfo=tz)

    monkeypatch.setattr(L, "datetime", Clock)
    monkeypatch.setattr(L, "market_open", lambda now=None: real_open(now or Clock.now(L.NEW_YORK)))


def test_waiting_orders_and_calls_start_at_the_open_then_get_graded(tmp_path, monkeypatch):
    prices = {"NVDA": 100.0, "^GSPC": 5000.0}
    league, sent = make_league(tmp_path, prices)
    p = L.new_player("Ann", 5000.0)
    p["orders"] = [{"side": "buy", "symbol": "NVDA", "dollars": 1000.0}]
    league.players["1"] = p
    league.calls = [{"id": 1, "uid": "1", "name": "Ann", "symbol": "NVDA", "direction": "up", "horizon": "1d",
                     "at": L.time.time(), "price": None, "due": 0, "result": None}]
    at(monkeypatch, 2026, 10, 11, 12, 0)  # Sunday: nothing fills
    asyncio.run(league.job())
    assert p["orders"] and league.calls[0]["price"] is None and not sent
    at(monkeypatch, 2026, 10, 12, 9, 31)  # Monday's open: the first quotes may still be Friday's close
    asyncio.run(league.job())
    assert p["orders"] and league.calls[0]["price"] is None
    at(monkeypatch, 2026, 10, 12, 9, 35)
    asyncio.run(league.job())
    assert not p["orders"] and p["positions"]["NVDA"]["shares"] == 10
    c = league.calls[0]
    assert c["price"] == 100.0 and c["due"] > L.time.time() + 86000 and not sent
    c["due"] = 0
    prices["NVDA"] = 97.0
    asyncio.run(league.job())
    [(cid, post)] = sent
    assert cid == 7 and post.embeds[0].title == "❌ Ann: NVDA up over a day" and c["result"] == pytest.approx(-0.03)
    saved = L.read_json(tmp_path / "league.json", {})
    assert saved["calls"][0]["result"] == pytest.approx(-0.03) and saved["players"]["1"]["positions"]


def test_weekly_standings_after_the_weeks_last_close(tmp_path, monkeypatch):
    league, sent = make_league(tmp_path, {"^GSPC": 5000.0})
    league.players["1"] = L.new_player("Ann", 5000.0)
    at(monkeypatch, 2026, 10, 8, 16, 45)  # a Thursday
    asyncio.run(league.job())
    assert not sent
    at(monkeypatch, 2026, 10, 9, 16, 45)  # Friday
    asyncio.run(league.job())
    asyncio.run(league.job())
    assert [p.embeds[0].title for _, p in sent] == ["🏆 This week's league standings"]


def command(bot, name, sub=None):
    cmd = next(c for c in bot.tree.get_commands() if c.name == name)
    return cmd.get_command(sub) if sub else cmd


def test_commands(tmp_path, monkeypatch):
    from marketbot.commands import register_commands
    bot = make_bot(tmp_path, {"NVDA": q("NVDA", 100.0), "BTC-USD": q("BTC-USD", 50_000.0), "^GSPC": q("^GSPC", 5e3)})
    register_commands(bot)
    league = next(f for f in bot.features if f.name == "league")

    async def resolve(interaction, text):
        return SimpleNamespace(symbol=text, name=text, market="stocks")

    bot.resolve_symbol = resolve
    user = SimpleNamespace(id=42, display_name="Ann", mention="<@42>")
    monkeypatch.setattr(L, "market_open", lambda now=None: True)

    def run(cmd, **kw):
        it = Interaction(1)
        it.user = user
        asyncio.run(cmd.callback(it, **kw))
        content, extra = it.out[-1]
        return content or extra["embed"]

    assert run(command(bot, "paper", "buy"), symbol="NVDA", dollars=2000.0).startswith("✅ Bought 20 NVDA")
    assert run(command(bot, "paper", "buy"), symbol="^GSPC", dollars=10.0).startswith("**^GSPC** is an index")
    assert run(command(bot, "paper", "buy"), symbol="NVDA", dollars=1e6) == "You have $98,000.00 in cash."
    assert "don't hold any BTC" in run(command(bot, "paper", "sell"), symbol="BTC-USD", shares=None)
    e = run(command(bot, "paper", "portfolio"), user=None)
    assert e.title == "💼 Ann's paper portfolio" and "**NVDA** 20 sh · $2,000.00 (+0.0%)" in e.fields[0].value
    monkeypatch.setattr(L, "market_open", lambda now=None: False)
    assert "fills at the next open" in run(command(bot, "paper", "sell"), symbol="NVDA", shares=5.0)
    assert league.players["42"]["orders"] == [{"side": "sell", "symbol": "NVDA", "shares": 5.0}]
    up = SimpleNamespace(name="1 week", value="1w")
    assert "starting from the next market open" in run(command(bot, "call"), symbol="NVDA",
                                                       direction=SimpleNamespace(value="up"), horizon=up)
    assert "from $50,000.00, graded <t:" in run(command(bot, "call"), symbol="BTC-USD",
                                                direction=SimpleNamespace(value="down"), horizon=up)
    e = run(command(bot, "league"))
    assert e.title == "🏆 League standings" and "**Ann** -0.00%" in e.fields[0].value or "**Ann** +0.00%" in \
        e.fields[0].value
    assert L.read_json(tmp_path / "league.json", {})["calls"][1]["price"] == 50_000.0
