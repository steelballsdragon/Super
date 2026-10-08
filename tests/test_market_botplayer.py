"""🤖 MarketBot as a league player: its rules, buying once per day (even across a restart), sitting out on stale
quotes or an untested model, the two-check safety stop, sizing from $5 to $10M, and the admin commands."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("numpy")

from marketbot.addons import botplayer as B  # noqa: E402
from marketbot.addons import league as L  # noqa: E402
from marketbot.engine import ScanHit  # noqa: E402
from marketbot.hours import market_open as real_open  # noqa: E402
from marketbot.yahoo import Quote  # noqa: E402
from tests.live_rehearsal import Interaction  # noqa: E402
from tests.test_market_bot import make_bot  # noqa: E402


def hit(sym, p_up, p_down=0.1, atr=2.0, pressure=0.0):
    return ScanHit(sym, sym, L.market_of(sym), 100.0, 0.0, [], p_up, p_down, 0.39, 0.25, 110.0, 90.0, pressure, atr)


def bot_player(markets="both", amount=10.0):
    return B.new_bot(amount, markets, 5000.0, 1, {"uid": "42", "name": "Ann", "guild": 9})


def test_tilt_and_decide():
    p = bot_player("stocks")
    hits = [hit("NVDA", 0.80, 0.05), hit("AMD", 0.70, 0.08), hit("WEAK", 0.55, 0.02), hit("FLAT", 0.62, 0.50),
            hit("AAPL", 0.65, 0.05), hit("MSFT", 0.66, 0.05), hit("TSLA", 0.75, 0.05), hit("META", 0.61, 0.05)]
    assert B.tilt(hits[0]) == pytest.approx(0.41 + 0.20)
    sells, renew, buys = B.decide(p, B.STOCKS, hits, "2026-10-12")
    assert not sells and not renew and [h.symbol for h in buys] == ["NVDA", "TSLA", "AMD", "MSFT", "AAPL"]
    p["auto"]["cooldown"] = {"TSLA": "2026-10-15"}
    assert "TSLA" not in [h.symbol for h in B.decide(p, B.STOCKS, hits, "2026-10-12")[2]]
    p["positions"] = {s: {"shares": 1, "cost": 1, "due": d} for s, d in
                      (("FLAT", "2026-12-01"), ("NVDA", "2026-10-01"), ("META", "2026-10-01"), ("GONE", "2026-10-01"))}
    sells, renew, buys = B.decide(p, B.STOCKS, hits, "2026-10-12")
    assert [s for s, _ in sells] == ["FLAT", "META"] and "faded" in sells[0][1] and "no longer a top pick" in sells[1][1]
    assert renew == ["NVDA"] and len(buys) == 5 - 4 + 2  # GONE (no fresh odds) is held


def test_helpers():
    from marketbot import features
    assert B.HOLD_DAYS == features.BREAKOUT_DAYS
    assert B.due_after(date(2026, 10, 9), B.STOCKS).isoformat() == "2026-10-23"
    assert B.due_after(date(2026, 10, 9), B.CRYPTO).isoformat() == "2026-10-19"
    skill = {"breakout_up": SimpleNamespace(auc=0.8), "breakout_down": SimpleNamespace(auc=0.75)}
    assert B.model_ready({B.STOCKS: SimpleNamespace(skill=skill)}, B.STOCKS)
    assert not B.model_ready({B.STOCKS: SimpleNamespace(skill={**skill, "breakout_up": SimpleNamespace(auc=0.6)})},
                             B.STOCKS)
    assert not B.model_ready({}, B.CRYPTO)
    now = 1_800_000_000
    assert B.fresh(Quote("X", "X", 10, 10, 0, time=now - 60, market_state="REGULAR"), B.STOCKS, now)
    assert not B.fresh(Quote("X", "X", 10, 10, 0, time=now - 60, market_state="POST"), B.STOCKS, now)
    assert not B.fresh(Quote("X", "X", 10, 10, 0, time=now - 3600), B.STOCKS, now)
    assert B.fresh(Quote("X", "X", 10, 10, 0, time=now - 1000, market_state="POST"), B.CRYPTO, now)


def test_scan_hits_carry_the_daily_range():
    from marketbot.engine import scan_all
    from marketbot.yahoo import Bars
    n = 300
    c = 100 + np.cumsum(np.random.default_rng(3).normal(0, 1, n))
    bars = Bars("AAPL", 1.6e9 + np.arange(n) * 86400.0, c, c + 1, c - 1, c, np.ones(n) * 1e6)
    [h] = scan_all([("AAPL", bars, None)], {}, 1)
    assert h.atr == pytest.approx(2.0, rel=0.2)


# ----- the bot's turns, with a fake engine and clock -----

class Market:
    def __init__(self, prices, hits, auc=0.8):
        self.prices, self.hits, self.auc = prices, hits, auc
        self.age, self.state, self.scans, self.quote_calls = 0, "REGULAR", [], []
        self.now = None

    async def quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        t = self.now().timestamp() - self.age
        return {s: Quote(s, s, self.prices[s], self.prices[s], 0.0, time=t,
                         market_state="" if s.endswith("-USD") else self.state) for s in symbols if s in self.prices}

    async def scan(self, symbols, quotes, lookback=1):
        self.scans.append(sorted(symbols))
        return [h for h in self.hits if h.symbol in symbols]

    @property
    def models(self):
        skill = {"breakout_up": SimpleNamespace(auc=self.auc), "breakout_down": SimpleNamespace(auc=self.auc)}
        return {m: SimpleNamespace(skill=skill) for m in (B.STOCKS, B.CRYPTO)}


def setup(tmp_path, monkeypatch, markets="both", amount=10.0, hits=None):
    from marketbot.channels import ChannelStore
    prices = {s: 100.0 for s in B.DEFAULT_STOCKS + list(B.SECTORS) + ["SPY", "QQQ", "IWM", "DIA"]}
    prices.update({s: 1000.0 for s in B.DEFAULT_CRYPTO})
    prices["^GSPC"] = 5000.0
    hits = hits if hits is not None else [hit("NVDA", 0.8), hit("AMD", 0.75), hit("AAPL", 0.7), hit("MSFT", 0.68),
                                          hit("TSLA", 0.66), hit("BTC-USD", 0.7), hit("ETH-USD", 0.65)]
    m = Market(prices, hits)
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(7, "league")
    bot = SimpleNamespace(engine=SimpleNamespace(data=m, scan=m.scan, models=m.models), data_dir=tmp_path,
                          channels=channels, send=send, _due=lambda *a, **k: False)
    league = L.League(bot)
    league.players[B.BOT_ID] = bot_player(markets, amount)
    league.save()

    def at(*when):
        class Clock(L.datetime):
            @classmethod
            def now(cls, tz=None):
                return L.datetime(*when, tzinfo=tz)
        monkeypatch.setattr(L, "datetime", Clock)
        monkeypatch.setattr(L, "market_open", lambda now=None: real_open(now or Clock.now(L.NEW_YORK)))
        m.now = lambda: Clock.now(L.NEW_YORK)

    return league, m, sent, at, bot


def test_buys_once_a_day_even_across_a_restart(tmp_path, monkeypatch):
    league, m, sent, at, bot = setup(tmp_path, monkeypatch)
    at(2026, 10, 12, 15, 45)  # Monday
    asyncio.run(league.job())
    p = league.players[B.BOT_ID]
    assert sorted(p["positions"]) == ["AAPL", "AMD", "BTC-USD", "MSFT", "NVDA"] and p["cash"] == pytest.approx(0)
    [(cid, post)] = sent
    e = post.embeds[0]
    assert cid == 7 and e.title == "🤖 MarketBot traded" and e.description.count("📈 Bought $2.00 of") == 5
    assert "breakout odds 80% (usual 39%), breakdown 10% (usual 25%)" in e.description
    nv = p["positions"]["NVDA"]
    assert nv["due"] == "2026-10-26" and nv["stop"] == pytest.approx(92.0)  # 3 x $2 ATR is 6%: the 8% floor
    assert p["auto"]["runs"] == {"stocks": "2026-10-12", "crypto": "2026-10-12"}
    assert p["auto"]["last"]["note"] in ("bought BTC", "bought NVDA, AMD, AAPL, MSFT")
    at(2026, 10, 12, 15, 50)
    asyncio.run(league.job())
    again = L.League(bot)  # a restart reads the saved markers
    at(2026, 10, 12, 15, 55)
    asyncio.run(again.job())
    assert len(m.scans) == 2 and len(sent) == 1


def test_only_inside_the_window_and_stocks_only_on_trading_days(tmp_path, monkeypatch):
    league, m, sent, at, _ = setup(tmp_path, monkeypatch)
    for when in ((2026, 10, 12, 10, 0), (2026, 10, 12, 16, 5)):
        at(*when)
        asyncio.run(league.job())
    assert not m.scans
    at(2026, 10, 11, 15, 45)  # Sunday: coins only
    asyncio.run(league.job())
    assert len(m.scans) == 1 and all(s.endswith("-USD") for s in m.scans[0])
    assert sorted(league.players[B.BOT_ID]["positions"]) == ["BTC-USD"]


def test_sits_out_on_stale_quotes_and_on_an_untested_model(tmp_path, monkeypatch):
    league, m, sent, at, _ = setup(tmp_path, monkeypatch, markets="stocks")
    m.state = "POST"
    at(2026, 10, 12, 15, 45)
    asyncio.run(league.job())
    p = league.players[B.BOT_ID]
    assert not p["positions"] and p["auto"]["runs"] == {} and not m.scans  # not marked: it tries again
    m.state = "REGULAR"
    at(2026, 10, 12, 15, 50)
    asyncio.run(league.job())
    assert len(p["positions"]) == 5
    league2, m2, _, at2, _ = setup(tmp_path / "b", monkeypatch, markets="stocks")
    m2.auc = 0.6
    league2.bot.engine.models = m2.models
    at2(2026, 10, 12, 15, 45)
    asyncio.run(league2.job())
    p2 = league2.players[B.BOT_ID]
    assert not p2["positions"] and p2["auto"]["runs"] == {"stocks": "2026-10-12"} and not m2.scans
    assert p2["auto"]["last"]["note"] == "model not ready: holding"


def test_two_check_safety_stop(tmp_path, monkeypatch):
    league, m, sent, at, _ = setup(tmp_path, monkeypatch, markets="stocks")
    p = league.players[B.BOT_ID]
    L.fill(p, {"side": "buy", "symbol": "NVDA", "dollars": 10.0}, 100.0)
    p["cash"] = 0.0
    p["positions"]["NVDA"].update(entry=100.0, at=0, stop=90.0, stop_hit=None, due="2026-12-01", why="x")
    m.prices["NVDA"] = 85.0
    at(2026, 10, 13, 9, 31)  # before 9:35 the quotes may still be yesterday's close
    asyncio.run(league.job())
    assert not m.quote_calls
    at(2026, 10, 13, 10, 0)
    asyncio.run(league.job())
    assert p["positions"]["NVDA"]["stop_hit"] and not sent
    at(2026, 10, 13, 10, 2)
    asyncio.run(league.job())
    assert "NVDA" in p["positions"]
    at(2026, 10, 13, 10, 5)
    asyncio.run(league.job())
    assert "NVDA" not in p["positions"] and p["auto"]["cooldown"] == {"NVDA": "2026-10-18"}
    [(_, post)] = sent
    assert "🛑 Sold **NVDA** at $85.00 (-15.0%" in post.embeds[0].description
    assert p["auto"]["tally"] == {"closed": 1, "wins": 0, "sum_ret": pytest.approx(-0.15)}
    L.fill(p, {"side": "buy", "symbol": "AMD", "dollars": 8.5}, 100.0)
    p["positions"]["AMD"].update(stop=90.0, stop_hit=None)
    m.prices["AMD"] = 85.0
    at(2026, 10, 13, 11, 0)
    asyncio.run(league.job())
    m.prices["AMD"] = 95.0
    at(2026, 10, 13, 11, 5)
    asyncio.run(league.job())
    assert p["positions"]["AMD"]["stop_hit"] is None  # a dip that recovered


def test_sizing_from_five_dollars_to_ten_million(tmp_path, monkeypatch):
    league, m, _, at, _ = setup(tmp_path, monkeypatch, markets="stocks", amount=10_000_000.0)
    at(2026, 10, 12, 15, 45)
    asyncio.run(league.job())
    assert [round(x["cost"]) for x in league.players[B.BOT_ID]["positions"].values()] == [2_000_000] * 5
    league, m, _, at, _ = setup(tmp_path / "b", monkeypatch, markets="both", amount=5.0)
    at(2026, 10, 12, 15, 45)
    asyncio.run(league.job())
    assert [round(x["cost"], 2) for x in league.players[B.BOT_ID]["positions"].values()] == [1.0] * 5
    league, m, _, at, _ = setup(tmp_path / "c", monkeypatch, markets="stocks", amount=5.0)
    league.players[B.BOT_ID]["cash"] = 2.5  # worth $2.50 now: $1 buys while there's $1 left
    at(2026, 10, 12, 15, 45)
    asyncio.run(league.job())
    p = league.players[B.BOT_ID]
    assert len(p["positions"]) == 2 and p["cash"] == pytest.approx(0.5)


def test_old_league_files_and_the_standings(tmp_path):
    path = tmp_path / "league.json"
    old = {"players": {"1": L.new_player("Ann", 5000.0)}, "calls": []}
    path.write_text(json.dumps(old))
    bot = SimpleNamespace(data_dir=tmp_path, engine=None, channels=None)
    league = L.League(bot)
    league.save()
    assert set(json.loads(path.read_text())) == {"players", "calls"}
    p = bot_player()
    L.fill(p, {"side": "buy", "symbol": "NVDA", "dollars": 10.0}, 100.0)
    rows = L.standings({"1": league.players["1"], B.BOT_ID: p}, {"NVDA": 103.7}, 5000.0)
    e = L.league_embed(rows, [])
    assert "🥇 **🤖 MarketBot** +3.70% ($10.37 from $10.00)" in e.fields[0].value
    assert "🥈 **Ann** +0.00% ($100,000.00) · S&P" in e.fields[0].value


def test_commands(tmp_path, monkeypatch):
    from marketbot.commands import register_commands
    bot = make_bot(tmp_path, {"^GSPC": Quote("^GSPC", "S&P", 5000.0, 5000.0, 0.0)})
    bot.channels.set(7, "league")
    register_commands(bot)
    league = next(f for f in bot.features if f.name == "league")
    group = next(c for c in bot.tree.get_commands() if c.name == "botplayer")
    assert group.default_permissions.manage_channels and group.guild_only
    paper = next(c for c in bot.tree.get_commands() if c.name == "paper")

    def run(cmd, guild=9, **kw):
        it = Interaction(1)
        it.guild_id = guild
        it.user = SimpleNamespace(id=42, display_name="Ann", mention="<@42>")
        asyncio.run(cmd.callback(it, **kw))
        content, extra = it.out[-1]
        return content or extra.get("embed")

    assert "No bot player yet" in run(paper.get_command("bot")).description
    reply = run(group.get_command("start"), amount=10.0, markets=None)
    assert reply.startswith("🤖 **MarketBot** joined the league with **$10.00** of pretend money (stocks and crypto)")
    assert [cid for cid, _ in bot.sent] == [7]
    assert "already playing with $10.00" in run(group.get_command("start"), amount=50.0, markets=None)
    e = run(paper.get_command("bot"))
    assert e.title == "💼 🤖 MarketBot's paper portfolio" and "📒 Record" in [f.name for f in e.fields]
    other = Interaction(1)
    other.guild_id, other.user = 10, SimpleNamespace(id=7, display_name="Bob")
    asyncio.run(group.get_command("stop").callback(other))
    assert other.out[-1][0].startswith("Only admins")
    ended = run(group.get_command("stop"))
    assert ended.startswith("🤖 MarketBot's season 1 is over: $10.00 → $10.00 (+0.00%)")
    assert B.BOT_ID not in league.players and league.bot_seasons[0]["season"] == 1
    e = run(paper.get_command("bot"))
    assert "No bot player yet" in e.description and e.fields[-1].name == "🗂️ Past seasons"
    assert "season 2" in run(group.get_command("start"), amount=20.0, markets=None) or \
        league.players[B.BOT_ID]["auto"]["season"] == 2
    assert "🤖 MarketBot" in league.status()[0] and "1 players" not in league.status()[0]
