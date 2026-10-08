"""/when: plain-words alerts. The parser, the AI fallback, conditions on prices, RSI, averages and 52-week levels,
firing only when they become true, and delivery (channel or DM)."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from marketbot.addons import when as W
from marketbot.yahoo import Bars, Quote
from tests.live_rehearsal import Interaction
from tests.test_market_bot import make_bot


def conds(text):
    return [(c.metric, c.op, c.value) for c in W.parse(text)[0]]


@pytest.mark.parametrize("text,expected", [
    ("NVDA drops below 170", [("price", "below", 170.0)]),
    ("bitcoin is up 5% today", [("change", "above", 5.0)]),
    ("when ETH falls 10%", [("change", "below", -10.0)]),
    ("TSLA RSI under 30", [("rsi", "below", 30.0)]),
    ("nvidia oversold", [("rsi", "below", 30.0)]),
    ("SPY crosses above its 200-day average", [("ma", "above", 200.0)]),
    ("apple below the 50 day", [("ma", "below", 50.0)]),
    ("AMD makes a new 52-week high", [("high52", "above", 0.0)]),
    ("GME volume is 3x normal", [("volume", "above", 3.0)]),
    ("s&p 500 moves 2%", [("move", "above", 2.0)]),
    ("tell me when BTC hits 100k", [("price", "touch", 100_000.0)]),
    ("$AAPL above 1,505.5", [("price", "above", 1505.5)]),
    ("MSFT above 400 and RSI below 40", [("rsi", "below", 40.0), ("price", "above", 400.0)]),
    ("what's the weather", []),
])
def test_parser(text, expected):
    assert conds(text) == expected


def test_symbol_words_prefer_tickers_then_names():
    assert W.symbol_words(W.parse("tell me when NVDA RSI is under 30")[1])[0] == "NVDA"
    assert W.symbol_words(W.parse("when nvidia drops 5%")[1])[0] == "nvidia"
    assert W.symbol_words(W.parse("$tsla above 300")[1])[0] == "tsla"
    assert "RSI" not in W.symbol_words("RSI ME IF")


def test_validity():
    assert W.valid(W.Cond("price", "above", 10)) and not W.valid(W.Cond("price", "above", 0))
    assert not W.valid(W.Cond("rsi", "below", 150)) and not W.valid(W.Cond("ma", "above", 2.5))
    assert not W.valid(W.Cond("volume", "above", 0.5)) and not W.valid(W.Cond("nope", "above", 1))
    assert W.Cond("change", "below", -3).text() == "down 3%+ today"


def bars(close):
    n = len(close)
    t = 1.7e9 + np.arange(n) * 86400.0
    c = np.asarray(close, dtype=float)
    return Bars("X", t, c, c, c, c, np.ones(n))


def quote(price, change=1.0, volume=None, avg=None, state="REGULAR"):
    return Quote("NVDA", "NVIDIA", price, price / (1 + change / 100), change, volume=volume, avg_volume=avg,
                 market_state=state)


def test_conditions_hold():
    q = quote(105.0, change=-4.0, volume=3e6, avg=1e6)
    assert W.holds(W.Cond("price", "above", 100), q, None) and not W.holds(W.Cond("price", "below", 100), q, None)
    assert W.holds(W.Cond("change", "below", -3), q, None) and W.holds(W.Cond("move", "above", 3), q, None)
    assert W.holds(W.Cond("volume", "above", 3), q, None) and W.holds(W.Cond("volume", "above", 2), quote(1), None) \
        is None
    rising = bars(np.linspace(50, 100, 300))
    assert W.holds(W.Cond("rsi", "above", 70), q, rising) and W.holds(W.Cond("ma", "above", 200), q, rising)
    assert W.holds(W.Cond("high52", "above", 0), q, rising) and not W.holds(W.Cond("low52", "below", 0), q, rising)
    assert W.holds(W.Cond("rsi", "above", 70), q, bars([1.0] * 10)) is None  # too little history


def make_desk(tmp_path, price, ai_answer=None):
    bot = make_bot(tmp_path, {"NVDA": quote(price), "BTC-USD": Quote("BTC-USD", "Bitcoin", 60_000, 59_000, 1.7)})
    from marketbot.commands import register_commands

    class AI:
        enabled = ai_answer is not None

        async def complete(self, *a, **k):
            return ai_answer

    bot.ai = AI()
    register_commands(bot)
    desk = next(f for f in bot.features if f.name == "when")
    lookup = {"NVDA": "NVDA", "BTC": "BTC-USD", "bitcoin": "BTC-USD"}
    bot.engine.directory = SimpleNamespace(lookup=lambda w: SimpleNamespace(symbol=lookup[w]) if w in lookup else None)

    async def resolve(text):
        return SimpleNamespace(symbol=lookup[text]) if text in lookup else (_ for _ in ()).throw(KeyError(text))

    async def history(symbol, live=None):
        return bars(np.linspace(50, 100, 300))

    bot.engine.resolve, bot.engine.history = resolve, history
    return bot, desk


def run_cmd(bot, name, user=42, guild=True, **kw):
    cmd = next(c for c in bot.tree.get_commands() if c.name == name)
    it = Interaction(1)
    it.user = SimpleNamespace(id=user, display_name="Ann")
    it.guild = object() if guild else None
    asyncio.run(cmd.callback(it, **kw))
    content, extra = it.out[-1]
    return content or extra.get("embed")


def test_alert_fires_when_it_becomes_true_and_again_after_resetting(tmp_path):
    bot, desk = make_desk(tmp_path, 180.0)
    reply = run_cmd(bot, "when", text="NVDA drops below 170")
    assert reply.startswith("🔔 Watching **NVDA**: price below $170.00. I'll ping you here (#1).")
    asyncio.run(desk.job())
    assert not bot.sent
    bot.engine.data._quotes["NVDA"] = quote(165.0)
    asyncio.run(desk.job())
    asyncio.run(desk.job())
    [(cid, post)] = bot.sent
    assert cid == 1 and post.content == "<@42>" and post.embeds[0].title.startswith("🔔 NVDA: NVDA drops below 170")
    bot.engine.data._quotes["NVDA"] = quote(175.0)
    asyncio.run(desk.job())
    bot.engine.data._quotes["NVDA"] = quote(169.0)
    asyncio.run(desk.job())
    assert len(bot.sent) == 2 and desk.alerts[0]["fired"] == 2
    assert W.read_json(tmp_path / "when.json", {})["alerts"][0]["fired"] == 2


def test_true_at_creation_waits_for_the_next_time(tmp_path):
    bot, desk = make_desk(tmp_path, 160.0)
    assert "That's true right now" in run_cmd(bot, "when", text="NVDA below 170")
    asyncio.run(desk.job())
    assert not bot.sent


def test_hits_picks_the_side_to_travel_and_rsi_uses_history(tmp_path):
    bot, desk = make_desk(tmp_path, 180.0)
    assert "price above $200.00" in run_cmd(bot, "when", text="NVDA hits 200")
    assert "price below $150.00" in run_cmd(bot, "when", text="NVDA hits 150")
    assert "RSI above 70" in run_cmd(bot, "when", text="NVDA RSI above 70")
    assert desk.alerts[-1]["on"] is True  # the fake history is a steady climb


def test_ai_fills_in_what_the_parser_cant_read(tmp_path):
    bot, desk = make_desk(tmp_path, 180.0, {"symbol": "bitcoin", "conditions": [
        {"metric": "change", "op": "above", "value": 8}, {"metric": "bogus", "op": "above", "value": 1}]})
    reply = run_cmd(bot, "when", text="ping me if the orange coin moons 8 percent", guild=False)
    assert "**BTC**: up 8%+ today" in reply and "by DM" in reply and desk.alerts[0]["dm"] is True
    bot2, _ = make_desk(tmp_path / "b", 180.0)
    assert "couldn't tell which stock" in run_cmd(bot2, "when", text="moons 8 percent")
    assert "couldn't read a condition" in run_cmd(bot2, "when", text="what about NVDA")


def test_dm_delivery_and_list_and_remove(tmp_path):
    bot, desk = make_desk(tmp_path, 180.0)
    got = []

    async def send(embed=None):
        got.append(embed)

    bot.get_user = lambda uid: SimpleNamespace(send=send)
    run_cmd(bot, "when", text="NVDA above 190", guild=False)
    bot.engine.data._quotes["NVDA"] = quote(191.0)
    asyncio.run(desk.job())
    assert len(got) == 1 and not bot.sent
    listed = run_cmd(bot, "whens")
    assert "`#1` **NVDA**: price above $190.00 · fired 1×" in listed.description
    assert run_cmd(bot, "whens", user=7, remove=1) == "You have no alert #1."
    assert run_cmd(bot, "whens", remove=1) == "Removed #1." and not desk.alerts


def test_stock_day_changes_wait_for_the_open(tmp_path):
    bot, desk = make_desk(tmp_path, 180.0)
    run_cmd(bot, "when", text="NVDA up 5% today")
    bot.engine.data._quotes["NVDA"] = quote(190.0, change=6.0, state="PRE")
    asyncio.run(desk.job())
    assert not bot.sent
    bot.engine.data._quotes["NVDA"] = quote(190.0, change=6.0)
    asyncio.run(desk.job())
    assert len(bot.sent) == 1
