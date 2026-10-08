"""The market calendar: parsing Nasdaq's and the Fed's calendars (real samples in tests/data), playbooks,
earnings reactions, the posts and their schedule."""

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("numpy")

from marketbot.addons import calendar as CAL  # noqa: E402
from marketbot.apis import calendars as C  # noqa: E402
from marketbot.yahoo import Bars  # noqa: E402
from tests.market_helpers import quote  # noqa: E402

DATA = Path(__file__).parent / "data"


def load(name):
    return json.loads((DATA / name).read_text())


# ----- parsing -----

def test_us_events_we_track_with_times_in_eastern():
    events = C.parse_econ(load("nasdaq_econ_2026-09-17.json"), date(2026, 9, 17))
    keys = [e.key for e in events]
    assert "fomc" in keys and "retail" in keys and all(e.key != "Germany" for e in events)
    fed = next(e for e in events if e.key == "fomc")
    assert (fed.actual, fed.consensus, fed.previous) == ("4.00%", "4.00%", "3.75%")
    assert datetime.fromtimestamp(fed.at, C.NEW_YORK).strftime("%H:%M") == "14:00"
    retail = [e for e in events if e.key == "retail"]
    assert {e.name for e in retail} >= {"Retail Sales", "Core Retail Sales"}
    assert datetime.fromtimestamp(retail[0].at, C.NEW_YORK).strftime("%H:%M") == "08:30"
    assert not any("Ex Gas" in e.name or "Control" in e.name or "Inventories" in e.name for e in events)


@pytest.mark.parametrize("name, key", [
    ("CPI (MoM) (Sep)", "cpi"), ("Core CPI (YoY)", "core_cpi"), ("Nonfarm Payrolls", "payrolls"),
    ("Unemployment Rate", "unemployment"), ("GDP (QoQ) (Q3)", "gdp"), ("Core PCE Price Index (MoM)", "core_pce"),
    ("PPI (MoM)", "ppi"), ("Initial Jobless Claims", "claims"), ("ISM Manufacturing PMI", "ism"),
    ("JOLTS Job Openings", "jolts"), ("CB Consumer Confidence", "sentiment"), ("Fed Interest Rate Decision", "fomc"),
    ("FOMC Meeting Minutes", "minutes"), ("Retail Sales Ex Gas/Autos", None), ("GDP Price Index", None),
    ("ISM Manufacturing Prices", None), ("FOMC Statement", None), ("Continuing Jobless Claims", None),
])
def test_event_names(name, key):
    hit = C.classify(name)
    assert (hit[0] if hit else None) == key


@pytest.mark.parametrize("text, v", [("0.4%", 0.4), ("254K", 254_000), ("-1.2B", -1.2e9), ("4.10%", 4.1),
                                     ("1,701K", 1_701_000), ("&nbsp;", None), ("", None), ("n/a", None)])
def test_values(text, v):
    assert C.value(text) == (pytest.approx(v) if v is not None else None)


def ev(actual, consensus, previous="", key="cpi", hot=1):
    return C.EconEvent(key, "CPI (MoM)", "CPI", 0, 3, actual, consensus, previous, 10, hot)


def test_surprise_against_consensus_else_previous():
    assert C.surprise(ev("0.4%", "0.3%")) == 1 and C.surprise(ev("0.2%", "0.3%")) == -1
    assert C.surprise(ev("0.3%", "0.3%")) == 0 and C.surprise(ev("0.5%", "", "0.3%")) == 1
    assert C.surprise(ev("", "0.3%")) == 0


def test_earnings_rows():
    rows = C.parse_earnings(load("nasdaq_earnings_2026-10-14.json"), date(2026, 10, 14))
    asml = next(r for r in rows if r.symbol == "ASML")
    assert asml.timing == "before open" and asml.market_cap > 1e11 and asml.eps_forecast == pytest.approx(12.47)
    assert asml.day == "2026-10-14" and all(r.symbol for r in rows)


def test_surprises_newest_first():
    rows = C.parse_surprises(load("nasdaq_surprise_nvda.json"))
    assert rows[0]["reported"] == "2026-08-26" and rows[0]["surprise_pct"] == pytest.approx(6.22)
    assert [r["reported"] for r in rows] == sorted((r["reported"] for r in rows), reverse=True)
    assert C.parse_surprises({}) == [] and C.parse_surprises({"data": None}) == []


def test_fomc_days_from_the_fed_page():
    days = C.parse_fomc((DATA / "fomc_calendar.html").read_text())
    assert {"2024-01-31", "2025-07-30", "2026-07-29", "2026-09-16"} <= set(days)
    assert days == sorted(days) and all(d[5:7] != "00" for d in days)


def test_fomc_cross_month_and_notation_votes():
    page = ('<h4><a id="1">2025 FOMC Meetings</a></h4>'
            '<div class="fomc-meeting__month"><strong>Apr/May</strong></div><div class="fomc-meeting__date">30-1</div>'
            '<div class="fomc-meeting__month"><strong>August</strong></div>'
            '<div class="fomc-meeting__date">22 (notation vote)</div>'
            '<div class="fomc-meeting__month"><strong>December</strong></div><div class="fomc-meeting__date">9-10*</div>')
    assert C.parse_fomc(page) == ["2025-05-01", "2025-12-10"]


# ----- playbooks and reactions -----

def bars(closes, start="2026-01-01"):
    t0 = int(np.datetime64(start, "s").astype(int))
    t = t0 + np.arange(len(closes), dtype=np.int64) * 86400
    c = np.asarray(closes, dtype=float)
    return Bars("X", t, c, c, c, c, np.ones(len(c)))


def test_playbook_measures_release_days_against_normal_days():
    rng = np.random.default_rng(2)
    moves = rng.normal(0, 0.005, 300)
    days = [str(np.datetime64("2026-01-01") + np.timedelta64(i, "D")) for i in range(1, 300)]
    release = days[10::25]  # every 25th day: bigger moves
    for d in release:
        moves[days.index(d) + 1] = 0.02 if days.index(d) % 2 else -0.02
    b = bars(100 * np.cumprod(1 + moves))
    p = CAL.make_playbook("CPI days", b, release)
    assert p.days == len(release) and p.avg_move == pytest.approx(2.0, abs=0.05)
    assert p.normal_move < 1.0 and "bigger than a normal day" in CAL.playbook_text(p)
    assert CAL.make_playbook("x", b, release[:3]) is None
    assert CAL.playbook_text(None) == ""


def test_earnings_reaction_follows_the_report_timing():
    b = bars([100, 100, 110, 99, 99], start="2026-08-24")  # 24: 100, 25: 100, 26: 110, 27: 99
    assert CAL.reaction(b, "2026-08-26", "before open") == pytest.approx(10.0)
    assert CAL.reaction(b, "2026-08-26", "after close") == pytest.approx(-10.0)
    assert CAL.reaction(b, "2026-08-26", "") == pytest.approx(10.0)  # the bigger of the two
    assert CAL.reaction(b, "2026-08-28", "after close") is None  # no next day yet
    assert CAL.reaction(b, "2026-09-30", "") is None


# ----- the desk (sources faked) -----

class FakeCal:
    def __init__(self, econ=None, earnings=None, surprises=None, fomc=None, ahead=None, past=None):
        self._econ, self._earn = econ or {}, earnings or {}
        self._surp, self._fomc = surprises or {}, fomc or []
        self._ahead, self._past = ahead or {}, past or {}
        self.fred = SimpleNamespace(enabled=bool(ahead or past), status_line=lambda: "ok")

    async def econ(self, day, ttl=900):
        return list(self._econ.get(day.isoformat(), []))

    async def earnings(self, day, ttl=0):
        return list(self._earn.get(day.isoformat(), []))

    async def surprises(self, symbol):
        return list(self._surp.get(symbol, []))

    async def fomc_days(self):
        return list(self._fomc)

    async def release_dates_ahead(self, rid, start, end):
        return [d for d in self._ahead.get(rid, []) if start.isoformat() <= d < end.isoformat()]

    async def release_dates(self, rid, before, count=16):
        return self._past.get(rid, [])[:count]


def make_desk(tmp_path, cal, quotes=None, history=None):
    from marketbot.channels import ChannelStore
    from marketbot.storage import StateStore
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    async def run(fn, *a):
        return fn(*a)

    async def quotes_fn(symbols):
        return {s: q for s, q in (quotes or {}).items() if s in symbols}

    async def daily(symbol, fresh=0):
        if symbol not in (history or {}):
            raise LookupError(symbol)
        return history[symbol]

    async def options(symbol):
        return SimpleNamespace(expected_move=7.0, spot=100.0) if symbol == "NVDA" else None

    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(9, "calendar", 1)
    state = StateStore(tmp_path / "state.json")
    bot = SimpleNamespace(
        engine=SimpleNamespace(data=SimpleNamespace(http=object(), quotes=quotes_fn),
                               cache=SimpleNamespace(daily=daily), run=run, options=options),
        data_dir=tmp_path, channels=channels, state=state, send=send, _watchlist=lambda m: ["NVDA"])

    def due(cid, kind, local, hour, minute, window=120):
        from marketbot.bot import MarketBot
        return MarketBot._due(bot, cid, kind, local, hour, minute, window)

    bot._due = due
    desk = CAL.CalendarDesk.__new__(CAL.CalendarDesk)
    CAL.Feature.__init__(desk, bot)
    desk.cal = cal
    desk._books = {}
    return desk, sent


def econ_events(day="2026-09-17"):
    return C.parse_econ(load("nasdaq_econ_2026-09-17.json"), date.fromisoformat(day))


def earning(symbol, day, timing="after close", cap=4e12):
    return C.Earning(symbol, f"{symbol} Inc", day, timing, cap, 1.0, 20)


def test_schedule_merges_nasdaq_fred_fomc_and_earnings(tmp_path):
    cal = FakeCal(econ={"2026-09-17": econ_events()}, fomc=["2026-09-17", "2026-10-28"],
                  earnings={"2026-09-16": [earning("NVDA", "2026-09-16", cap=1e9), earning("TINY", "2026-09-16", cap=1e9),
                                           earning("AAPL", "2026-09-16", cap=3e12)]},
                  ahead={10: ["2026-09-15", "2026-10-14"], 9: ["2026-09-17"]})
    desk, _ = make_desk(tmp_path, cal)
    items = asyncio.run(desk.schedule(date(2026, 9, 14), 7))
    keys = [(it.day, it.key) for it in items]
    assert ("2026-09-15", "cpi") in keys  # from FRED, Nasdaq hadn't listed it
    assert keys.count(("2026-09-17", "fomc")) == 1 and keys.count(("2026-09-17", "retail")) == 1  # no double
    assert ("2026-10-14", "cpi") not in keys  # outside the week
    syms = [it.earning.symbol for it in items if it.earning]
    assert syms == ["AAPL", "NVDA"]  # big, or on a watchlist; biggest first
    assert all(items[i].day <= items[i + 1].day for i in range(len(items) - 1))


def test_week_ahead_and_agenda_fit_discord(tmp_path):
    spx = bars(100 * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.01, 400)), start="2025-08-01")
    days = [str(d) for d in (np.datetime64("2025-08-01") + np.arange(1, 380, 21).astype("timedelta64[D]"))]
    many = [earning(f"S{i}", "2026-09-17", "before open" if i % 2 else "after close", cap=6e10 + i) for i in range(30)]
    cal = FakeCal(econ={"2026-09-17": econ_events()}, fomc=["2026-09-17"] + days[:8], earnings={"2026-09-17": many},
                  past={9: days[:12]}, surprises={"S1": [{"reported": "2026-05-20", "quarter": "Q", "eps": 1.0,
                                                           "consensus": 0.9, "surprise_pct": 11.1}]})
    desk, _ = make_desk(tmp_path, cal, history={"^GSPC": spx})
    week = asyncio.run(desk.week_ahead(date(2026, 9, 14)))
    assert week.fields[0].name == "Thursday 17" and "Fed decision" in week.fields[0].value
    assert "Fed decision days" not in week.fields[0].value or "S&P 500 moved" in week.fields[0].value
    assert len(week) <= 6000
    agenda = asyncio.run(desk.agenda(date(2026, 9, 17)))
    names = [f.name for f in agenda.fields]
    assert names[0] == "Economy (ET)" and "Earnings before the open" in names and "Earnings after the close" in names
    assert "2 PM" in agenda.fields[0].value and len(agenda) <= 6000
    assert all(len(f.value) <= 1024 for f in agenda.fields)


def test_quiet_days_say_so(tmp_path):
    desk, _ = make_desk(tmp_path, FakeCal())
    assert "No major" in asyncio.run(desk.agenda(date(2026, 9, 19))).description
    assert "quiet week" in asyncio.run(desk.week_ahead(date(2026, 9, 21))).description


def test_results_are_posted_once_with_the_right_words(tmp_path):
    events = [ev("0.4%", "0.3%", "0.2%"), C.EconEvent("core_cpi", "Core CPI (MoM)", "Core CPI", 0, 3, "0.2%", "0.3%",
                                                     "0.3%", 10, 1),
              C.EconEvent("claims", "Initial Jobless Claims", "Jobless claims", 0, 1, "230K", "210K", "215K", 180, -1),
              C.EconEvent("fomc", "Fed Interest Rate Decision", "Fed decision", 0, 3, "4.25%", "4.00%", "4.00%",
                          None, 1)]
    desk, sent = make_desk(tmp_path, FakeCal(econ={"2026-09-17": events}),
                           quotes={"^GSPC": quote("^GSPC", 6000, -0.8)})
    ny = datetime(2026, 9, 17, 9, 0, tzinfo=C.NEW_YORK)
    asyncio.run(desk.post_results(ny, desk.bot.channels.of_kind("calendar")))
    asyncio.run(desk.post_results(ny, desk.bot.channels.of_kind("calendar")))
    titles = [p.embeds[0].title for _, p in sent]
    assert titles == ["📊 CPI is out", "📊 Fed decision is out"]  # claims are minor: not posted; once each
    cpi = sent[0][1].embeds[0].description
    assert "**hotter than expected**" in cpi and "**cooler than expected**" in cpi
    assert "more hawkish than expected" in sent[1][1].embeds[0].description
    assert "-0.80%" in sent[0][1].embeds[0].fields[0].value


def test_earnings_reactions_after_the_open(tmp_path):
    surprises = {"NVDA": [{"reported": "2026-09-16", "quarter": "Q", "eps": 2.2, "consensus": 2.0, "surprise_pct": 10.0}]}
    cal = FakeCal(earnings={"2026-09-16": [earning("NVDA", "2026-09-16", "after close", cap=4e12),
                                           earning("SMALL", "2026-09-16", "after close", cap=1e9)],
                            "2026-09-17": [earning("JPM", "2026-09-17", "before open", cap=7e11),
                                           earning("LATE", "2026-09-17", "after close", cap=7e11)]},
                  surprises=surprises)
    desk, sent = make_desk(tmp_path, cal, quotes={"NVDA": quote("NVDA", 190, 9.5), "JPM": quote("JPM", 300, -1.2)})
    ny = datetime(2026, 9, 17, 9, 55, tzinfo=C.NEW_YORK)
    asyncio.run(desk.post_reactions(ny, desk.bot.channels.of_kind("calendar")))
    asyncio.run(desk.post_reactions(ny, desk.bot.channels.of_kind("calendar")))
    titles = [p.embeds[0].title for _, p in sent]
    assert titles == ["💼 NVDA after earnings: +9.5%", "💼 JPM after earnings: -1.2%"]
    assert "a bigger move than priced" in sent[0][1].embeds[0].description
    assert "EPS $2.20 vs $2.00 expected (+10.0%)" in sent[0][1].embeds[0].description


def test_the_job_posts_the_agenda_once_on_trading_days(tmp_path, monkeypatch):
    desk, sent = make_desk(tmp_path, FakeCal(econ={"2026-09-17": econ_events()}))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 17, 7, 50, tzinfo=tz)

    monkeypatch.setattr(CAL, "datetime", Clock)
    asyncio.run(desk.job())
    asyncio.run(desk.job())
    assert [p.embeds[0].title for _, p in sent] == ["📅 Today · Thursday, September 17"]
    desk.bot.channels.update(9, briefs=False)
    desk.bot.state.set("briefs", "x", 1)
    assert desk.watchlist() == {"NVDA"}


def test_no_calendar_channel_means_no_work(tmp_path):
    desk, sent = make_desk(tmp_path, FakeCal())
    desk.bot.channels.remove(9)
    asyncio.run(desk.job())
    assert not sent
