"""/portfolio (private holdings, beta, stress tests, the weekly DM) and the weekly market memo."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from marketbot.addons import memo as M
from marketbot.addons import portfolio as P
from marketbot.yahoo import Bars, Quote
from tests.live_rehearsal import Interaction
from tests.test_market_bot import make_bot


def series(start: str, closes, symbol="X"):
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() + 14.5 * 3600
    c = np.asarray(closes, dtype=float)
    t = t0 + np.arange(len(c)) * 86400.0
    return Bars(symbol, t, c, c, c, c, np.ones(len(c)))


def q(symbol, price, change=0.0):
    return Quote(symbol, symbol, price, price / (1 + change / 100), change)


def test_add_and_remove_keep_the_average_cost():
    h = {}
    P.add(h, "NVDA", 10, 100.0)
    P.add(h, "NVDA", 10, 200.0)
    assert h["NVDA"] == {"shares": 20, "cost": 3000.0}
    assert P.remove(h, "NVDA", 5) and h["NVDA"]["cost"] == pytest.approx(2250.0)
    assert P.remove(h, "NVDA", None) and h == {} and not P.remove(h, "NVDA", None)


def test_snapshot_and_show():
    h = {"NVDA": {"shares": 10, "cost": 1000.0}, "BTC-USD": {"shares": 0.1, "cost": 5000.0}}
    snap = P.snapshot(h, {"NVDA": q("NVDA", 150.0, 50.0), "BTC-USD": q("BTC-USD", 60_000.0, 0.0)})
    assert snap["total"] == 7500 and snap["day"] == pytest.approx(500) and snap["rows"][0]["symbol"] == "BTC-USD"
    e = P.show_embed("Ann", snap, 1.3)
    assert "Worth **$7,500** · today **$500** (+7.14%)" in e.description and "Beta **1.30**" in e.description
    assert "⚠️ BTC is 80% of it" in e.description and "**NVDA** $1,500 · 20% · +50.0% today · +50.0% total" in \
        e.fields[0].value


def test_beta_and_close_on():
    rng = np.random.default_rng(1)
    m = rng.normal(0, 0.01, 400)
    bench = series("2024-01-01", 100 * np.cumprod(1 + m))
    stock = series("2024-01-01", 50 * np.cumprod(1 + 2 * m))
    assert P.beta(stock, bench) == pytest.approx(2.0, abs=0.01)
    assert P.beta(series("2024-01-01", [1.0] * 30), bench) is None
    s = series("2020-02-17", [10, 11, 12, 13])
    assert P.close_on(s, "2020-02-18") == 11 and P.close_on(s, "2020-02-24") == 13
    assert P.close_on(s, "2020-02-01") is None and P.close_on(s, "2020-03-01") is None  # long after it stopped


def test_stress_uses_real_history_when_it_exists_and_beta_otherwise():
    bench = series("2020-02-19", np.linspace(100, 66, 34))  # -34% to 2020-03-23
    old = series("2020-02-19", np.linspace(50, 25, 34))  # -50%
    new = series("2025-01-01", [10.0] * 300)  # listed later
    snap = P.snapshot({"OLD": {"shares": 10, "cost": 500}, "NEW": {"shares": 50, "cost": 500}},
                      {"OLD": q("OLD", 50.0), "NEW": q("NEW", 10.0)})
    r = P.stress(snap, {"OLD": 1.5, "NEW": None}, {"OLD": old, "NEW": new}, bench)
    assert [round(p, 3) for _, _, p in r["shocks"]] == [-0.125, -0.25, -0.437]  # NEW counts as beta 1
    [(name, spx, pct, guessed)] = r["episodes"]  # 2008 and 2022 aren't in the S&P history given
    assert name == "2020 Covid crash" and spx == pytest.approx(-0.34) and guessed == 1
    assert pct == pytest.approx(0.5 * -0.5 + 0.5 * -0.34)
    e = P.stress_embed(snap, r, 1.25)
    assert "**-10%** → about **-$125** (-12.5%)" in e.fields[0].value and "1 estimated from beta" in e.fields[1].value


def make_desk(tmp_path, quotes, histories):
    bot = make_bot(tmp_path, quotes)

    async def daily(symbol, fresh=0):
        if symbol not in histories:
            raise KeyError(symbol)
        return histories[symbol]

    bot.engine.cache = SimpleNamespace(daily=daily)
    from marketbot.commands import register_commands
    register_commands(bot)

    async def resolve(interaction, text):
        return SimpleNamespace(symbol=text, name=text, market="stocks")

    bot.resolve_symbol = resolve
    return bot, next(f for f in bot.features if f.name == "portfolio")


def run(bot, sub, user=42, **kw):
    group = next(c for c in bot.tree.get_commands() if c.name == "portfolio")
    it = Interaction(1)
    it.user = SimpleNamespace(id=user, display_name="Ann")
    asyncio.run(group.get_command(sub).callback(it, **kw))
    content, extra = it.out[-1]
    assert extra.get("ephemeral") is True
    return content or extra.get("embed")


def test_commands_are_private(tmp_path):
    rng = np.random.default_rng(2)
    m = rng.normal(0, 0.01, 300)
    hist = {"^GSPC": series("2025-01-01", 100 * np.cumprod(1 + m)),
            "NVDA": series("2025-01-01", 100 * np.cumprod(1 + 1.5 * m))}
    bot, desk = make_desk(tmp_path, {"NVDA": q("NVDA", 120.0, 2.0)}, hist)
    assert "It's empty" in run(bot, "show")
    assert run(bot, "add", symbol="NVDA", shares=10.0, price=100.0).startswith("✅ NVDA: you now hold 10 at an "
                                                                                 "average $100.00")
    assert "at an average $110.00" in run(bot, "add", symbol="NVDA", shares=10.0, price=None)  # today's $120
    e = run(bot, "show")
    assert "Worth **$2,400**" in e.description and "Beta **1.5" in e.description
    e = run(bot, "stress")
    assert e.title == "🧯 Portfolio stress test" and "**-20%**" in e.fields[0].value
    assert run(bot, "remove", symbol="AAPL", shares=None) == "You don't have AAPL in your portfolio."
    assert "It's empty" in run(bot, "show", user=7)  # someone else's is separate
    saved = P.read_json(tmp_path / "portfolios.json", {})
    assert saved["users"]["42"]["holdings"]["NVDA"]["shares"] == 20


def test_weekly_dm_after_the_weeks_last_close(tmp_path, monkeypatch):
    hist = {"NVDA": series("2026-09-01", np.linspace(100, 110, 40))}
    bot, desk = make_desk(tmp_path, {"NVDA": q("NVDA", 120.0)}, hist)
    desk.users = {"42": {"holdings": {"NVDA": {"shares": 10, "cost": 1000.0}}, "memo": True},
                  "7": {"holdings": {"NVDA": {"shares": 1, "cost": 100.0}}, "memo": False}}
    got = []

    async def send(embed=None):
        got.append(embed)

    bot.get_user = lambda uid: SimpleNamespace(send=send)
    next(f for f in bot.features if f.name == "memo").latest = {"summary": "Stocks rose."}

    def at(*when):
        class Clock(P.datetime):
            @classmethod
            def now(cls, tz=None):
                return P.datetime(*when, tzinfo=tz)
        monkeypatch.setattr(P, "datetime", Clock)

    at(2026, 10, 9, 16, 50)
    asyncio.run(desk.job_memo())
    assert not got  # before 5 PM
    at(2026, 10, 9, 17, 5)
    asyncio.run(desk.job_memo())
    asyncio.run(desk.job_memo())
    [e] = got
    assert e.title.startswith("📬 Your week") and "**$1,200**" in e.description
    assert e.fields[-1].value == "Stocks rose."


# ----- the weekly memo -----

def test_week_move_and_formats():
    b = series("2026-09-01", np.linspace(100, 120, 21))
    now = b.t[-1] + 3600
    assert M.week_move(b, now) == pytest.approx((120 / 113 - 1) * 100, rel=1e-3)
    assert M.fmt("^TNX", 0.12) == "+12 bp" and M.fmt("^VIX", -2.0) == "-2.0 pts" and M.fmt("^GSPC", 1.234) == "+1.2%"
    assert M.week_move(series("2026-09-01", [1.0] * 5), now) is None


def memo_desk(tmp_path, ai_answer):
    from marketbot.channels import ChannelStore
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    class AI:
        enabled = ai_answer is not None

        async def complete(self, *a, **k):
            return ai_answer

    async def daily(symbol, fresh=0):
        return series("2026-09-01", np.linspace(100, 110 if symbol != "XLE" else 90, 38))

    async def schedule(start, days):
        return [SimpleNamespace(day="2026-10-13", title="CPI", importance=3, key="cpi"),
                SimpleNamespace(day="2026-10-14", title="Claims", importance=1, key="claims")]

    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(4, "research")
    due = set()

    def _due(cid, kind, local, hour, minute, window_min=120):
        if (cid, kind) in due or (local.hour, local.minute) < (hour, minute):
            return False
        due.add((cid, kind))
        return True

    news = [SimpleNamespace(opinion=False, importance=80, headline=SimpleNamespace(title="Fed holds rates",
                                                                                   published=1.7914e9)),
            SimpleNamespace(opinion=True, importance=90, headline=SimpleNamespace(title="Op-ed", published=1.7914e9))]
    bot = SimpleNamespace(data_dir=tmp_path, channels=channels, send=send, _due=_due, ai=AI(), recent_news=news,
                          engine=SimpleNamespace(cache=SimpleNamespace(daily=daily)),
                          features=[SimpleNamespace(name="calendar", schedule=schedule)])
    return M.MemoDesk(bot), sent


def test_memo_posts_once_after_the_weeks_last_close(tmp_path, monkeypatch):
    desk, sent = memo_desk(tmp_path, {"title": "Calm week", "summary": "Stocks rose, energy fell.",
                                      "points": ["Tech led"], "watch": ["CPI on Tuesday"]})

    def at(*when):
        class Clock(M.datetime):
            @classmethod
            def now(cls, tz=None):
                return M.datetime(*when, tzinfo=tz)
        monkeypatch.setattr(M, "datetime", Clock)
        monkeypatch.setattr(M.time, "time", lambda: Clock.now(M.timezone.utc).timestamp())

    at(2026, 10, 8, 17, 0)  # Thursday
    asyncio.run(desk.job())
    assert not sent and [n["title"] for n in desk.news] == ["Fed holds rates"]
    at(2026, 10, 9, 16, 45)
    asyncio.run(desk.job())
    asyncio.run(desk.job())
    [(cid, post)] = sent
    e = post.embeds[0]
    assert cid == 4 and e.title == "🗞️ Weekly memo: Calm week" and "free AI reader" in e.footer.text
    assert [f.name for f in e.fields] == ["The week", "Sectors", "Takeaways", "Next week"]
    assert e.fields[1].value.endswith("Energy -1.8%")
    assert desk.latest["facts"]["next_week"] == ["Tue: CPI"] and desk.latest["ai"] is True


def test_memo_without_ai_uses_the_facts(tmp_path):
    desk, _ = memo_desk(tmp_path, None)
    desk.collect_news(1.7914e9)
    memo, facts, used_ai = asyncio.run(desk.write(1.7914e9 + 3600))
    assert not used_ai and memo["title"] == "Stocks rose this week" and "Fed holds rates" in memo["points"]
    assert M.memo_embed(memo, facts, False).footer.text.startswith("Summary from the facts above")
