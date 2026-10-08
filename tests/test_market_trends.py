"""The trends channel, offline: period changes from daily closes, the saved closes, the trends desk (Yahoo's spark,
the backups and the screeners all faked), the trends embeds and recaps, and the bot's trends board, recap schedule,
commands and channel kinds. No network, no sleeps: clocks are faked where the code reads them."""

import asyncio
import json
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
discord = pytest.importorskip("discord")

from discord import app_commands  # noqa: E402

from marketbot import bot as botmod, briefs, embeds as E, trends as T  # noqa: E402
from marketbot.ai import NewsAI  # noqa: E402
from marketbot.bot import TRENDS_CLOSED_SECONDS, TRENDS_OPEN_SECONDS, MarketBot  # noqa: E402
from marketbot.briefs import Post, _fit_total  # noqa: E402
from marketbot.channels import KIND_NAMES, KINDS, ChannelStore  # noqa: E402
from marketbot.commands import INTROS, KIND_CHOICES, SETUP_CHANNELS, register_commands  # noqa: E402
from marketbot.directory import Directory, Listing  # noqa: E402
from marketbot.hours import NEW_YORK, is_trading_day  # noqa: E402
from marketbot.limits import DESCRIPTION, FIELD_NAME, FIELD_VALUE, FIELDS, FOOTER, TITLE, TOTAL  # noqa: E402
from marketbot.sources import Coin  # noqa: E402
from marketbot.trends import (BAR_HOUR, BARS_BACK, CRYPTO_ALIASES, CRYPTO_LABELS, CRYPTO_PERIODS, DAY,  # noqa: E402
                              EPOCH, KEEP_DAYS, MAJOR_ETFS, MIN_COIN_VOLUME, PERIODS, SPARK_BATCH, STABLES,
                              ClosesStore, Mover, Snapshot, TrendsDesk, last_trading_day_of_month,
                              last_trading_day_of_week, movers_from_screener, period_changes, real_coin)
from marketbot.universe import CRYPTO, SECTORS, STOCKS  # noqa: E402
from marketbot.yahoo import Bars, Quote, YahooError  # noqa: E402

END = date(2026, 10, 6)  # a Tuesday
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=NEW_YORK).timestamp()  # during the session


# ----- helpers -----

def ny(y, m, d, hh=16, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NEW_YORK)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def stamps(days, hh=16, mm=0):
    """Unix times of the given dates at hh:mm New York time."""
    return np.array([datetime.combine(d, dtime(hh, mm), NEW_YORK).timestamp() for d in days], dtype=np.int64)


def calendar_days(end, n):
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def trading_days(end, n):
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def ratio(a, b):
    return (a / b - 1) * 100


def crypto_label(period):
    """The title words of a crypto trends embed: CoinGecko's rolling windows, the calendar periods mapped to them."""
    return CRYPTO_LABELS.get(CRYPTO_ALIASES.get(period, period), PERIODS[period])


def label(period, market):
    return crypto_label(period) if market == "crypto" else PERIODS[period]


def coin(symbol, name, price=10.0, c24=1.0, c7=5.0, c30=10.0, c1y=50.0, volume=5e7, cap=1e9, rank=1):
    return Coin(symbol, name, price, cap, rank, 0.1, c24, c7, -20.0, volume, c30, c1y)


def coin_symbol(i):
    return "K" + chr(65 + i // 26) + chr(65 + i % 26)  # no trailing digits: short() would strip them


def assert_fits(e):
    """Every one of Discord's embed limits."""
    assert e.title is None or len(e.title) <= TITLE
    assert e.description is None or len(e.description) <= DESCRIPTION
    assert len(e.fields) <= FIELDS
    for f in e.fields:
        assert 0 < len(f.name) <= FIELD_NAME
        assert 0 < len(f.value) <= FIELD_VALUE
    assert not e.footer.text or len(e.footer.text) <= FOOTER
    assert len(e) <= TOTAL


def blocks_closed(e):
    for f in e.fields:
        if f.value.startswith("```"):
            assert f.value.endswith("```") and f.value.count("```") % 2 == 0, f.name


def rows(value):
    """The table rows of a field ("—" is an empty table)."""
    if value == "—":
        return []
    return [ln for ln in value.split("\n") if ln and not ln.startswith("```")]


def row_labels(value, width=7):
    """The label column of an ANSI table (after the colour code, the arrow and a space)."""
    return [ln.split("m", 1)[1][2:2 + width].strip() for ln in rows(value)]


def big_snapshot(day_source="Yahoo Finance"):
    """Everything the trends channel can show, at full size, with a few absurd values."""
    rng = np.random.default_rng(11)
    sp500 = [f"S{i:03d}" for i in range(503)]
    ndx = [f"S{i:03d}" for i in range(0, 200, 2)] + [f"N{i:02d}" for i in range(10)]
    snap = Snapshot(NOW, day_source=day_source, periods_source="Yahoo Finance")
    for s in dict.fromkeys(sp500 + ndx + list(SECTORS) + MAJOR_ETFS):
        snap.changes[s] = {p: float(rng.normal(0, 15)) for p in PERIODS}
        snap.prices[s] = float(rng.uniform(1, 3000))
        snap.names[s] = "An Extremely Long Company Name Holdings Incorporated Class A Common Stock"
    snap.changes["S007"]["1W"] = snap.changes["S007"]["WTD"] = 98765.43
    snap.prices["S007"] = 987654321.5
    snap.day_gainers = [Mover(f"GAIN{i}", "Name " * 40, 1234567.0 + i, 80.0 - i, 9.9e12, 9e13) for i in range(25)]
    snap.day_losers = [Mover(f"LOSE{i}", "Name " * 40, 0.00001234 + i, -60.0 + i, 1e9) for i in range(25)]
    snap.most_active = [Mover(f"ACT{i}", "Name " * 40, 55.5, 1.5 - i * 0.2, 1e9 * (25 - i)) for i in range(25)]
    snap.coins = [coin(coin_symbol(i), f"Coin {coin_symbol(i)}", float(rng.uniform(0.001, 70000)),
                       float(rng.normal(0, 10)), float(rng.normal(0, 20)), float(rng.normal(0, 30)),
                       float(rng.normal(0, 200)), float(rng.uniform(2e6, 5e10)), float(rng.uniform(1e8, 1e12)), i + 1)
                  for i in range(250)]
    return snap, sp500, ndx


def huge_snapshot():
    """Prices and moves so large that every table row is very wide: the recaps must still fit one message."""
    snap, sp500, ndx = big_snapshot()
    for k, s in enumerate(snap.changes):
        snap.changes[s] = {p: (1e30 if (k + j) % 2 else -1e30) * (1 + k / 1000) for j, p in enumerate(PERIODS)}
        snap.prices[s] = 1e40
    for lst, sign in ((snap.day_gainers, 1), (snap.day_losers, -1), (snap.most_active, 1)):
        for m in lst:
            m.price, m.change, m.volume = 1e40, sign * 1e30, 1e40
    for c in snap.coins:
        c.price, c.volume = 1e40, 1e40
        c.change_24h = c.change_7d = c.change_30d = c.change_1y = 1e30 if c.rank % 2 else -1e30
    return snap, sp500, ndx


# ----- period_changes -----

def test_period_changes_exact_numbers():
    days = calendar_days(END, 300)
    t, c = stamps(days), 100.0 + np.arange(300)
    ch = period_changes(t, c, NOW)
    assert set(ch) == set(PERIODS)
    assert days[20] == date(2025, 12, 31)  # the last close of last year
    assert days[297] == date(2026, 10, 4) and days[293] == date(2026, 9, 30)  # the Sunday and the month end before
    expected = {"1D": ratio(399, 398), "WTD": ratio(399, 397), "MTD": ratio(399, 393), "1W": ratio(399, 394),
                "1M": ratio(399, 378), "3M": ratio(399, 336), "1Y": ratio(399, 147), "YTD": ratio(399, 120)}
    for period, value in expected.items():
        assert ch[period] == pytest.approx(value, rel=1e-12), period
    assert all(type(v) is float for v in ch.values())
    assert BARS_BACK == {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "1Y": 252}


def test_period_changes_falling_prices_are_negative():
    t, c = stamps(calendar_days(END, 70)), 400.0 - np.arange(70)
    ch = period_changes(t, c, NOW)
    assert ch["1D"] == pytest.approx(ratio(331, 332)) and ch["1W"] == pytest.approx(ratio(331, 336))
    assert ch["WTD"] == pytest.approx(ratio(331, 333)) and ch["MTD"] == pytest.approx(ratio(331, 337))
    assert ch["3M"] == pytest.approx(ratio(331, 394)) and all(v < 0 for v in ch.values())
    assert set(ch) == {"1D", "WTD", "MTD", "1W", "1M", "3M"}


def test_period_changes_leaves_its_inputs_alone():
    t, c = stamps(calendar_days(END, 30)), 100.0 + np.arange(30)
    c[3] = np.nan
    t0, c0 = t.copy(), c.copy()
    period_changes(t, c, NOW)
    assert np.array_equal(t, t0) and np.array_equal(c, c0, equal_nan=True)


def test_period_changes_without_now_uses_the_last_close_year():
    t, c = stamps(calendar_days(END, 300)), 100.0 + np.arange(300)
    assert period_changes(t, c) == period_changes(t, c, NOW)


def test_ytd_is_flat_in_a_new_year_before_its_first_close():
    t, c = stamps(calendar_days(date(2026, 12, 31), 30)), 100.0 + np.arange(30)
    ch = period_changes(t, c, ny(2027, 1, 1, 12).timestamp())  # New Year's Day: no close yet this year
    assert ch["YTD"] == 0.0 and ch["1D"] == pytest.approx(ratio(129, 128))


def test_ytd_uses_new_years_eve_in_new_york_not_utc():
    # 23:30 on New Year's Eve in New York is already January 1 in UTC: it's still last year's last close.
    t = np.array([ny(2025, 12, 30).timestamp(), ny(2025, 12, 31, 23, 30).timestamp(), ny(2026, 1, 2).timestamp()])
    assert datetime.fromtimestamp(t[1], timezone.utc).year == 2026
    ch = period_changes(t, np.array([100.0, 110.0, 121.0]), ny(2026, 1, 2, 17).timestamp())
    assert ch["YTD"] == pytest.approx(10.0) and ch["1D"] == pytest.approx(10.0)


def test_ytd_close_just_after_midnight_new_york_belongs_to_the_new_year():
    t = np.array([ny(2025, 12, 31).timestamp(), ny(2026, 1, 1, 0, 30).timestamp(), ny(2026, 1, 2).timestamp()])
    ch = period_changes(t, np.array([100.0, 105.0, 121.0]), ny(2026, 1, 2, 17).timestamp())
    assert ch["YTD"] == pytest.approx(21.0)


def test_ytd_year_comes_from_new_york_time_of_now():
    # 22:00 New York on December 31 is 03:00 UTC on January 1: the year is still 2025 in New York.
    t = np.array([ny(2024, 12, 31).timestamp(), ny(2025, 6, 2).timestamp(), ny(2025, 12, 31).timestamp()])
    now = ny(2025, 12, 31, 22).timestamp()
    assert datetime.fromtimestamp(now, timezone.utc).year == 2026
    ch = period_changes(t, np.array([80.0, 90.0, 100.0]), now)
    assert ch["YTD"] == pytest.approx(25.0)


def test_no_ytd_without_a_close_from_last_year():
    t, c = stamps(calendar_days(END, 279)), 100.0 + np.arange(279) * 0.1  # starts on January 1, 2026
    assert "YTD" not in period_changes(t, c, NOW)


CAL = {"WTD", "MTD"}  # with daily closes ending Tuesday October 6: Sunday the 4th and September 30 are before them


@pytest.mark.parametrize("n, expected", [
    (0, set()), (1, set()), (2, {"1D"}),  # Monday and Tuesday only: no close before the week began
    (3, {"1D", "WTD"}), (5, {"1D", "WTD"}), (6, {"1D", "1W", "WTD"}),  # October 1 onwards: no close before the month
    (7, {"1D", "1W"} | CAL), (21, {"1D", "1W"} | CAL),
    (22, {"1D", "1W", "1M"} | CAL), (63, {"1D", "1W", "1M"} | CAL), (64, {"1D", "1W", "1M", "3M"} | CAL),
    (200, {"1D", "1W", "1M", "3M"} | CAL), (201, {"1D", "1W", "1M", "3M", "1Y"} | CAL),
    (252, {"1D", "1W", "1M", "3M", "1Y"} | CAL), (253, {"1D", "1W", "1M", "3M", "1Y"} | CAL),
    (279, {"1D", "1W", "1M", "3M", "1Y"} | CAL), (280, set(PERIODS)),
])
def test_short_series_give_only_the_periods_they_cover(n, expected):
    days = calendar_days(END, n)
    t, c = stamps(days), 100.0 + np.arange(n) * 0.1
    assert set(period_changes(t, c, NOW)) == expected


@pytest.mark.parametrize("n", [201, 230, 252, 253])
def test_new_listing_one_year_is_since_its_first_close(n):
    t, c = stamps(calendar_days(END, n)), 50.0 + np.arange(n) * 0.2
    assert period_changes(t, c, NOW)["1Y"] == pytest.approx(ratio(c[-1], c[0]))


def test_one_year_after_a_full_year_is_252_closes_back():
    t, c = stamps(calendar_days(END, 260)), 50.0 + np.arange(260) * 0.2
    assert period_changes(t, c, NOW)["1Y"] == pytest.approx(ratio(c[-1], c[7]))


def test_nan_zero_negative_and_infinite_closes_are_dropped():
    t = stamps(calendar_days(END, 6))  # Thursday October 1 to Tuesday the 6th
    ch = period_changes(t, np.array([100.0, np.nan, 0.0, -3.0, np.inf, 110.0]), NOW)
    assert ch == {"1D": pytest.approx(10.0), "WTD": pytest.approx(10.0)}  # Thursday's close is also last week's
    ch = period_changes(stamps(calendar_days(END, 3)), np.array([100.0, 105.0, np.nan]), NOW)
    assert ch == {"1D": pytest.approx(5.0), "WTD": pytest.approx(5.0)}  # a missing latest close: the one before is
    # the latest (Monday's), and Sunday's is the last one before the week
    assert period_changes(t, np.full(6, np.nan), NOW) == {}
    assert period_changes(t, np.array([np.nan, np.nan, np.nan, np.nan, np.nan, 7.0]), NOW) == {}


def test_dropped_closes_shift_the_windows_to_real_closes():
    c = 100.0 + np.arange(10)
    c[7] = np.nan  # 107 missing: a week back from 109 is now 103 (five real closes back)
    ch = period_changes(stamps(calendar_days(END, 10)), c, NOW)
    assert ch["1D"] == pytest.approx(ratio(109, 108)) and ch["1W"] == pytest.approx(ratio(109, 103))


# The first close of each period, for 300 daily closes ending END (a Tuesday): WTD from Sunday the 4th, MTD from
# September 30.
STARTS = {"1D": 298, "WTD": 297, "MTD": 293, "1W": 294, "1M": 278, "3M": 236, "1Y": 47, "YTD": 20}


@pytest.mark.parametrize("factor", [2.0, 0.5, 3.0, 1 / 3])
@pytest.mark.parametrize("k", [299, 298, 297, 295, 294, 293, 290, 279, 278, 237, 236, 100, 48, 47, 30, 21, 20, 10])
def test_a_split_like_jump_removes_only_the_periods_containing_it(factor, k):
    days = calendar_days(END, 300)
    t, base = stamps(days), 100.0 + 0.1 * np.arange(300)
    c = base.copy()
    c[k:] *= factor  # a jump between close k-1 and close k
    ch = period_changes(t, c, NOW)
    assert set(ch) == {p for p, s in STARTS.items() if s >= k}
    clean = period_changes(t, base, NOW)
    for p in ch:  # both ends on the same side of the jump: the same move as without it
        assert ch[p] == pytest.approx(clean[p], rel=1e-9)


@pytest.mark.parametrize("factor, kept", [(1.7, True), (1 / 1.7, True), (1.8, False), (1 / 1.8, False)])
def test_jump_threshold_is_one_point_seven_five(factor, kept):
    days = calendar_days(END, 300)
    c = 100.0 + 0.1 * np.arange(300)
    c[290:] *= factor
    ch = period_changes(stamps(days), c, NOW)
    assert ("1M" in ch) is kept
    if kept:
        assert ch["1M"] == pytest.approx(ratio(c[-1], c[278]))


def test_a_split_then_a_reverse_split_hide_the_long_periods():
    c = np.full(300, 100.0)
    c[100:] *= 2
    c[200:] /= 2
    ch = period_changes(stamps(calendar_days(END, 300)), c, NOW)
    assert set(ch) == {"1D", "WTD", "MTD", "1W", "1M", "3M"} and all(v == 0.0 for v in ch.values())


@pytest.mark.parametrize("now, last, wtd_from, mtd_from", [
    (ny(2026, 10, 6, 15), date(2026, 10, 6), date(2026, 10, 2), date(2026, 9, 30)),  # an ordinary Tuesday
    (ny(2026, 10, 5, 10), date(2026, 10, 2), date(2026, 10, 2), date(2026, 9, 30)),  # Monday before its close: flat
    (ny(2026, 10, 10, 12), date(2026, 10, 9), date(2026, 10, 2), date(2026, 9, 30)),  # Saturday: the week just ended
    (ny(2026, 10, 11, 21), date(2026, 10, 9), date(2026, 10, 2), date(2026, 9, 30)),  # Sunday night (Monday in UTC)
    (ny(2026, 10, 1, 10), date(2026, 9, 30), date(2026, 9, 25), date(2026, 9, 30)),  # the 1st before its close: flat
    (ny(2026, 4, 7, 17), date(2026, 4, 7), date(2026, 4, 2), date(2026, 3, 31)),  # after Good Friday: from Thursday
    (ny(2026, 4, 2, 17), date(2026, 4, 2), date(2026, 3, 27), date(2026, 3, 31)),  # the Thursday before Good Friday
    (ny(2027, 6, 1, 17), date(2027, 6, 1), date(2027, 5, 28), date(2027, 5, 28)),  # after Memorial Day, May 31
    (ny(2026, 1, 2, 17), date(2026, 1, 2), date(2025, 12, 26), date(2025, 12, 31)),  # the week of Christmas and
    # New Year's Day (both Thursdays, closed): the week from the Friday after Christmas, the month from New Year's Eve
    (ny(2026, 9, 8, 17), date(2026, 9, 8), date(2026, 9, 4), date(2026, 8, 31)),  # after Labor Day
    (ny(2026, 11, 3, 17), date(2026, 11, 3), date(2026, 10, 30), date(2026, 10, 30)),  # clocks went back on the 1st
    (ny(2026, 3, 10, 17), date(2026, 3, 10), date(2026, 3, 6), date(2026, 2, 27)),  # clocks went forward on the 8th
])
def test_week_and_month_to_date_from_the_last_close_before_they_began(now, last, wtd_from, mtd_from):
    days = trading_days(last, 300)
    c = 100.0 + 0.5 * np.arange(300)
    ch = period_changes(stamps(days), c, now.timestamp())
    assert ch["WTD"] == pytest.approx(ratio(c[-1], c[days.index(wtd_from)]), rel=1e-12)
    assert ch["MTD"] == pytest.approx(ratio(c[-1], c[days.index(mtd_from)]), rel=1e-12)
    assert (ch["WTD"] == 0.0) is (wtd_from == last) and (ch["MTD"] == 0.0) is (mtd_from == last)
    assert ch["1D"] == pytest.approx(ratio(c[-1], c[-2])) and ch["1W"] == pytest.approx(ratio(c[-1], c[-6]))
    if now.month == 1:
        assert ch["YTD"] == ch["MTD"]


def test_the_week_and_month_begin_at_midnight_new_york_not_utc():
    # A close at 23:30 on a Sunday in New York (Monday in UTC) is still before the week.
    t = np.array([ny(2026, 10, 2).timestamp(), ny(2026, 10, 4, 23, 30).timestamp(), ny(2026, 10, 6).timestamp()])
    assert datetime.fromtimestamp(t[1], timezone.utc).weekday() == 0
    ch = period_changes(t, np.array([100.0, 110.0, 121.0]), ny(2026, 10, 6, 17).timestamp())
    assert ch["WTD"] == pytest.approx(10.0)
    # 00:30 on Monday in New York is already in the week.
    t[1] = ny(2026, 10, 5, 0, 30).timestamp()
    assert period_changes(t, np.array([100.0, 110.0, 121.0]), ny(2026, 10, 6, 17).timestamp())["WTD"] == \
        pytest.approx(21.0)
    # 22:00 on Sunday in New York (Monday in UTC): this week is still the one that began on Monday the 5th.
    t = np.array([ny(2026, 10, 2).timestamp(), ny(2026, 10, 9).timestamp()])
    now = ny(2026, 10, 11, 22).timestamp()
    assert datetime.fromtimestamp(now, timezone.utc).weekday() == 0
    assert period_changes(t, np.array([100.0, 130.0]), now)["WTD"] == pytest.approx(30.0)
    # The month the same way: 22:00 on September 30 in New York is October 1 in UTC.
    t = np.array([ny(2026, 8, 31).timestamp(), ny(2026, 9, 30).timestamp()])
    now = ny(2026, 9, 30, 22).timestamp()
    assert datetime.fromtimestamp(now, timezone.utc).month == 10
    assert period_changes(t, np.array([100.0, 120.0]), now)["MTD"] == pytest.approx(20.0)
    # A close at 23:30 on September 30 in New York is still September's.
    t = np.array([ny(2026, 9, 29).timestamp(), ny(2026, 9, 30, 23, 30).timestamp(), ny(2026, 10, 2).timestamp()])
    assert period_changes(t, np.array([100.0, 110.0, 121.0]), ny(2026, 10, 2, 17).timestamp())["MTD"] == \
        pytest.approx(10.0)


def test_calendar_periods_without_now_use_the_last_closes_week_and_month():
    days = trading_days(date(2026, 10, 9), 30)  # the last close is Friday October 9
    c = 100.0 + np.arange(30)
    ch = period_changes(stamps(days), c)
    assert ch["WTD"] == pytest.approx(ratio(c[-1], c[days.index(date(2026, 10, 2))]))
    assert ch["MTD"] == pytest.approx(ratio(c[-1], c[days.index(date(2026, 9, 30))]))


def test_a_jump_this_week_hides_the_week_but_not_today():
    days = trading_days(END, 300)
    c = 100.0 + 0.1 * np.arange(300)
    c[-2:] *= 2  # a split on Monday the 5th
    ch = period_changes(stamps(days), c, NOW)
    assert set(ch) == {"1D"} and ch["1D"] == pytest.approx(ratio(c[-1], c[-2]))
    c = 100.0 + 0.1 * np.arange(300)
    c[-3:] *= 2  # on Friday the 2nd: the week (from Friday's close) is clean, the month isn't
    ch = period_changes(stamps(days), c, NOW)
    assert set(ch) == {"1D", "WTD"} and ch["WTD"] == pytest.approx(ratio(c[-1], c[-3]))


# ----- the saved closes -----

def test_closes_store_update_and_series():
    store = ClosesStore(None)
    t = np.array([10, 11, 12, 13, 14, 15]) * DAY + 14 * 3600
    store.update("AAA", t, np.array([1.0, np.nan, 0.0, -2.0, np.inf, 6.0]))
    assert store.days["AAA"] == {10: 1.0, 15: 6.0}
    store.update("AAA", np.array([15 * DAY + 3600, 16 * DAY]), np.array([6.5, 7.0]))  # a later close for day 15
    assert store.days["AAA"] == {10: 1.0, 15: 6.5, 16: 7.0}
    tt, cc = store.series("AAA")
    assert BAR_HOUR == 14 * 3600 + 30 * 60
    assert tt.tolist() == [10 * DAY + BAR_HOUR, 15 * DAY + BAR_HOUR, 16 * DAY + BAR_HOUR]
    assert cc.tolist() == [1.0, 6.5, 7.0]
    assert tt.dtype == np.int64 and cc.dtype == float
    # Extra closes {day number: close}: added in date order, replacing a saved close of the same day.
    assert store.series("AAA", {16: 8.0})[1].tolist() == [1.0, 6.5, 8.0]
    tt, cc = store.series("AAA", {18: 9.0, 12: 2.0})
    assert tt.tolist() == [d * DAY + BAR_HOUR for d in (10, 12, 15, 16, 18)]
    assert cc.tolist() == [1.0, 2.0, 6.5, 7.0, 9.0]
    assert store.days["AAA"] == {10: 1.0, 15: 6.5, 16: 7.0}  # the extras aren't kept
    for extra in (None, {}):
        assert store.series("AAA", extra)[1].tolist() == [1.0, 6.5, 7.0]
    # `before`: only the saved closes of earlier days; the extras are always added.
    assert store.series("AAA", before=16)[1].tolist() == [1.0, 6.5]
    assert store.series("AAA", before=15)[1].tolist() == [1.0]
    assert store.series("AAA", before=17)[1].tolist() == [1.0, 6.5, 7.0]
    tt, cc = store.series("AAA", before=10)
    assert tt.size == 0 and cc.size == 0 and tt.dtype == np.int64
    tt, cc = store.series("AAA", {15: 6.0, 16: 8.0}, before=15)
    assert tt.tolist() == [d * DAY + BAR_HOUR for d in (10, 15, 16)] and cc.tolist() == [1.0, 6.0, 8.0]
    assert store.series("AAA", {5: 0.5, 30: 3.0}, before=0)[1].tolist() == [0.5, 3.0]
    tt, cc = store.series("NOPE")
    assert tt.size == 0 and cc.size == 0
    store.update("FLT", np.array([20.5 * DAY, 21.9 * DAY]), np.array([3.0, 4.0]))  # float times work too
    assert store.days["FLT"] == {20: 3.0, 21: 4.0} and all(type(d) is int for d in store.days["FLT"])
    tt, cc = store.series("NOPE", {20: 5.0})
    assert tt.tolist() == [20 * DAY + BAR_HOUR] and cc.tolist() == [5.0]


def test_series_times_fall_on_their_new_york_date():
    """Day numbers are UTC dates; the series puts each close in the New York morning, so calendar periods (measured
    in New York time) see the right date, summer and winter."""
    store = ClosesStore(None)
    days = [date(2026, 1, 2), date(2026, 3, 6), date(2026, 3, 9), date(2026, 7, 1), date(2026, 10, 30),
            date(2026, 11, 2), date(2026, 12, 31)]
    store.update("AAA", stamps(days, 9, 30), np.arange(1.0, 8.0))  # Yahoo's daily bars start at the open
    assert sorted(store.days["AAA"]) == [(d - EPOCH).days for d in days]
    tt, _ = store.series("AAA")
    assert [datetime.fromtimestamp(t, NEW_YORK).date() for t in tt.tolist()] == days
    assert all(9 <= datetime.fromtimestamp(t, NEW_YORK).hour <= 10 for t in tt.tolist())


def test_closes_store_keeps_the_latest_days_only():
    store = ClosesStore(None)
    store.update("AAA", np.arange(350) * DAY, np.arange(350) + 1.0)
    days = store.days["AAA"]
    assert len(days) == KEEP_DAYS and min(days) == 50 and max(days) == 349 and days[50] == 51.0
    store.update("AAA", np.arange(10) * DAY, np.ones(10))  # older closes arriving late are dropped again
    assert len(store.days["AAA"]) == KEEP_DAYS and min(store.days["AAA"]) == 50
    store.update("AAA", np.arange(350, 360) * DAY, np.ones(10))
    assert len(store.days["AAA"]) == KEEP_DAYS and min(store.days["AAA"]) == 60 and max(store.days["AAA"]) == 359


def test_closes_store_round_trip(tmp_path):
    path = tmp_path / "deep" / "er" / "trend-closes.npz"
    store = ClosesStore(path)
    assert store.days == {}
    store.update("AAA", np.arange(10) * DAY, 100.0 + np.arange(10) * 1.2345)
    store.update("BRK-B", np.arange(5, 15) * DAY, 400000.0 + np.arange(10))
    store.update("TINY", np.array([3 * DAY]), np.array([0.000123]))
    store.save()
    assert [p.name for p in path.parent.iterdir()] == ["trend-closes.npz"]  # no temp files left behind
    again = ClosesStore(path)
    assert set(again.days) == {"AAA", "BRK-B", "TINY"}
    assert all(type(s) is str for s in again.days) and all(type(d) is int for d in again.days["AAA"])
    for s, series in store.days.items():
        assert set(again.days[s]) == set(series)
        for d, c in series.items():
            assert again.days[s][d] == pytest.approx(c, rel=1e-6)  # saved as float32
    tt, cc = again.series("AAA", {10: 120.0})
    assert len(tt) == 11 and cc[-1] == 120.0 and tt[-1] == 10 * DAY + BAR_HOUR


def test_saving_keeps_the_latest_days_across_symbols(tmp_path):
    path = tmp_path / "c.npz"
    store = ClosesStore(path)
    store.update("OLD", np.arange(0, 50) * DAY, np.ones(50))
    store.update("AAA", np.arange(0, 300) * DAY, np.full(300, 2.0))
    store.update("BBB", np.arange(100, 400) * DAY, np.full(300, 3.0))
    store.save()
    again = ClosesStore(path)
    assert sorted(again.days["AAA"]) == list(range(100, 300))
    assert sorted(again.days["BBB"]) == list(range(100, 400))
    assert again.days["OLD"] == {} and again.series("OLD")[0].size == 0


def test_saving_nothing_and_saving_nowhere(tmp_path):
    ClosesStore(None).save()  # no file: nothing to do
    path = tmp_path / "c.npz"
    ClosesStore(path).save()
    assert ClosesStore(path).days == {}


@pytest.mark.parametrize("content", [b"", b"not a zip at all", b"PK\x03\x04garbage", b"\x93NUMPY broken"])
def test_unreadable_saved_closes_start_over(tmp_path, caplog, content):
    path = tmp_path / "c.npz"
    path.write_bytes(content)
    assert ClosesStore(path).days == {}
    assert "unreadable" in caplog.text


def test_saved_closes_that_need_pickle_are_refused(tmp_path):
    path = tmp_path / "c.npz"
    np.savez(path, symbols=np.array([{"evil": 1}], dtype=object), days=np.array([1]), grid=np.ones((1, 1)))
    assert ClosesStore(path).days == {}


def test_saved_closes_missing_an_array_start_over(tmp_path):
    path = tmp_path / "c.npz"
    np.savez(path, symbols=np.array(["A"]), days=np.array([1]))
    assert ClosesStore(path).days == {}


@pytest.mark.parametrize("symbols, days, grid", [
    (["A", "B"], [1, 2, 3], np.ones((1, 3), np.float32)),  # fewer rows than symbols
    (["A"], [1, 2, 3], np.ones((2, 3), np.float32)),  # more rows than symbols
    (["A"], [1, 2, 3], np.ones((1, 2), np.float32)),  # fewer columns than days
    (["A"], [1, 2], np.ones((1, 3), np.float32)),  # more columns than days
    (["A"], [1, 2, 3], np.ones(3, np.float32)),  # one dimension
    (["A"], [1, 2, 3], np.ones((1, 3, 1), np.float32)),  # three
    (["A"], [1], np.float32(1.0)),  # none
])
def test_saved_closes_that_do_not_line_up_start_over(tmp_path, caplog, symbols, days, grid):
    path = tmp_path / "trend-closes.npz"
    np.savez_compressed(path, symbols=np.array(symbols), days=np.array(days), grid=grid)
    store = ClosesStore(path)  # no exception: the bot still starts
    assert store.days == {} and "unreadable" in caplog.text


def test_saved_closes_load_all_or_nothing(tmp_path, caplog):
    path = tmp_path / "trend-closes.npz"
    # The first row is fine; the second has a day that isn't a number: nothing is kept, not half the file.
    np.savez_compressed(path, symbols=np.array(["A", "B"]), days=np.array(["1", "x"]),
                        grid=np.array([[5.0, np.nan], [np.nan, 6.0]], np.float32))
    assert ClosesStore(path).days == {} and "unreadable" in caplog.text


def test_loading_drops_closes_that_are_not_positive(tmp_path):
    path = tmp_path / "trend-closes.npz"
    np.savez_compressed(path, symbols=np.array(["A", "B"]), days=np.arange(10, 16),
                        grid=np.array([[1.0, 0.0, -2.0, np.nan, np.inf, 3.0], [-1.0] * 6], np.float32))
    store = ClosesStore(path)
    assert store.days == {"A": {10: 1.0, 15: 3.0}, "B": {}}


def test_a_failed_save_keeps_the_old_file_and_no_temp_files(tmp_path, monkeypatch):
    path = tmp_path / "c.npz"
    store = ClosesStore(path)
    store.update("AAA", np.arange(3) * DAY, np.array([1.0, 2.0, 3.0]))
    store.save()
    before = path.read_bytes()
    store.update("AAA", np.array([3 * DAY]), np.array([4.0]))

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(T.np, "savez_compressed", boom)
    with pytest.raises(RuntimeError):
        store.save()
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["c.npz"]


# ----- snapshots, coins and screener rows -----

def test_snapshot_movers_best_first_with_names_and_prices():
    snap = Snapshot(NOW, changes={"A": {"1D": 1.0, "1W": 5.0}, "B": {"1D": 3.0}, "C": {"1D": -2.0}, "D": {"1W": 1.0}},
                    prices={"A": 10.0, "B": 20.0}, names={"B": "Beta"})
    assert [m.symbol for m in snap.movers("1D")] == ["B", "A", "C"]
    b = snap.movers("1D")[0]
    assert (b.name, b.price, b.change, b.volume, b.cap) == ("Beta", 20.0, 3.0, None, None)
    c = snap.movers("1D")[-1]
    assert (c.name, c.price) == ("C", 0.0)  # no name or price known: the symbol and zero
    assert [m.symbol for m in snap.movers("1D", ["C", "ZZZ", "A", "D"])] == ["A", "C"]
    assert [m.symbol for m in snap.movers("1W")] == ["A", "D"]
    assert snap.movers("3M") == [] and snap.movers("1D", []) == []


def test_snapshots_do_not_share_their_lists():
    a, b = Snapshot(1.0), Snapshot(2.0)
    a.day_gainers.append(Mover("X", "X", 1.0, 1.0))
    a.changes["X"] = {"1D": 1.0}
    a.errors.append("boom")
    assert b.day_gainers == [] and b.changes == {} and b.errors == [] and b.coins == []
    assert (b.day_source, b.periods_source) == ("", "")


def test_snapshot_movers_ties_keep_the_pool_order():
    snap = Snapshot(NOW, changes={s: {"1D": 2.0} for s in ("X", "Y", "Z")})
    assert [m.symbol for m in snap.movers("1D", ["Z", "X", "Y"])] == ["Z", "X", "Y"]


def test_snapshot_breadth_per_period():
    snap = Snapshot(NOW, changes={"A": {"1D": 1.0, "1W": -1.0}, "B": {"1D": -2.0, "1W": -3.0}, "C": {"1D": 0.0},
                                  "D": {"1W": 4.0}, "E": {"1D": 0.5}})
    members = ["A", "B", "C", "D", "E", "MISSING"]
    assert snap.breadth(members) == (2, 1)  # a flat stock is neither up nor down
    assert snap.breadth(members, "1W") == (1, 2)
    assert snap.breadth(members, "1Y") == (0, 0) and snap.breadth([]) == (0, 0)
    assert snap.breadth(["A", "A"]) == (2, 0)  # counted as given


def test_coin_movers_keep_real_liquid_coins_with_the_period():
    snap = Snapshot(NOW, coins=[
        coin("BTC", "Bitcoin", 60000, c24=2.0, c7=4.0, c30=None, volume=3e10, cap=1.2e12),
        coin("ETH", "Ethereum", 3000, c24=-3.0, c30=12.0, volume=1e10),
        coin("SOL", "Solana", 150, c24=7.5, c30=-8.0, volume=MIN_COIN_VOLUME),  # exactly the floor is enough
        coin("USDT", "Tether", 1.0, c24=0.01, c7=0.0),
        coin("XAUT", "Tether Gold", 2400, c24=1.0),  # gold-backed: in STABLES
        coin("WBTC", "Wrapped Bitcoin", 60000, c24=2.1),
        coin("STETH", "Lido Staked Ether", 3000, c24=-2.9),
        coin("TSLAX", "Tesla xStock", 250, c24=9.0),
        coin("NVDAON", "NVIDIA (Ondo Tokenized)", 180, c24=4.0),
        coin("THIN", "Thin Coin", 5.0, c24=50.0, volume=MIN_COIN_VOLUME - 1),
        coin("NOV", "No Volume", 5.0, c24=40.0, volume=None),
        coin("PEG", "Some Dollar", 1.002, c24=0.1, c7=0.3),  # an unlisted stablecoin
    ])
    assert [m.symbol for m in snap.coin_movers("1D")] == ["SOL-USD", "BTC-USD", "ETH-USD"]
    btc = snap.coin_movers("1D")[1]
    assert (btc.name, btc.price, btc.change, btc.volume, btc.cap) == ("Bitcoin", 60000, 2.0, 3e10, 1.2e12)
    assert [m.symbol for m in snap.coin_movers("1M")] == ["ETH-USD", "SOL-USD"]  # BTC has no 30-day change
    assert [m.symbol for m in snap.coin_movers("1W")] == ["ETH-USD", "SOL-USD", "BTC-USD"]
    assert [m.change for m in snap.coin_movers("1Y")] == [50.0, 50.0, 50.0]
    assert snap.coin_movers("3M") == [] and snap.coin_movers("YTD") == [] and snap.coin_movers("5Y") == []
    # CoinGecko has no calendar week or month: this week is the last 7 days, this month the last 30.
    assert snap.coin_movers("WTD") == snap.coin_movers("1W") and snap.coin_movers("MTD") == snap.coin_movers("1M")


def test_period_names_and_crypto_windows():
    assert list(PERIODS) == ["1D", "WTD", "MTD", "1W", "1M", "3M", "YTD", "1Y"]
    assert PERIODS == {"1D": "today", "WTD": "this week", "MTD": "this month", "1W": "over 5 sessions",
                       "1M": "over 21 sessions", "3M": "over 3 months", "YTD": "year to date", "1Y": "over a year"}
    assert CRYPTO_PERIODS == {"1D": "change_24h", "1W": "change_7d", "1M": "change_30d", "1Y": "change_1y"}
    assert CRYPTO_ALIASES == {"WTD": "1W", "MTD": "1M"}
    assert CRYPTO_LABELS == {"1D": "in 24 hours", "1W": "over 7 days", "1M": "over 30 days", "1Y": "over a year"}
    assert set(BARS_BACK) | {"WTD", "MTD", "YTD"} == set(PERIODS)


@pytest.mark.parametrize("c, real", [
    (coin("BTC", "Bitcoin", 60000), True),
    (coin("USDT", "Tether", 1.0), False),
    (coin("usdc", "USDC", 1.0, c7=3.0), False),  # tickers are compared in capitals
    (coin("PAXG", "PAX Gold", 2400, c7=3.0), False),
    (coin("WBTC", "Wrapped Bitcoin", 60000), False),
    (coin("WEETH", "Wrapped eETH", 3200), False),
    (coin("STETH", "Lido Staked Ether", 3000), False),
    (coin("RSETH", "Kelp DAO Restaked ETH", 3000), False),
    (coin("JITOSOL", "Jito Liquid Staking SOL", 180), False),
    (coin("AAPLX", "Apple Tokenised Stock", 230), False),
    (coin("TSLAX", "Tesla xStock", 250), False),
    (coin("BUSDC", "Bridged USDC", 1.0, c7=2.0), False),
    (coin("PEG", "Mystery Dollar", 1.001, c7=0.3), False),  # $1 and not moving: a stablecoin by behaviour
    (coin("PEG", "Mystery Dollar", 1.001, c7=None), False),
    (coin("ONE", "Coin At A Dollar", 1.0, c7=5.0), True),  # trades near $1 but moves
    (coin("ONE", "Coin At A Dollar", 1.0, c7=-1.6), True),
    (coin("LOW", "Cheap", 0.96, c7=0.0), True),
    (coin("HI", "Pricey", 1.031, c7=0.0), True),
    (coin("NOPRICE", "No Price", None), True),
    (coin("NONAME", None, 5.0), True),
    (coin("ONDO", "Ondo", 0.92, c7=-6.0), True),  # the ONDO token is a real coin
    (coin("AAPLON", "Apple (Ondo Tokenized)", 230), False),  # Ondo's tokenized stocks aren't
])
def test_real_coin(c, real):
    assert real_coin(c) is real


def test_the_ondo_token_is_a_real_coin():
    assert real_coin(coin("ONDO", "Ondo", 0.92, c7=-6.0))
    assert not T.DERIVED.search("Ondo") and not T.DERIVED.search("Ondo Finance")
    snap = Snapshot(NOW, coins=[coin("ONDO", "Ondo", 0.92, c24=4.0, c7=-6.0)])
    assert [m.symbol for m in snap.coin_movers("1D")] == ["ONDO-USD"]


def test_stables_are_upper_case_tickers():
    assert all(s == s.upper() for s in STABLES) and {"USDT", "USDC", "DAI"} <= STABLES


def screen_row(sym, price, chg, vol=1e6, cap=5e9, name=None, source=None):
    row = {"symbol": sym, "shortName": name or f"{sym} Inc", "regularMarketPrice": price,
           "regularMarketChangePercent": chg, "regularMarketVolume": vol, "marketCap": cap}
    if source:
        row["source"] = source
    return row


def test_movers_from_screener_skips_rows_missing_what_it_needs():
    rows_ = [
        screen_row("AAA", 10, 5.5, 1e6, 3e9, "Alpha"),
        {"symbol": "BBB", "longName": "Beta Long Name", "regularMarketPrice": "12.5", "regularMarketChangePercent": "-1"},
        {"symbol": "CCC", "shortName": "", "longName": "", "regularMarketPrice": 1.0, "regularMarketChangePercent": 0},
        {"symbol": "", "regularMarketPrice": 1.0, "regularMarketChangePercent": 1.0},
        {"regularMarketPrice": 1.0, "regularMarketChangePercent": 1.0},
        {"symbol": None, "regularMarketPrice": 1.0, "regularMarketChangePercent": 1.0},
        {"symbol": "DDD", "regularMarketPrice": None, "regularMarketChangePercent": 1.0},
        {"symbol": "EEE", "regularMarketPrice": 5.0},
        {},
    ]
    out = movers_from_screener(rows_)
    assert [(m.symbol, m.name, m.price, m.change, m.volume, m.cap) for m in out] == [
        ("AAA", "Alpha", 10.0, 5.5, 1e6, 3e9), ("BBB", "Beta Long Name", 12.5, -1.0, None, None),
        ("CCC", "CCC", 1.0, 0.0, None, None)]
    assert all(type(m.price) is float and type(m.change) is float for m in out)
    assert movers_from_screener([]) == []


def test_screener_rows_with_text_for_numbers_are_skipped():
    out = movers_from_screener([screen_row("AAA", "N/A", 1.0), screen_row("BBB", 10.0, "—"),
                                screen_row("CCC", 5.0, 2.0)])
    assert [m.symbol for m in out] == ["CCC"]


def test_screener_rows_of_any_shape_never_raise():
    rows_ = [
        None, "AAA", 5, ["AAA", 1.0, 2.0],  # not rows at all
        screen_row("NAN", float("nan"), 1.0), screen_row("INF", 1.0, float("inf")), screen_row("NEG", 2.0, "--"),
        {"symbol": 123, "regularMarketPrice": 1.0, "regularMarketChangePercent": 1.0},  # a symbol that isn't text
        {"symbol": ["X"], "regularMarketPrice": 1.0, "regularMarketChangePercent": 1.0},
        screen_row("FMT", "$1,234.50", "+2.5%", vol="1,000", cap="N/A", name=None),  # Nasdaq-style text numbers
        {"symbol": "ODD", "shortName": 42, "regularMarketPrice": 3, "regularMarketChangePercent": -1,
         "regularMarketVolume": "lots", "marketCap": float("nan")},
    ]
    out = movers_from_screener(rows_)
    assert [(m.symbol, m.name, m.price, m.change, m.volume, m.cap) for m in out] == [
        ("FMT", "FMT Inc", 1234.5, 2.5, 1000.0, None), ("ODD", "42", 3.0, -1.0, None, None)]
    assert all(type(m.price) is float and type(m.change) is float for m in out)
    assert movers_from_screener(None) == [] and movers_from_screener([None, {}]) == []


# ----- the trends desk (MarketData, CoinGecko and the clock faked) -----

class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(T, "time", c)  # the trends module's time.time()
    return c


class SparkYahoo:
    def __init__(self, bars, fail_symbols=()):
        self.bars = bars
        self.fail_symbols = set(fail_symbols)
        self.calls = []

    async def spark_bars(self, symbols, range_="1y", interval="1d"):
        self.calls.append((list(symbols), range_, interval))
        await asyncio.sleep(0)
        if len(symbols) > SPARK_BATCH:
            raise ValueError("spark takes at most 20 symbols")
        if self.fail_symbols & set(symbols):
            raise YahooError("Yahoo HTTP 500", 500)
        return {s: self.bars[s] for s in symbols if s in self.bars}


def default_screens(source=None):
    return {"day_gainers": [screen_row("UPA", 10, 12.5, source=source), screen_row("UPB", 20, 8.0, source=source)],
            "day_losers": [screen_row("DNA", 30, -9.0, source=source)],
            "most_actives": [screen_row("ACT", 40, 0.5, vol=9e8, source=source)]}


class FakeData:
    def __init__(self, bars=None, quotes=None, screens=None, yahoo_ok=True, fail_spark=(), fail_screens=(),
                 quotes_error=None, outage=None):
        self.yahoo = SparkYahoo(bars or {}, fail_spark)
        self.yahoo_ok = yahoo_ok
        self.backup = quotes or {}
        self.screens = screens if screens is not None else default_screens()
        self.fail_screens = set(fail_screens)
        self.quotes_error = quotes_error
        self.quote_calls = []
        self.screen_calls = []
        self._outage = outage

    async def screener(self, scr_id, count=25):
        self.screen_calls.append((scr_id, count))
        await asyncio.sleep(0)
        if scr_id in self.fail_screens:
            raise YahooError(f"No {scr_id} list: Yahoo HTTP 503 " + "x" * 300, 503)
        return [dict(r) for r in self.screens.get(scr_id, [])]

    async def quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        if self.quotes_error:
            raise self.quotes_error
        return {s: self.backup[s] for s in symbols if s in self.backup}

    def outage(self):
        return self._outage

    async def close(self):
        pass


class FakeSources:
    def __init__(self, coins=None, error=None):
        self.coins = coins if coins is not None else [coin("BTC", "Bitcoin", 60000, c24=2.0),
                                                      coin("ETH", "Ethereum", 3000, c24=-1.0)]
        self.error = error
        self.calls = []
        self.inflight = self.max_inflight = 0

    async def top_coins(self, count=250):
        self.calls.append(count)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)
            if self.error:
                raise self.error
            return list(self.coins[:count])
        finally:
            self.inflight -= 1


def make_directory(extra=()):
    return Directory([Listing("AAA", "Alpha Corp", STOCKS, "stock", 5e10, ("sp500",)),
                      Listing("BBB", "Beta Inc", STOCKS, "stock", 6e10, ("sp500", "ndx100")),
                      Listing("CCC", "Gamma Co", STOCKS, "stock", 7e10, ("sp500",)),
                      Listing("DDD", "Delta Ltd", STOCKS, "stock", 8e10, ("ndx100",)),
                      Listing("ZZZ", "In No Index", STOCKS, "stock", 1e9),
                      Listing("XLK", "Technology Select Sector SPDR", STOCKS, "etf", 7e10)] + list(extra))


UNIVERSE = ["AAA", "BBB", "CCC", "DDD"] + list(SECTORS) + MAJOR_ETFS


def spark(symbol, n=300, end=END, drift=0.0005, name=None):
    days = trading_days(end, n)
    t = stamps(days, 9, 30)  # Yahoo's daily bars start at the open
    i = np.arange(n)
    c = 100 * np.exp(drift * i + 0.01 * np.sin(i))
    return Bars(symbol, t, c, c.copy(), c.copy(), c.copy(), np.zeros(n), {"shortName": name} if name else {})


def all_bars(symbols=UNIVERSE, skip=(), end=END, n=300):
    return {s: spark(s, n=n, end=end, drift=0.0001 * (k - 20), name="Alpha (Yahoo)" if s == "AAA" else None)
            for k, s in enumerate(symbols) if s not in skip}


def live(symbol, price, change, t=NOW, name=None, source="Nasdaq"):
    return Quote(symbol=symbol, name=name or symbol, price=price, prev_close=price / (1 + change / 100),
                 change_pct=change, time=t, source=source)


def test_universe_and_index_members():
    desk = TrendsDesk(FakeData(), FakeSources(), make_directory())
    assert desk.universe() == UNIVERSE and len(desk.universe()) == 47
    assert "ZZZ" not in desk.universe()
    assert desk.index_members("sp500") == ["AAA", "BBB", "CCC"] and desk.index_members("ndx100") == ["BBB", "DDD"]
    assert desk.closes.path is None
    bare = TrendsDesk(FakeData(), None, None)
    assert bare.universe() == list(SECTORS) + MAJOR_ETFS
    assert bare.index_members("sp500") == []


def test_refresh_with_yahoo_answering(tmp_path, clock):
    bars = all_bars(skip={"XLRE", "JETS"})
    data = FakeData(bars=bars, quotes={"XLRE": live("XLRE", 41.0, 1.5, name="Real Estate SPDR")})
    sources = FakeSources()
    desk = TrendsDesk(data, sources, make_directory(), tmp_path)
    snap = asyncio.run(desk.refresh())
    assert desk.last is snap and snap.at == NOW
    # A year of daily closes, at most 20 symbols a call.
    assert len(data.yahoo.calls) == 3 and all(len(b) <= SPARK_BATCH for b, _, _ in data.yahoo.calls)
    assert [s for b, _, _ in data.yahoo.calls for s in b] == UNIVERSE
    assert {(r, i) for _, r, i in data.yahoo.calls} == {("1y", "1d")}
    for s in bars:
        assert snap.changes[s] == period_changes(bars[s].t, bars[s].close, NOW)
        assert snap.prices[s] == bars[s].close[-1]
    assert set(snap.changes["AAA"]) == set(PERIODS)
    assert snap.names["AAA"] == "Alpha (Yahoo)"  # Yahoo's name first
    assert snap.names["BBB"] == "Beta Inc"  # then the symbol list's
    assert snap.names["SPY"] == "SPY"  # then the symbol
    # Only the symbols Yahoo left out are asked of the backup; with nothing saved, today is the quote's change.
    assert data.quote_calls == [["XLRE", "JETS"]]
    assert snap.changes["XLRE"] == {"1D": 1.5} and snap.prices["XLRE"] == 41.0
    assert snap.names["XLRE"] == "Real Estate SPDR"
    assert "JETS" not in snap.changes
    assert snap.periods_source == "Yahoo Finance"
    # Today's lists and the coins.
    assert [m.symbol for m in snap.day_gainers] == ["UPA", "UPB"] and [m.symbol for m in snap.day_losers] == ["DNA"]
    assert [m.symbol for m in snap.most_active] == ["ACT"] and snap.most_active[0].volume == 9e8
    assert sorted(data.screen_calls) == [("day_gainers", 25), ("day_losers", 25), ("most_actives", 25)]
    assert snap.day_source == "Yahoo Finance"
    assert [c.symbol for c in snap.coins] == ["BTC", "ETH"] and sources.calls == [250]
    assert snap.errors == []
    saved = ClosesStore(tmp_path / "trend-closes.npz")
    assert set(saved.days) == set(bars) and len(saved.days["AAA"]) == KEEP_DAYS


def test_screener_lists_from_nasdaq_say_so(tmp_path, clock):
    data = FakeData(bars=all_bars(), screens=default_screens(source="Nasdaq (last close)"))
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.day_source == "Nasdaq (last close)"
    assert [m.name for m in snap.day_gainers] == ["UPA Inc", "UPB Inc"]


def test_no_screener_lists_leave_the_day_source_blank(tmp_path, clock):
    data = FakeData(bars=all_bars(), fail_screens={"day_gainers", "day_losers", "most_actives"})
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.day_source == "" and not snap.day_gainers and not snap.day_losers and not snap.most_active
    assert len(snap.errors) == 3 and len(snap.changes) == len(UNIVERSE)


def test_refresh_without_yahoo_uses_saved_closes_and_backup_quotes(tmp_path, clock):
    yesterday = date(2026, 10, 5)
    first = FakeData(bars=all_bars(skip={"XLRE"}, end=yesterday))
    asyncio.run(TrendsDesk(first, FakeSources(), make_directory(), tmp_path).refresh())
    path = tmp_path / "trend-closes.npz"
    saved_at = path.stat().st_mtime_ns
    days = trading_days(yesterday, 300)
    c = np.float32(first.yahoo.bars["AAA"].close).astype(float)  # what was saved

    price = round(c[-1] * 1.007, 4)
    down = FakeData(yahoo_ok=False, quotes={"AAA": live("AAA", price, 0.7, name="Alpha (Nasdaq)"),
                                            "XLRE": live("XLRE", 41.0, -1.25)})
    snap = asyncio.run(TrendsDesk(down, FakeSources(), make_directory(), tmp_path).refresh())
    assert down.yahoo.calls == []  # Yahoo is resting: not asked
    assert down.quote_calls == [UNIVERSE]
    ch = snap.changes["AAA"]
    assert set(ch) == set(PERIODS)
    assert ch["1D"] == 0.7  # the quote's own change, not one worked out from the saved closes
    assert ch["WTD"] == pytest.approx(ratio(price, c[days.index(date(2026, 10, 2))]), rel=1e-9)  # last Friday
    assert ch["MTD"] == pytest.approx(ratio(price, c[days.index(date(2026, 9, 30))]), rel=1e-9)
    assert ch["1W"] == pytest.approx(ratio(price, c[-5]), rel=1e-9)
    assert ch["1M"] == pytest.approx(ratio(price, c[-21]), rel=1e-9)
    assert ch["3M"] == pytest.approx(ratio(price, c[-63]), rel=1e-9)
    assert ch["1Y"] == pytest.approx(ratio(price, c[-252]), rel=1e-9)
    assert ch["YTD"] == pytest.approx(ratio(price, c[days.index(date(2025, 12, 31))]), rel=1e-9)
    assert snap.prices["AAA"] == price and snap.names["AAA"] == "Alpha (Nasdaq)"
    assert snap.changes["XLRE"] == {"1D": -1.25}  # nothing saved for it: the quote's own change
    assert set(snap.changes) == {"AAA", "XLRE"}
    assert snap.periods_source == "saved closes + Nasdaq"
    assert path.stat().st_mtime_ns == saved_at  # nothing new to save


def test_todays_backup_price_replaces_todays_saved_close(tmp_path, clock):
    first = FakeData(bars=all_bars(end=END))  # the saved closes include today's (still moving) close
    asyncio.run(TrendsDesk(first, FakeSources(), make_directory(), tmp_path).refresh())
    c = np.float32(first.yahoo.bars["BBB"].close).astype(float)
    price = round(c[-1] * 1.01, 4)
    down = FakeData(yahoo_ok=False, quotes={"BBB": live("BBB", price, 0.3)})
    snap = asyncio.run(TrendsDesk(down, FakeSources(), make_directory(), tmp_path).refresh())
    ch = snap.changes["BBB"]
    assert snap.prices["BBB"] == price and ch["1D"] == 0.3
    # Today's and yesterday's saved closes give way to the quote's price and previous close.
    assert ch["1W"] == pytest.approx(ratio(price, c[-6]), rel=1e-9)
    assert ch["WTD"] == pytest.approx(ratio(price, c[-3]), rel=1e-9)  # Friday October 2
    assert ch["MTD"] == pytest.approx(ratio(price, c[-5]), rel=1e-9)  # September 30
    assert set(ch) == set(PERIODS)


def test_backup_quote_without_a_time_counts_as_now(tmp_path, clock):
    first = FakeData(bars=all_bars(end=date(2026, 10, 5)))
    asyncio.run(TrendsDesk(first, FakeSources(), make_directory(), tmp_path).refresh())
    c = np.float32(first.yahoo.bars["CCC"].close).astype(float)
    price = round(c[-1] * 1.003, 4)
    down = FakeData(yahoo_ok=False, quotes={"CCC": live("CCC", price, 0.3, t=0.0)})
    snap = asyncio.run(TrendsDesk(down, FakeSources(), make_directory(), tmp_path).refresh())
    ch = snap.changes["CCC"]
    assert ch["1D"] == 0.3 and set(ch) == set(PERIODS)  # today's session: the saved closes line up
    assert ch["1W"] == pytest.approx(ratio(price, c[-5]), rel=1e-9)
    assert ch["WTD"] == pytest.approx(ratio(price, c[-2]), rel=1e-9)


def test_stale_saved_closes_do_not_become_a_one_day_move(tmp_path, clock):
    first = FakeData(bars=all_bars(end=date(2026, 9, 25)))  # the saved closes stop a week and a half ago
    asyncio.run(TrendsDesk(first, FakeSources(), make_directory(), tmp_path).refresh())
    last = float(np.float32(first.yahoo.bars["AAA"].close[-1]))
    down = FakeData(yahoo_ok=False, quotes={"AAA": live("AAA", last * 1.05, 0.8)})
    snap = asyncio.run(TrendsDesk(down, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.changes["AAA"] == {"1D": 0.8}  # a gap in the saved closes: no longer periods either
    assert snap.prices["AAA"] == last * 1.05


def test_backup_quotes_without_a_price_or_change_are_skipped(tmp_path, clock):
    quotes = {"AAA": live("AAA", 0.0, 1.0), "BBB": live("BBB", 10.0, 1.0)}
    quotes["BBB"].change_pct = None
    data = FakeData(yahoo_ok=False, quotes=quotes)
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.changes == {} and snap.prices == {}
    assert not (tmp_path / "trend-closes.npz").exists()  # nothing from Yahoo: nothing saved


# ----- backup_changes: the periods from a backup quote and the saved closes -----

def closes_desk(last_day, n=300, symbol="AAA"):
    """A trends desk whose saved closes for `symbol` are n trading days ending last_day: 100, 100.25, 100.5..."""
    desk = TrendsDesk(FakeData(), FakeSources(), make_directory())
    days = trading_days(last_day, n)
    c = 100.0 + 0.25 * np.arange(n)
    desk.closes.update(symbol, stamps(days, 9, 30), c)  # Yahoo's daily bars start at the open
    return desk, days, c


def backup_quote(price, prev_close, change, t=NOW, state="REGULAR", symbol="AAA"):
    return Quote(symbol=symbol, name=symbol, price=price, prev_close=prev_close, change_pct=change, time=t,
                 market_state=state, source="Nasdaq")


def test_backup_changes_exact_numbers():
    desk, days, c = closes_desk(date(2026, 10, 5))  # saved up to Monday; the quote is Tuesday's
    # The quote's change (2.5%) deliberately isn't price / previous close: 1D is the quote's own all the same.
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, 176.0, 2.5), NOW)
    s = list(c[:-1]) + [176.0, 180.0]  # the saved closes up to Friday, the quote's previous close on Monday, today
    assert closes.tolist() == s
    assert ch == {
        "1D": 2.5,
        "WTD": pytest.approx(ratio(180.0, c[days.index(date(2026, 10, 2))]), rel=1e-12),  # from Friday's close
        "MTD": pytest.approx(ratio(180.0, c[days.index(date(2026, 9, 30))]), rel=1e-12),
        "1W": pytest.approx(ratio(180.0, s[-6]), rel=1e-12),
        "1M": pytest.approx(ratio(180.0, s[-22]), rel=1e-12),
        "3M": pytest.approx(ratio(180.0, s[-64]), rel=1e-12),
        "1Y": pytest.approx(ratio(180.0, s[-253]), rel=1e-12),
        "YTD": pytest.approx(ratio(180.0, c[days.index(date(2025, 12, 31))]), rel=1e-12),
    }
    assert s[-6] == c[-5] and s[-253] == c[-252]
    assert desk.closes.days["AAA"][(date(2026, 10, 5) - EPOCH).days] == c[-1]  # the saved closes are left alone


def test_backup_changes_put_the_previous_close_on_the_previous_trading_day():
    # Monday: the previous close is Friday's, so this week so far is the quote's price against it.
    desk, days, c = closes_desk(date(2026, 10, 2))
    now = ny(2026, 10, 5, 15).timestamp()
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, 175.0, 1.0, t=now), now)
    assert closes.tolist() == list(c[:-1]) + [175.0, 180.0]
    assert ch["WTD"] == pytest.approx(ratio(180.0, 175.0)) and ch["1D"] == 1.0
    assert ch["MTD"] == pytest.approx(ratio(180.0, c[days.index(date(2026, 9, 30))]))
    # The Tuesday after Labor Day: the previous close is Friday's, the saved closes must reach Thursday.
    desk, days, c = closes_desk(date(2026, 9, 4))
    now = ny(2026, 9, 8, 15).timestamp()
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, 175.0, 1.0, t=now), now)
    assert closes.tolist() == list(c[:-1]) + [175.0, 180.0]
    assert ch["WTD"] == pytest.approx(ratio(180.0, 175.0))
    assert ch["MTD"] == pytest.approx(ratio(180.0, c[days.index(date(2026, 8, 31))]))
    assert set(ch) == set(PERIODS)


@pytest.mark.parametrize("now, t, last_saved, wtd_from", [
    # A weekend: Friday's closing quote, a quote stamped Saturday, or one with no time at all.
    (ny(2026, 10, 10, 12), ny(2026, 10, 9, 16), date(2026, 10, 9), date(2026, 10, 2)),
    (ny(2026, 10, 10, 12), ny(2026, 10, 10, 10), date(2026, 10, 9), date(2026, 10, 2)),
    (ny(2026, 10, 10, 12), None, date(2026, 10, 9), date(2026, 10, 2)),
    (ny(2026, 10, 11, 21), None, date(2026, 10, 9), date(2026, 10, 2)),
    # Thanksgiving: the quote is Wednesday's close.
    (ny(2026, 11, 26, 12), ny(2026, 11, 26, 12), date(2026, 11, 25), date(2026, 11, 20)),
    # Good Friday: Thursday's close.
    (ny(2026, 4, 3, 12), None, date(2026, 4, 2), date(2026, 3, 27)),
])
def test_backup_changes_on_a_closed_day_use_the_last_session(now, t, last_saved, wtd_from):
    desk, days, c = closes_desk(last_saved)
    now = now.timestamp()
    q = backup_quote(c[-1] * 1.01, c[-2] * 1.002, 0.4, t=t.timestamp() if t else 0.0, state="CLOSED")
    ch, closes = desk.backup_changes("AAA", q, now)
    # The session is the last trading day; the previous close goes on the trading day before it.
    s = list(c[:-2]) + [q.prev_close, q.price]
    assert closes.tolist() == s
    assert set(ch) == set(PERIODS) and ch["1D"] == 0.4
    assert ch["1W"] == pytest.approx(ratio(q.price, s[-6]), rel=1e-12)
    assert ch["WTD"] == pytest.approx(ratio(q.price, c[days.index(wtd_from)]), rel=1e-12)


def test_backup_changes_before_the_open():
    desk, days, c = closes_desk(date(2026, 10, 5))
    now = ny(2026, 10, 6, 8).timestamp()
    # Pre-market without a time: the quote is yesterday's close, its previous close the session before.
    q = backup_quote(c[-1], c[-2] * 1.001, 0.3, t=0.0, state="PRE")
    ch, closes = desk.backup_changes("AAA", q, now)
    assert closes.tolist() == list(c[:-2]) + [q.prev_close, q.price]
    assert ch["1D"] == 0.3 and ch["WTD"] == pytest.approx(ratio(q.price, q.prev_close))  # Monday against Friday
    assert ch["1W"] == pytest.approx(ratio(q.price, c[-6]), rel=1e-12)
    # With a time, the pre-market quote counts for today: its previous close is Monday's.
    q = backup_quote(c[-1] * 1.004, c[-1], 0.4, t=now, state="PREPRE")
    ch, closes = desk.backup_changes("AAA", q, now)
    assert closes.tolist() == list(c[:-1]) + [q.prev_close, q.price]
    assert ch["1D"] == 0.4 and ch["WTD"] == pytest.approx(ratio(q.price, c[-2]))


@pytest.mark.parametrize("last_saved", [date(2026, 10, 1), date(2026, 9, 25), None])
def test_backup_changes_with_a_gap_give_only_today(last_saved):
    if last_saved:
        desk, _, c = closes_desk(last_saved)  # Friday the 2nd is missing (or more)
    else:
        desk = TrendsDesk(FakeData(), FakeSources(), make_directory())  # nothing saved
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, 176.0, 2.5), NOW)
    assert ch == {"1D": 2.5} and closes[-2:].tolist() == [176.0, 180.0]


@pytest.mark.parametrize("prev_close", [None, 0.0])
def test_backup_changes_without_a_previous_close_give_only_today(prev_close):
    desk, _, c = closes_desk(date(2026, 10, 5))
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, prev_close, 2.5), NOW)
    assert closes.tolist() == list(c[:-1]) + [180.0]  # nothing placed on Monday
    assert ch == {"1D": 2.5}


def test_backup_changes_never_place_a_negative_previous_close():
    desk, _, c = closes_desk(date(2026, 10, 5))
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, -5.0, 2.5), NOW)
    assert closes.tolist() == list(c[:-1]) + [180.0] and ch["1D"] == 2.5


def test_backup_changes_without_a_daily_change_still_give_the_periods():
    desk, days, c = closes_desk(date(2026, 10, 5))
    ch, _ = desk.backup_changes("AAA", backup_quote(180.0, 176.0, None), NOW)
    assert "1D" not in ch and set(ch) == set(PERIODS) - {"1D"}
    assert ch["WTD"] == pytest.approx(ratio(180.0, c[-2]))


def test_backup_changes_replace_todays_and_yesterdays_saved_closes():
    desk, days, c = closes_desk(END)  # Yahoo answered earlier today: Monday's and today's closes are saved
    ch, closes = desk.backup_changes("AAA", backup_quote(180.0, 176.0, 2.5), NOW)
    s = list(c[:-2]) + [176.0, 180.0]
    assert closes.tolist() == s
    assert ch["1W"] == pytest.approx(ratio(180.0, s[-6])) and ch["WTD"] == pytest.approx(ratio(180.0, c[-3]))


@pytest.mark.parametrize("prev_close, price", [(349.0, 350.0), (175.0, 350.0)])
def test_backup_changes_across_a_split_give_only_today(prev_close, price):
    desk, _, c = closes_desk(date(2026, 10, 5))  # Friday's saved close is 174.5
    ch, _ = desk.backup_changes("AAA", backup_quote(price, prev_close, 0.6), NOW)
    assert ch == {"1D": 0.6}


def test_periods_source_does_not_credit_yahoo_when_nothing_came_back(tmp_path, clock):
    data = FakeData(yahoo_ok=False, quotes_error=RuntimeError("Nasdaq HTTP 503"))
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.changes == {}
    assert snap.periods_source == ""
    assert E.trends_board(snap, [], []).footer.text.startswith("Today: Yahoo Finance · periods: — · ")
    # Yahoo and the backup both answering nothing at all: blank too.
    empty = asyncio.run(TrendsDesk(FakeData(), FakeSources(), make_directory(), tmp_path).refresh())
    assert empty.changes == {} and empty.periods_source == ""


def test_periods_source_names_whoever_gave_most(tmp_path, clock):
    bars = all_bars()
    data = FakeData(bars=bars, fail_spark={UNIVERSE[0], UNIVERSE[20]},  # two of three batches fail
                    quotes={s: live(s, 50.0, 2.0) for s in UNIVERSE[:40]})
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert len(snap.changes) == len(UNIVERSE)  # 7 from Yahoo, 40 from the backup
    assert snap.periods_source == "saved closes + Nasdaq"


def test_a_failing_spark_batch_falls_back_for_its_symbols_only(tmp_path, clock):
    bars = all_bars()
    second = UNIVERSE[20:40]
    data = FakeData(bars=bars, fail_spark={second[0]}, quotes={s: live(s, 50.0, 2.0) for s in second})
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.errors == ["Yahoo spark: 1 of 3 batches failed"]
    assert data.quote_calls == [second]
    for s in second:
        assert snap.changes[s] == {"1D": 2.0}
    for s in UNIVERSE[:20] + UNIVERSE[40:]:
        assert snap.changes[s] == period_changes(bars[s].t, bars[s].close, NOW)
    assert snap.periods_source == "Yahoo Finance"  # 27 symbols from Yahoo, 20 from the backup


def test_every_spark_batch_failing(tmp_path, clock):
    data = FakeData(bars=all_bars(), fail_spark=set(UNIVERSE), quotes={"SPY": live("SPY", 600.0, -0.4)})
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert snap.errors == ["Yahoo spark: 3 of 3 batches failed"]
    assert snap.changes == {"SPY": {"1D": -0.4}} and snap.periods_source == "saved closes + Nasdaq"


def test_spark_answers_too_short_or_unasked_are_handled(tmp_path, clock):
    bars = all_bars()
    bars["AAA"] = spark("AAA", n=1)  # a single close: no period can be measured
    bars["EXTRA"] = spark("EXTRA")
    yahoo = SparkYahoo(bars)

    async def everything(symbols, range_="1y", interval="1d"):  # Yahoo also answers for a symbol not asked
        out = await SparkYahoo.spark_bars(yahoo, symbols, range_, interval)
        out["EXTRA"] = bars["EXTRA"]
        return out

    yahoo.spark_bars = everything
    data = FakeData(quotes={"AAA": live("AAA", 101.0, 1.1)})
    data.yahoo = yahoo
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert data.quote_calls == [["AAA"]] and snap.changes["AAA"] == {"1D": 1.1}
    assert "EXTRA" not in snap.changes


def test_errors_are_collected_and_shortened(tmp_path, clock):
    data = FakeData(bars=all_bars(), fail_screens={"day_losers"}, fail_spark={UNIVERSE[0]},
                    quotes_error=RuntimeError("Nasdaq down " + "y" * 400))
    sources = FakeSources(error=RuntimeError("CoinGecko HTTP 429 " + "z" * 400))
    snap = asyncio.run(TrendsDesk(data, sources, make_directory(), tmp_path).refresh())
    assert len(snap.errors) == 4 and all(len(e) <= 160 for e in snap.errors)
    assert any(e.startswith("day_losers: No day_losers list") for e in snap.errors)
    assert any(e.startswith("CoinGecko: CoinGecko HTTP 429") for e in snap.errors)
    assert any(e.startswith("backup quotes: Nasdaq down") for e in snap.errors)
    assert "Yahoo spark: 1 of 3 batches failed" in snap.errors
    assert [m.symbol for m in snap.day_gainers] == ["UPA", "UPB"] and snap.day_losers == [] and snap.most_active
    assert snap.coins == []
    assert set(snap.changes) == set(UNIVERSE[20:])


def test_refresh_without_coin_source_or_directory(tmp_path, clock):
    bars = all_bars(list(SECTORS) + MAJOR_ETFS)
    desk = TrendsDesk(FakeData(bars=bars), None, None, tmp_path)
    snap = asyncio.run(desk.refresh())
    assert set(snap.changes) == set(SECTORS) | set(MAJOR_ETFS)
    assert snap.names["XLK"] == "XLK" and snap.coins == []
    assert len(snap.errors) == 1 and snap.errors[0].startswith("CoinGecko: ")


def test_refresh_survives_an_unwritable_folder(tmp_path, clock, caplog):
    folder = tmp_path / "blocked"
    folder.write_text("a file where the folder should be")
    desk = TrendsDesk(FakeData(bars=all_bars()), FakeSources(), make_directory(), folder)
    snap = asyncio.run(desk.refresh())
    assert len(snap.changes) == len(UNIVERSE)
    assert "Couldn't save the trend closes" in caplog.text


def test_one_bad_screener_row_does_not_sink_the_refresh(tmp_path, clock):
    screens = default_screens()
    screens["day_losers"].append(screen_row("BAD", "N/A", -3.0))
    data = FakeData(bars=all_bars(), screens=screens)
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert len(snap.changes) == len(UNIVERSE) and [m.symbol for m in snap.day_gainers] == ["UPA", "UPB"]
    assert [m.symbol for m in snap.day_losers] == ["DNA"] and snap.errors == []
    assert snap.day_source == "Yahoo Finance"


class JunkScreens(FakeData):
    """A screener whose answers aren't lists of rows."""

    def __init__(self, answers, **kwargs):
        super().__init__(**kwargs)
        self.answers = answers

    async def screener(self, scr_id, count=25):
        self.screen_calls.append((scr_id, count))
        return self.answers.get(scr_id, [])


def test_screener_answers_that_are_not_lists_of_rows(tmp_path, clock):
    data = JunkScreens({"day_gainers": 5, "day_losers": ["junk", screen_row("DNA", 30, -9.0, source="Nasdaq")],
                        "most_actives": None}, bars=all_bars())
    snap = asyncio.run(TrendsDesk(data, FakeSources(), make_directory(), tmp_path).refresh())
    assert len(snap.errors) == 1 and snap.errors[0].startswith("day_gainers: ")  # caught where it's converted
    assert snap.day_gainers == [] and [m.symbol for m in snap.day_losers] == ["DNA"] and snap.most_active == []
    assert snap.day_source == ""  # the first row wasn't a row: no source is claimed from it
    assert len(snap.changes) == len(UNIVERSE)


def test_refresh_reuses_a_snapshot_younger_than_max_age(tmp_path, clock):
    data = FakeData(bars=all_bars())
    desk = TrendsDesk(data, FakeSources(), make_directory(), tmp_path)
    first = asyncio.run(desk.refresh(max_age=60))
    clock.now += 59
    assert asyncio.run(desk.refresh(max_age=60)) is first
    assert len(data.screen_calls) == 3 and len(data.yahoo.calls) == 3
    clock.now += 1  # exactly max_age old: refreshed
    second = asyncio.run(desk.refresh(max_age=60))
    assert second is not first and second.at == NOW + 60 and len(data.screen_calls) == 6
    third = asyncio.run(desk.refresh())  # max_age 0: always fresh
    assert third is not second and len(data.screen_calls) == 9 and desk.last is third


def test_concurrent_refreshes_share_one_fetch(tmp_path, clock):
    data = FakeData(bars=all_bars())
    sources = FakeSources()
    desk = TrendsDesk(data, sources, make_directory(), tmp_path)

    async def run():
        return await asyncio.gather(*(desk.refresh(max_age=60) for _ in range(5)))

    snaps = asyncio.run(run())
    assert all(s is snaps[0] for s in snaps)
    assert len(data.screen_calls) == 3 and len(data.yahoo.calls) == 3 and sources.calls == [250]


def test_forced_refreshes_queue_behind_each_other(tmp_path, clock):
    data = FakeData(bars=all_bars())
    sources = FakeSources()
    desk = TrendsDesk(data, sources, make_directory(), tmp_path)

    async def run():
        return await asyncio.gather(desk.refresh(), desk.refresh(), desk.refresh())

    snaps = asyncio.run(run())
    assert len({id(s) for s in snaps}) == 3 and desk.last is snaps[-1]
    assert sources.max_inflight == 1 and len(data.screen_calls) == 9


# ----- when the recaps are due -----

@pytest.mark.parametrize("day, last", [
    (date(2026, 10, 9), True),  # an ordinary Friday
    (date(2026, 10, 8), False),
    (date(2026, 10, 5), False),
    (date(2026, 10, 10), False),  # Saturday
    (date(2026, 10, 11), False),  # Sunday
    (date(2026, 4, 2), True),  # Thursday before Good Friday (2026-04-03)
    (date(2026, 4, 3), False),
    (date(2026, 7, 2), True),  # Independence Day observed on Friday 2026-07-03
    (date(2026, 7, 3), False),
    (date(2026, 6, 18), True),  # Juneteenth on a Friday
    (date(2026, 11, 25), False),  # the day before Thanksgiving: Friday still trades
    (date(2026, 11, 26), False),  # Thanksgiving
    (date(2026, 11, 27), True),
    (date(2026, 12, 24), True),  # Christmas on a Friday
    (date(2026, 12, 25), False),
    (date(2026, 12, 31), True),  # New Year's Day 2027 is a Friday holiday
    (date(2027, 12, 23), True),  # Christmas 2027 (Saturday) is observed on Friday the 24th
    (date(2027, 12, 31), True),  # New Year's Day 2028 (Saturday) isn't observed in December
    (date(2026, 9, 11), True),
])
def test_last_trading_day_of_week(day, last):
    assert last_trading_day_of_week(day, is_trading_day) is last


@pytest.mark.parametrize("day, last", [
    (date(2026, 10, 30), True),  # October 31 is a Saturday
    (date(2026, 10, 29), False),
    (date(2026, 10, 31), False),
    (date(2026, 1, 30), True),  # January 31 is a Saturday
    (date(2026, 2, 27), True),  # February 28 is a Saturday
    (date(2026, 5, 29), True),  # May 31 is a Sunday
    (date(2026, 8, 31), True),  # a Monday
    (date(2026, 8, 28), False),
    (date(2026, 3, 31), True),
    (date(2026, 4, 30), True),
    (date(2026, 11, 30), True),
    (date(2026, 12, 31), True),
    (date(2026, 12, 30), False),
    (date(2027, 5, 28), True),  # May 31, 2027 is Memorial Day
    (date(2027, 5, 31), False),
    (date(2024, 3, 28), True),  # Good Friday 2024 was March 29
    (date(2027, 12, 31), True),
])
def test_last_trading_day_of_month(day, last):
    assert last_trading_day_of_month(day, is_trading_day) is last


def test_last_trading_day_rules_take_any_calendar():
    always = lambda d: True  # noqa: E731
    weekdays = lambda d: d.weekday() < 5  # noqa: E731
    assert not last_trading_day_of_week(date(2026, 10, 8), always)
    assert last_trading_day_of_week(date(2026, 10, 9), always)  # the week ends on Friday whatever the calendar
    assert not last_trading_day_of_month(date(2026, 10, 30), always)  # Saturday the 31st trades in this calendar
    assert last_trading_day_of_month(date(2026, 10, 31), always)
    closed_friday = lambda d: weekdays(d) and d != date(2026, 10, 9)  # noqa: E731
    assert last_trading_day_of_week(date(2026, 10, 8), closed_friday)
    closed_rest = lambda d: weekdays(d) and d < date(2026, 10, 7)  # noqa: E731
    assert last_trading_day_of_week(date(2026, 10, 6), closed_rest)
    assert last_trading_day_of_month(date(2026, 10, 6), closed_rest)


def test_every_trading_week_and_month_of_two_years_has_exactly_one_last_day():
    d = date(2026, 1, 1)
    weeks, months = {}, {}
    while d < date(2028, 1, 1):
        if last_trading_day_of_week(d, is_trading_day):
            weeks.setdefault(d.isocalendar()[:2], []).append(d)
        if last_trading_day_of_month(d, is_trading_day):
            months.setdefault((d.year, d.month), []).append(d)
        d += timedelta(days=1)
    assert len(months) == 24 and all(len(v) == 1 for v in months.values())
    assert all(len(v) == 1 for v in weeks.values()) and len(weeks) >= 103


# ----- embeds -----

def test_sources_note():
    q = lambda s, src: Quote(s, s, 1.0, 1.0, 0.0, source=src)  # noqa: E731
    assert E.sources_note({}) == "Yahoo Finance"
    assert E.sources_note({"A": q("A", "Yahoo"), "B": q("B", "Yahoo")}) == "Yahoo Finance"
    assert E.sources_note({"A": q("A", "Nasdaq"), "B": q("B", "Coinbase"), "C": q("C", "Nasdaq")}) == \
        "Nasdaq, Coinbase (backups: Yahoo isn't answering)"
    assert E.sources_note({"A": q("A", "Yahoo"), "B": q("B", "Nasdaq")}) == "Yahoo Finance, Nasdaq"
    assert E.sources_note({"A": q("A", "CoinGecko")}) == "CoinGecko (backups: Yahoo isn't answering)"
    assert E.sources_note({"A": None, "B": q("B", "Massive")}) == "Massive (backups: Yahoo isn't answering)"
    assert E.sources_note({"A": None}) == "Yahoo Finance"


@pytest.mark.parametrize("market", ["stocks", "sectors", "crypto"])
@pytest.mark.parametrize("period", list(PERIODS))
def test_trends_embed_fits_discord_for_every_market_and_period(market, period):
    snap, sp500, ndx = big_snapshot()
    e = E.trends_embed(snap, period, market, sp500, ndx)
    assert_fits(e)
    blocks_closed(e)
    assert e.title == f"{E.TREND_MARKETS[market]} · biggest moves {label(period, market)}"
    empty = E.trends_embed(Snapshot(NOW), period, market, [], [])
    assert_fits(empty)
    assert not empty.fields and empty.description
    assert empty.title == e.title


@pytest.mark.parametrize("market", ["stocks", "sectors", "crypto"])
@pytest.mark.parametrize("period", list(PERIODS))
def test_trends_embed_with_absurd_numbers_still_fits(market, period):
    snap, sp500, ndx = huge_snapshot()
    assert_fits(E.trends_embed(snap, period, market, sp500, ndx))


@pytest.mark.parametrize("period", ["WTD", "MTD", "1W", "1M", "3M", "YTD", "1Y"])
def test_stock_trends_over_a_period(period):
    snap, sp500, ndx = big_snapshot()
    e = E.trends_embed(snap, period, "stocks", sp500, ndx)
    pool = list(dict.fromkeys(sp500 + ndx))
    best = sorted(pool, key=lambda s: -snap.changes[s][period])
    assert [f.name for f in e.fields] == ["🚀 Gainers", "💥 Losers"]
    assert row_labels(e.fields[0].value) == [s for s in best if snap.changes[s][period] > 0][:10]
    assert row_labels(e.fields[1].value) == [s for s in best[::-1] if snap.changes[s][period] < 0][:10]
    up, down = snap.breadth(sp500, period)
    assert e.description == f"S&P 500 {PERIODS[period]}: **{up}** up · **{down}** down"
    assert e.footer.text == "S&P 500 + Nasdaq-100 · Yahoo Finance"


def test_stock_trends_count_limits_the_rows():
    snap, sp500, ndx = big_snapshot()
    e = E.trends_embed(snap, "YTD", "stocks", sp500, ndx, count=5)
    assert [len(rows(f.value)) for f in e.fields] == [5, 5]


@pytest.mark.parametrize("source", ["Yahoo Finance", "Nasdaq (last close)"])
def test_stock_trends_today_use_the_whole_market_lists(source):
    snap, sp500, ndx = big_snapshot(source)
    e = E.trends_embed(snap, "1D", "stocks", sp500, ndx)
    assert [f.name for f in e.fields] == ["🚀 Gainers", "💥 Losers", "🔊 Most traded"]
    assert row_labels(e.fields[0].value) == [f"GAIN{i}" for i in range(10)]
    assert row_labels(e.fields[1].value) == [f"LOSE{i}" for i in range(10)]  # the biggest fall first
    assert row_labels(e.fields[2].value) == [f"ACT{i}" for i in range(8)]
    assert e.footer.text == f"Whole US market, companies worth $2B+ · {source}"
    up, down = snap.breadth(sp500)
    assert e.description == f"S&P 500: **{up}** up · **{down}** down"


def test_stock_trends_today_without_the_screener_lists_use_the_index_members():
    snap, sp500, ndx = big_snapshot()
    snap.day_gainers, snap.day_losers, snap.most_active = [], [], []
    e = E.trends_embed(snap, "1D", "stocks", sp500, ndx)
    assert [f.name for f in e.fields] == ["🚀 Gainers", "💥 Losers"]
    assert e.footer.text == "S&P 500 + Nasdaq-100 · Yahoo Finance"
    assert e.description.startswith("S&P 500 today: **")


def test_stock_trends_ignore_sectors_and_etfs():
    snap = Snapshot(NOW, changes={"XLK": {"1W": 9.0}, "SPY": {"1W": 5.0}, "AAA": {"1W": 1.0}},
                    prices={"XLK": 1.0, "SPY": 1.0, "AAA": 1.0})
    e = E.trends_embed(snap, "1W", "stocks", ["AAA"], [])
    assert row_labels(e.fields[0].value) == ["AAA"] and row_labels(e.fields[1].value) == []
    assert e.fields[1].value == "—"


@pytest.mark.parametrize("period", list(PERIODS))
def test_sector_trends(period):
    snap, sp500, ndx = big_snapshot()
    snap.periods_source = "saved closes + Nasdaq"
    e = E.trends_embed(snap, period, "sectors", sp500, ndx)
    assert [f.name for f in e.fields] == ["Sector & major ETFs"]
    order = sorted(list(SECTORS) + MAJOR_ETFS, key=lambda s: -snap.changes[s][period])[:20]
    assert row_labels(e.fields[0].value, 12) == [SECTORS.get(s, s)[:12] for s in order]
    assert e.footer.text == "saved closes + Nasdaq · past moves, not predictions"


@pytest.mark.parametrize("period, window, words", [
    ("1D", "change_24h", "in 24 hours"), ("1W", "change_7d", "over 7 days"), ("1M", "change_30d", "over 30 days"),
    ("1Y", "change_1y", "over a year"),
    ("WTD", "change_7d", "over 7 days"), ("MTD", "change_30d", "over 30 days"),  # CoinGecko's nearest windows
])
def test_crypto_trends(period, window, words):
    snap, sp500, ndx = big_snapshot()
    e = E.trends_embed(snap, period, "crypto", sp500, ndx)
    assert e.title == f"🪙 Crypto · biggest moves {words}"
    movers = snap.coin_movers(period)
    assert sorted(m.change for m in movers) == sorted(getattr(c, window) for c in snap.coins
                                                      if real_coin(c) and c.volume >= MIN_COIN_VOLUME)
    assert [f.name for f in e.fields] == ["🚀 Gainers", "💥 Losers"]
    assert row_labels(e.fields[0].value) == [m.symbol[:-4] for m in movers if m.change > 0][:10]
    assert row_labels(e.fields[1].value) == [m.symbol[:-4] for m in movers[::-1] if m.change < 0][:10]
    assert e.footer.text == "Top 250 coins by market cap, without stablecoins and wrapped coins · CoinGecko"
    assert e.description is None


@pytest.mark.parametrize("market", ["stocks", "sectors"])
@pytest.mark.parametrize("period", list(PERIODS))
def test_empty_stock_and_sector_trends_explain_why(market, period):
    e = E.trends_embed(Snapshot(NOW), period, market, ["AAA"], ["BBB"])
    assert e.description == E.NO_PERIOD_DATA and "/status" in E.NO_PERIOD_DATA
    assert e.footer.text in ("S&P 500 + Nasdaq-100 · Yahoo Finance", "Yahoo Finance · past moves, not predictions")


CRYPTO_FOOTER = "Top 250 coins by market cap, without stablecoins and wrapped coins · CoinGecko"


@pytest.mark.parametrize("period", ["3M", "YTD"])
def test_crypto_periods_coingecko_does_not_give_are_explained(period):
    snap, sp500, ndx = big_snapshot()
    e = E.trends_embed(snap, period, "crypto", sp500, ndx)
    assert "CoinGecko" in e.description and "Yahoo" not in e.description and e.description != E.NO_PERIOD_DATA
    assert e.description == ("CoinGecko has 24-hour, 7-day, 30-day and 1-year changes for coins: pick one of those.")
    assert not e.fields and e.footer.text == CRYPTO_FOOTER
    assert e.title == f"🪙 Crypto · biggest moves {PERIODS[period]}"
    assert_fits(e)


@pytest.mark.parametrize("period", ["1D", "WTD", "MTD", "1W", "1M", "1Y"])
def test_empty_crypto_trends_do_not_blame_yahoo(period):
    for snap in (Snapshot(NOW),  # CoinGecko didn't answer
                 Snapshot(NOW, coins=[coin("USDT", "Tether", 1.0, c7=0.0)])):  # nothing but a stablecoin
        e = E.trends_embed(snap, period, "crypto", [], [])
        assert "Yahoo" not in e.description and e.description != E.NO_PERIOD_DATA
        assert e.description == "No crypto numbers right now: CoinGecko isn't answering. They come back by themselves."
        assert not e.fields and e.footer.text == CRYPTO_FOOTER


def test_a_coin_period_with_only_rises_shows_an_empty_losers_table():
    snap = Snapshot(NOW, coins=[coin("BTC", "Bitcoin", 60000, c24=2.0), coin("ETH", "Ethereum", 3000, c24=1.0)])
    e = E.trends_embed(snap, "1D", "crypto", [], [])
    assert row_labels(e.fields[0].value) == ["BTC", "ETH"] and e.fields[1].value == "—"


def test_trends_board_with_everything():
    snap, sp500, ndx = big_snapshot()
    e = E.trends_board(snap, sp500, ndx, "🟢 Market open")
    assert_fits(e)
    blocks_closed(e)
    up, down = snap.breadth(sp500)
    nup, ndown = snap.breadth(ndx)
    assert e.title == "🔥 Market Trends · Live"
    assert e.description == (f"🟢 Market open · updated <t:{int(NOW)}:R>\nS&P 500 today: **{up}** up · **{down}** "
                             f"down · Nasdaq-100: **{nup}** up · **{ndown}** down")
    assert [f.name for f in e.fields] == ["🚀 Top gainers today (US, $2B+)", "💥 Top losers today",
                                          "🔊 Most traded today", "🏭 Sectors today",
                                          "📅 This week's leaders (S&P 500 + Nasdaq-100)", "🪙 Crypto 24h (top 250)"]
    assert [len(rows(f.value)) for f in e.fields] == [8, 8, 6, 12, 10, 10]
    sectors = sorted(SECTORS, key=lambda s: -snap.changes[s]["1D"])
    assert row_labels(e.fields[3].value, 12) == [SECTORS[s][:12] for s in sectors]
    week = snap.movers("WTD", list(dict.fromkeys(sp500 + ndx)))  # the calendar week, not the last 5 sessions
    assert week[0].symbol == "S007"
    assert row_labels(e.fields[4].value) == [m.symbol for m in week[:5]] + [m.symbol for m in week[::-1][:5]]
    coins = snap.coin_movers("1D")
    assert row_labels(e.fields[5].value) == [m.symbol[:-4] for m in coins[:5]] + [m.symbol[:-4] for m in coins[::-1][:5]]
    assert e.color.value == (E.GREEN if up >= down else E.RED)
    assert e.footer.text == ("Today: Yahoo Finance · periods: Yahoo Finance · crypto: CoinGecko · "
                             "/trends for any period")


def test_trends_board_says_last_session_for_nasdaqs_lists():
    snap, sp500, ndx = big_snapshot("Nasdaq (last close)")
    e = E.trends_board(snap, sp500, ndx)
    assert [f.name for f in e.fields][:3] == ["🚀 Top gainers last session (US, $2B+)", "💥 Top losers last session",
                                              "🔊 Most traded last session"]
    assert e.description.startswith(f"updated <t:{int(NOW)}:R>\nS&P 500 today: ")
    assert e.footer.text.startswith("Today: Nasdaq (last close) · periods: Yahoo Finance · ")


def test_empty_trends_board_waits_for_data():
    e = E.trends_board(Snapshot(NOW), [], [])
    assert [f.name for f in e.fields] == ["Waiting for data"]
    assert e.description == f"updated <t:{int(NOW)}:R>" and e.color.value == E.GREEN
    assert e.footer.text == "Today: — · periods: — · crypto: CoinGecko · /trends for any period"
    assert_fits(e)


def test_trends_board_turns_red_when_more_stocks_fall():
    snap = Snapshot(NOW, changes={"A": {"1D": -1.0}, "B": {"1D": -2.0}, "C": {"1D": 3.0}},
                    prices={"A": 1.0, "B": 1.0, "C": 1.0})
    e = E.trends_board(snap, ["A", "B", "C"], ["C"])
    assert e.color.value == E.RED
    assert e.description.endswith("S&P 500 today: **1** up · **2** down · Nasdaq-100: **1** up · **0** down")
    assert [f.name for f in e.fields] == ["Waiting for data"]  # no lists, sectors, week or coins yet


def test_trends_board_week_is_the_calendar_week():
    snap = Snapshot(NOW, changes={"A": {"WTD": 5.0, "1W": -5.0}, "B": {"WTD": -5.0, "1W": 5.0}, "C": {"1W": 9.0}},
                    prices={"A": 1.0, "B": 1.0, "C": 1.0})
    e = E.trends_board(snap, ["A", "B", "C"], [])
    week, = [f for f in e.fields if f.name.startswith("📅")]
    assert row_labels(week.value) == ["A", "B", "B", "A"]  # by this week's change; C has none this week
    only_rolling = Snapshot(NOW, changes={"C": {"1W": 9.0}}, prices={"C": 1.0})
    assert [f.name for f in E.trends_board(only_rolling, ["C"], []).fields] == ["Waiting for data"]


def test_trends_board_with_only_coins():
    e = E.trends_board(Snapshot(NOW, coins=[coin("BTC", "Bitcoin", 60000, c24=3.0)]), [], [])
    assert [f.name for f in e.fields] == ["🪙 Crypto 24h (top 250)"]
    assert set(row_labels(e.fields[0].value)) == {"BTC"}


def test_trends_board_with_absurd_numbers_still_fits():
    snap, sp500, ndx = huge_snapshot()
    assert_fits(E.trends_board(snap, sp500, ndx, "🌙 After hours"))


# ----- the recaps -----

class FakeDesk:
    def __init__(self, snap=None, sp500=(), ndx=()):
        self.snap = snap if snap is not None else Snapshot(NOW)
        self.last = None
        self.calls = []
        self.members = {"sp500": list(sp500), "ndx100": list(ndx)}
        self.directory = None

    async def refresh(self, max_age=0):
        self.calls.append(max_age)
        self.snap.at = time.time()
        self.last = self.snap
        return self.snap

    def index_members(self, tag):
        return list(self.members.get(tag, []))


def recap_bot(snap, sp500=(), ndx=()):
    return SimpleNamespace(trends=FakeDesk(snap, sp500, ndx))


@pytest.mark.parametrize("period, titles", [
    ("1D", ["📈 Stocks · biggest moves today", "🏭 Sectors & ETFs · biggest moves today"]),
    ("1W", ["📈 Stocks · biggest moves this week", "🏭 Sectors & ETFs · biggest moves this week",
            "🪙 Crypto · biggest moves over 7 days"]),
    ("1M", ["📈 Stocks · biggest moves this month", "🏭 Sectors & ETFs · biggest moves this month",
            "🪙 Crypto · biggest moves over 30 days", "📈 Stocks · biggest moves year to date"]),
])
def test_trends_recaps_fit_one_message(period, titles):
    snap, sp500, ndx = big_snapshot()
    bot = recap_bot(snap, sp500, ndx)
    posts = asyncio.run(briefs.trends_recap(bot, period))
    assert len(posts) == 1 and not posts[0].files and posts[0].content is None
    embeds = posts[0].embeds
    assert len(embeds) == len(titles) <= 10
    prefix = {"1D": "📅 Daily trends · ", "1W": "🗓️ Weekly trends · week of ", "1M": "📆 Monthly trends · "}[period]
    assert embeds[0].title.startswith(prefix) and embeds[0].title.endswith(" · " + titles[0])
    assert [e.title for e in embeds[1:]] == titles[1:]
    for e in embeds:
        assert_fits(e)
    assert sum(len(e) for e in embeds) <= TOTAL
    assert bot.trends.calls == [120]
    if period == "1M":
        assert [len(rows(f.value)) for f in embeds[3].fields] == [5, 5]  # the YTD leaders, five each


@pytest.mark.parametrize("period", ["1D", "1W", "1M"])
def test_recaps_with_absurd_numbers_drop_trailing_embeds_to_fit(period):
    snap, sp500, ndx = huge_snapshot()
    embeds = asyncio.run(briefs.trends_recap(recap_bot(snap, sp500, ndx), period))[0].embeds
    assert embeds and sum(len(e) for e in embeds) <= TOTAL
    for e in embeds:
        assert_fits(e)
    assert embeds[0].title.endswith("📈 Stocks · biggest moves " + {"1D": "today", "1W": "this week",
                                                                       "1M": "this month"}[period])
    if period == "1M":
        assert len(embeds) == 3  # the year-to-date leaders didn't fit: left out rather than break the message


@pytest.mark.parametrize("period, stocks, coins", [("1D", "1D", None), ("1W", "WTD", "change_7d"),
                                                   ("1M", "MTD", "change_30d")])
def test_recaps_cover_the_calendar_week_and_month(period, stocks, coins):
    """The weekly and monthly recaps rank stocks and sectors by the calendar week and month (not the last 5 or 21
    sessions); crypto by CoinGecko's 7 and 30 days; the monthly one adds the year to date."""
    snap, sp500, ndx = big_snapshot()
    embeds = asyncio.run(briefs.trends_recap(recap_bot(snap, sp500, ndx), period))[0].embeds
    pool = list(dict.fromkeys(sp500 + ndx))
    best = sorted(pool, key=lambda s: -snap.changes[s][stocks])
    up, down = snap.breadth(sp500, stocks)
    if stocks == "1D":  # today: the whole market's lists
        assert row_labels(embeds[0].fields[0].value) == [f"GAIN{i}" for i in range(10)]
        assert embeds[0].description == f"S&P 500: **{up}** up · **{down}** down"
    else:
        assert row_labels(embeds[0].fields[0].value) == [s for s in best if snap.changes[s][stocks] > 0][:10]
        assert embeds[0].description == f"S&P 500 {PERIODS[stocks]}: **{up}** up · **{down}** down"
    assert len(embeds) == {"1D": 2, "1W": 3, "1M": 4}[period]
    order = sorted(list(SECTORS) + MAJOR_ETFS, key=lambda s: -snap.changes[s][stocks])[:20]
    assert row_labels(embeds[1].fields[0].value, 12) == [SECTORS.get(s, s)[:12] for s in order]
    if coins:
        movers = sorted((c for c in snap.coins if real_coin(c) and c.volume >= MIN_COIN_VOLUME),
                        key=lambda c: -getattr(c, coins))
        assert row_labels(embeds[2].fields[0].value) == [c.symbol for c in movers if getattr(c, coins) > 0][:10]
    if period == "1M":
        ytd = sorted(pool, key=lambda s: -snap.changes[s]["YTD"])
        assert embeds[3].title == "📈 Stocks · biggest moves year to date"
        assert row_labels(embeds[3].fields[0].value) == [s for s in ytd if snap.changes[s]["YTD"] > 0][:5]


@pytest.mark.parametrize("period", ["1D", "1W", "1M"])
def test_recaps_of_an_empty_snapshot_still_post(period):
    embeds = asyncio.run(briefs.trends_recap(recap_bot(Snapshot(NOW)), period))[0].embeds
    assert len(embeds) == {"1D": 2, "1W": 3, "1M": 4}[period]
    assert all(e.description for e in embeds) and sum(len(e) for e in embeds) <= TOTAL


def test_crypto_trends_brief():
    snap, sp500, ndx = big_snapshot()
    bot = recap_bot(snap, sp500, ndx)
    posts = asyncio.run(briefs.crypto_trends(bot))
    assert len(posts) == 1 and len(posts[0].embeds) == 1
    e = posts[0].embeds[0]
    assert e.title.startswith("🪙 Crypto daily trends · ") and e.title.endswith(" · last 24 hours")
    assert [f.name for f in e.fields] == ["🚀 Gainers", "💥 Losers"]
    assert_fits(e)
    assert bot.trends.calls == [120]
    huge, sp, nd = huge_snapshot()
    assert_fits(asyncio.run(briefs.crypto_trends(recap_bot(huge, sp, nd)))[0].embeds[0])


def sized(n):
    return discord.Embed(description="x" * n)


@pytest.mark.parametrize("sizes, kept", [
    ([], 0), ([3000, 2000, 1000], 2), ([2900, 2900], 2), ([2900, 2901], 1), ([5900], 1), ([5900, 10], 1),
    ([2000, 4000, 100], 1), ([1000] * 5, 5), ([1000] * 6, 5), ([4096, 4096, 4096], 1),
])
def test_fit_total_drops_trailing_embeds(sizes, kept):
    embeds = [sized(n) for n in sizes]
    out = _fit_total(embeds)
    assert out == embeds[:kept]


# ----- the bot (Discord faked) -----

class FakeEngine:
    def __init__(self, data=None):
        self.data = data or FakeData()
        self.models = {}

    async def close(self):
        pass


def make_bot(tmp_path, data=None, desk=None):
    bot = MarketBot(tmp_path, engine=FakeEngine(data), ai=NewsAI(readers=[]))
    bot.sent, bot.boards = [], []

    async def send(cid, post):
        bot.sent.append((cid, post))
        return SimpleNamespace(id=1)

    async def show_board(cid, embed):
        bot.boards.append((cid, embed))

    bot.send = send
    bot.show_board = show_board
    if desk is not None:
        bot.trends = desk
    return bot


def test_the_bot_builds_its_trends_desk(tmp_path):
    bot = MarketBot(tmp_path, engine=FakeEngine(), ai=NewsAI(readers=[]))
    assert isinstance(bot.trends, TrendsDesk) and bot.trends.closes.path == tmp_path / "trend-closes.npz"
    assert bot.trends.sources is None and bot.trends.directory is None  # the fake engine has neither
    assert ("trends", 60, bot.job_trends) in bot.schedule()


def test_job_trends_posts_the_board_to_trends_channels_only(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "market_open", lambda: True)
    snap, sp500, ndx = big_snapshot()
    desk = FakeDesk(snap, sp500, ndx)
    bot = make_bot(tmp_path, desk=desk)
    for cid, kind in ((1, "stocks"), (2, "trends"), (3, "crypto"), (4, "trends"), (5, "news"), (6, "nvidia"),
                      (7, "research")):
        bot.channels.set(cid, kind)
    bot.quotes["^GSPC"] = Quote("^GSPC", "S&P 500", 6000.0, 5990.0, 0.17, market_state="REGULAR")
    asyncio.run(bot.job_trends())
    assert [cid for cid, _ in bot.boards] == [2, 4]
    e = bot.boards[0][1]
    assert bot.boards[1][1] is e  # one board for every trends channel
    assert e.title == "🔥 Market Trends · Live" and e.description.startswith("🟢 Market open · updated <t:")
    assert "S&P 500 today: **" in e.description
    assert desk.calls == [120] and not bot.sent  # a snapshot under two minutes old is reused, not refetched


def test_job_trends_without_the_sp500_quote_has_no_market_state(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "market_open", lambda: False)
    bot = make_bot(tmp_path, desk=FakeDesk())
    bot.channels.set(2, "trends")
    asyncio.run(bot.job_trends())
    (cid, e), = bot.boards
    assert cid == 2 and e.description.startswith("updated <t:")


def test_job_trends_does_nothing_without_a_trends_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "market_open", lambda: True)
    desk = FakeDesk()
    bot = make_bot(tmp_path, desk=desk)
    bot.channels.set(1, "stocks")
    bot.channels.set(2, "nvidia")
    asyncio.run(bot.job_trends())
    assert desk.calls == [] and bot.boards == []


@pytest.mark.parametrize("is_open, age, refreshed", [
    (True, 10, False), (True, TRENDS_OPEN_SECONDS - 6, False), (True, TRENDS_OPEN_SECONDS - 4, True),
    (True, TRENDS_OPEN_SECONDS + 60, True), (False, TRENDS_OPEN_SECONDS + 60, False),
    (False, TRENDS_CLOSED_SECONDS - 6, False), (False, TRENDS_CLOSED_SECONDS - 4, True),
    (False, 10 * TRENDS_CLOSED_SECONDS, True),
])
def test_job_trends_cadence(tmp_path, monkeypatch, is_open, age, refreshed):
    """The board keeps its own clock: it's refreshed when the board itself is due, whatever made the last snapshot."""
    monkeypatch.setattr(botmod, "market_open", lambda: is_open)
    desk = FakeDesk()
    bot = make_bot(tmp_path, desk=desk)
    bot.channels.set(2, "trends")
    bot._trends_board_at = time.time() - age
    asyncio.run(bot.job_trends())
    assert bool(desk.calls) is refreshed and len(bot.boards) == (1 if refreshed else 0)
    if refreshed:
        every = TRENDS_OPEN_SECONDS if is_open else TRENDS_CLOSED_SECONDS
        assert desk.calls == [min(every - 5, 120)] and bot._trends_board_at > time.time() - 5
    assert (TRENDS_OPEN_SECONDS, TRENDS_CLOSED_SECONDS) == (300, 1800)


def test_a_trends_command_refresh_does_not_freeze_the_board(tmp_path, monkeypatch):
    """/trends and the recaps refresh the shared snapshot; the board still updates on its own schedule (it used to
    wait for the snapshot to age, so frequent /trends use froze it)."""
    monkeypatch.setattr(botmod, "market_open", lambda: True)
    desk = FakeDesk()
    bot = make_bot(tmp_path, desk=desk)
    bot.channels.set(2, "trends")
    bot._trends_board_at = time.time() - TRENDS_OPEN_SECONDS
    asyncio.run(desk.refresh(max_age=180))  # someone ran /trends just now
    asyncio.run(bot.job_trends())
    assert len(bot.boards) == 1


def test_setup_lets_a_new_trends_channel_get_its_board_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "market_open", lambda: False)
    bot = make_bot(tmp_path, desk=FakeDesk())
    bot.channels.set(2, "trends")
    asyncio.run(bot.job_trends())
    assert len(bot.boards) == 1
    bot.channels.set(9, "trends")  # what /channel does, which also resets the board clock:
    bot._trends_board_at = 0.0
    asyncio.run(bot.job_trends())
    assert [cid for cid, _ in bot.boards] == [2, 2, 9]


def test_board_job_and_command_share_one_refresh(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(botmod, "market_open", lambda: False)
    data = FakeData(bars=all_bars())
    bot = make_bot(tmp_path, data=data)
    bot.trends = TrendsDesk(data, FakeSources(), make_directory(), tmp_path)
    bot.channels.set(2, "trends")

    async def both():
        return await asyncio.gather(bot.job_trends(), bot.trends.refresh(max_age=180))

    _, snap = asyncio.run(both())
    assert len(data.screen_calls) == 3 and len(data.yahoo.calls) == 3
    assert bot.trends.last is snap and len(bot.boards) == 1
    assert bot.boards[0][1].description.endswith("S&P 500 today: **" + str(snap.breadth(["AAA", "BBB", "CCC"])[0])
                                                 + "** up · **" + str(snap.breadth(["AAA", "BBB", "CCC"])[1])
                                                 + "** down · Nasdaq-100: **" + str(snap.breadth(["BBB", "DDD"])[0])
                                                 + "** up · **" + str(snap.breadth(["BBB", "DDD"])[1]) + "** down")


def test_live_boards_and_move_alerts_leave_trends_channels_alone(tmp_path):
    bot = make_bot(tmp_path)
    bot.channels.set(9, "trends")
    bot.quotes = {"NVDA": Quote("NVDA", "NVIDIA", 100.0, 80.0, 25.0, market_state="REGULAR"),
                  "^GSPC": Quote("^GSPC", "S&P 500", 6000.0, 5000.0, 20.0, market_state="REGULAR")}
    assert bot.live_symbols() == []
    asyncio.run(bot.refresh_boards())
    asyncio.run(bot.check_moves())
    assert not bot.boards and not bot.sent


@pytest.fixture
def bot_clock(monkeypatch):
    """The bot module's time.time(), frozen and movable."""
    c = Clock()
    monkeypatch.setattr(botmod, "time", SimpleNamespace(time=c.time, monotonic=time.monotonic))
    return c


def test_job_live_drops_quotes_no_source_refreshed_for_fifteen_minutes(tmp_path, bot_clock):
    assert botmod.STALE_QUOTE == 900
    data = FakeData(quotes={"AAPL": live("AAPL", 230.0, 0.4), "^GSPC": live("^GSPC", 6000.0, 0.2, name="S&P 500")})
    bot = make_bot(tmp_path, data=data)
    bot.channels.set(1, "stocks")
    bot.quotes["KEPT"] = live("KEPT", 1.0, 0.0)  # put there without a source's timestamp: never counted as stale
    asyncio.run(bot.job_live())
    assert bot.quote_seen == {"AAPL": NOW, "^GSPC": NOW} and set(bot.quotes) == {"AAPL", "^GSPC", "KEPT"}
    del data.backup["^GSPC"]  # e.g. an index only Yahoo has, while Yahoo is down
    bot_clock.now = NOW + 600
    asyncio.run(bot.job_live())
    assert "^GSPC" in bot.quotes and bot.quote_seen["AAPL"] == NOW + 600
    bot_clock.now = NOW + 900  # exactly fifteen minutes: still shown
    asyncio.run(bot.job_live())
    assert "^GSPC" in bot.quotes
    bot_clock.now = NOW + 901
    asyncio.run(bot.job_live())
    assert set(bot.quotes) == {"AAPL", "KEPT"} and bot.quotes_at == NOW + 901
    assert len(bot.boards) == 4
    assert "S&P 500" in bot.boards[-2][1].fields[0].value  # the Indices table still had it at fifteen minutes
    assert bot.boards[-1][1].fields[0].value == "—"  # and now it's gone, not shown as live
    data.backup["^GSPC"] = live("^GSPC", 6010.0, 0.3, name="S&P 500")  # back: shown again
    bot_clock.now = NOW + 960
    asyncio.run(bot.job_live())
    assert bot.quotes["^GSPC"].price == 6010.0 and bot.quote_seen["^GSPC"] == NOW + 960


def test_one_failing_board_does_not_stop_the_others_or_the_alerts(tmp_path, monkeypatch, caplog):
    btc = Quote("BTC-USD", "Bitcoin", 66000.0, 60000.0, 10.0, market_state="REGULAR", source="CoinGecko",
                extra={"change_window": "24h", "volume24h": 3e10})
    bot = make_bot(tmp_path, data=FakeData(quotes={"BTC-USD": btc, "AAPL": live("AAPL", 230.0, 0.4)}))
    for cid, kind in ((1, "stocks"), (2, "trends"), (3, "crypto"), (4, "stocks")):
        bot.channels.set(cid, kind)

    def boom(*args, **kwargs):
        raise RuntimeError("stocks board broke")

    monkeypatch.setattr(E, "stocks_board", boom)
    asyncio.run(bot.job_live())
    assert [cid for cid, _ in bot.boards] == [3]  # the crypto board; never one for the trends channel
    assert "Board for channel 1 failed" in caplog.text and "Board for channel 4 failed" in caplog.text
    (cid, post), = bot.sent  # the move alerts still ran after the failing boards
    assert cid == 3 and post.embeds[0].title == "🚀 BTC up 10.0% in 24 hours"


@pytest.mark.parametrize("extra, window", [({"change_window": "24h", "volume24h": 1e9}, "in 24 hours"),
                                           ({}, "since midnight UTC")])
def test_crypto_move_alerts_name_the_quotes_window(tmp_path, extra, window):
    bot = make_bot(tmp_path)
    bot.channels.set(3, "crypto")
    bot.quotes = {"BTC-USD": Quote("BTC-USD", "Bitcoin", 63600.0, 60000.0, 6.0, extra=dict(extra),
                                   source="CoinGecko" if extra else "Coinbase")}
    asyncio.run(bot.check_moves())
    (cid, post), = bot.sent
    assert cid == 3 and post.embeds[0].title == f"🚀 BTC up 6.0% {window}"


@pytest.fixture
def recaps(tmp_path, monkeypatch):
    """A bot with trends channel 7, the recap builders stubbed and the bot's clock frozen."""
    bot = make_bot(tmp_path)
    calls = []

    async def trends_recap(b, period):
        assert b is bot
        calls.append(period)
        return [Post([discord.Embed(title=f"recap {period}")])]

    async def crypto_trends(b):
        calls.append("crypto")
        return [Post([discord.Embed(title="crypto recap")])]

    monkeypatch.setattr(briefs, "trends_recap", trends_recap)
    monkeypatch.setattr(briefs, "crypto_trends", crypto_trends)
    clock_ = SimpleNamespace(utc=utc(2026, 10, 6, 12))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock_.utc.astimezone(tz) if tz else clock_.utc.replace(tzinfo=None)

    monkeypatch.setattr(botmod, "datetime", Frozen)
    bot.channels.set(7, "trends")

    def at(when, cid=7):
        clock_.utc = when.astimezone(timezone.utc)
        local = when.astimezone(NEW_YORK)
        asyncio.run(bot._trend_recaps(cid, local, is_trading_day(local.date())))
        return list(calls)

    return SimpleNamespace(bot=bot, calls=calls, clock=clock_, at=at)


def test_daily_recap_after_the_close_once(recaps):
    assert recaps.at(ny(2026, 10, 6, 16, 19)) == []
    assert recaps.at(ny(2026, 10, 6, 16, 20)) == ["1D"]
    for hh, mm in ((16, 21), (16, 35), (16, 50), (17, 59)):
        recaps.at(ny(2026, 10, 6, hh, mm))
    assert recaps.calls == ["1D"]
    (cid, post), = recaps.bot.sent
    assert cid == 7 and post.embeds[0].title == "recap 1D"


def test_daily_recap_window_ends_at_six(recaps):
    assert recaps.at(ny(2026, 10, 6, 18, 0)) == []  # missed: skipped, not posted late
    assert recaps.at(ny(2026, 10, 7, 17, 59)) == ["1D"]


def test_weekly_recap_on_friday_after_the_daily(recaps):
    for mm in (20, 21, 34, 35, 36, 50, 51):
        recaps.at(ny(2026, 10, 9, 16, mm))
    assert recaps.calls == ["1D", "1W"]


def test_month_end_friday_gets_all_three(recaps):
    for hh, mm in ((16, 20), (16, 35), (16, 49), (16, 50), (17, 0), (18, 29)):
        recaps.at(ny(2026, 10, 30, hh, mm))
    assert recaps.calls == ["1D", "1W", "1M"]
    assert [p.embeds[0].title for _, p in recaps.bot.sent] == ["recap 1D", "recap 1W", "recap 1M"]


def test_a_late_start_posts_one_recap_per_tick(recaps):
    assert recaps.at(ny(2026, 10, 30, 17, 0)) == ["1D"]
    assert recaps.at(ny(2026, 10, 30, 17, 1)) == ["1D", "1W"]
    assert recaps.at(ny(2026, 10, 30, 17, 2)) == ["1D", "1W", "1M"]
    assert recaps.at(ny(2026, 10, 30, 17, 3)) == ["1D", "1W", "1M"]


def test_weekly_and_monthly_windows_close_too(recaps):
    assert recaps.at(ny(2026, 10, 30, 18, 15)) == ["1M"]  # the daily (till 18:00) and weekly (till 18:15) are over
    assert recaps.at(ny(2026, 10, 30, 18, 30)) == ["1M"]


@pytest.mark.parametrize("day, expected", [
    (date(2026, 4, 2), ["1D", "1W"]),  # Thursday before Good Friday
    (date(2026, 7, 2), ["1D", "1W"]),  # before the observed Independence Day
    (date(2026, 11, 27), ["1D", "1W"]),  # the day after Thanksgiving still trades
    (date(2026, 12, 24), ["1D", "1W"]),  # Christmas Eve, before a Friday Christmas
    (date(2026, 12, 31), ["1D", "1W", "1M"]),  # New Year's Day 2027 is a Friday
    (date(2026, 8, 31), ["1D", "1M"]),  # a Monday month end
    (date(2026, 5, 29), ["1D", "1W", "1M"]),  # May 31 is a Sunday
    (date(2026, 4, 30), ["1D", "1M"]),
    (date(2026, 11, 25), ["1D"]),
    (date(2026, 10, 6), ["1D"]),
    (date(2026, 3, 9), ["1D"]),  # the Monday after clocks went forward
    (date(2026, 11, 2), ["1D"]),  # the Monday after clocks went back
    (date(2026, 4, 3), []),  # Good Friday
    (date(2026, 7, 3), []),
    (date(2026, 11, 26), []),  # Thanksgiving
    (date(2026, 12, 25), []),
    (date(2026, 10, 10), []),  # Saturday
    (date(2026, 10, 31), []),  # a Saturday month end
    (date(2027, 5, 31), []),  # Memorial Day on the last day of May
])
def test_recaps_follow_the_nyse_calendar(recaps, day, expected):
    for hh, mm in ((16, 20), (16, 35), (16, 50), (17, 30)):
        recaps.at(ny(day.year, day.month, day.day, hh, mm))
    assert recaps.calls == expected


def test_crypto_recap_after_midnight_utc_every_day(recaps):
    assert recaps.at(utc(2026, 10, 10, 0, 4)) == []
    assert recaps.at(utc(2026, 10, 10, 0, 5)) == ["crypto"]  # a Saturday: crypto never closes
    assert recaps.at(utc(2026, 10, 10, 0, 6)) == ["crypto"]
    assert recaps.at(utc(2026, 10, 10, 2, 4)) == ["crypto"]
    assert recaps.at(utc(2026, 10, 11, 0, 30)) == ["crypto", "crypto"]
    assert recaps.at(utc(2026, 12, 25, 1, 0)) == ["crypto"] * 3  # evening of Christmas Eve in New York
    assert recaps.at(utc(2026, 12, 26, 0, 5)) == ["crypto"] * 4  # Christmas Day in New York


def test_crypto_recap_window_ends_two_hours_after(recaps):
    assert recaps.at(utc(2026, 10, 10, 2, 5)) == []
    assert recaps.at(utc(2026, 10, 10, 23, 59)) == []


def test_crypto_recap_after_new_york_midnight_is_not_utc_midnight(recaps):
    assert recaps.at(ny(2026, 10, 7, 0, 5)) == []  # 04:05 UTC: the window closed at 02:05


def test_recaps_are_not_posted_again_after_a_restart(recaps, tmp_path):
    recaps.at(ny(2026, 10, 6, 16, 20))
    again = make_bot(tmp_path)  # the same data folder: the same saved state
    asyncio.run(again._trend_recaps(7, ny(2026, 10, 6, 16, 30), True))
    assert recaps.calls == ["1D"] and not again.sent


def test_concurrent_ticks_post_the_daily_recap_once(recaps):
    bot = recaps.bot
    when = ny(2026, 10, 6, 16, 20)
    recaps.clock.utc = when.astimezone(timezone.utc)

    async def both():
        await asyncio.gather(*(bot._trend_recaps(7, when, True) for _ in range(4)))

    asyncio.run(both())
    assert recaps.calls == ["1D"]


def test_each_trends_channel_gets_its_own_recaps(recaps):
    recaps.bot.channels.set(8, "trends")
    recaps.at(ny(2026, 10, 6, 16, 20), cid=7)
    recaps.at(ny(2026, 10, 6, 16, 21), cid=8)
    recaps.at(ny(2026, 10, 6, 16, 22), cid=8)
    assert recaps.calls == ["1D", "1D"] and [c for c, _ in recaps.bot.sent] == [7, 8]


def test_job_briefs_runs_the_trend_recaps_unless_briefs_are_off(recaps):
    bot = recaps.bot
    bot.channels.set(8, "trends")
    bot.channels.update(8, briefs=False)
    recaps.clock.utc = ny(2026, 10, 9, 16, 25).astimezone(timezone.utc)
    asyncio.run(bot.job_briefs())
    assert recaps.calls == ["1D"] and [c for c, _ in bot.sent] == [7]
    recaps.clock.utc = ny(2026, 10, 9, 16, 40).astimezone(timezone.utc)
    asyncio.run(bot.job_briefs())
    assert recaps.calls == ["1D", "1W"] and [c for c, _ in bot.sent] == [7, 7]
    recaps.clock.utc = utc(2026, 10, 10, 0, 10)
    asyncio.run(bot.job_briefs())
    assert recaps.calls == ["1D", "1W", "crypto"]


def test_a_failing_recap_is_logged_and_the_other_channels_still_post(recaps, monkeypatch, caplog):
    bot = recaps.bot
    bot.channels.set(8, "trends")
    seen = []

    async def flaky(b, period):
        seen.append(period)
        if len(seen) == 1:
            raise RuntimeError("Yahoo HTTP 500")
        return [Post([discord.Embed(title="ok")])]

    monkeypatch.setattr(briefs, "trends_recap", flaky)
    recaps.clock.utc = ny(2026, 10, 6, 16, 20).astimezone(timezone.utc)
    asyncio.run(bot.job_briefs())
    assert seen == ["1D", "1D"] and [c for c, _ in bot.sent] == [8]
    assert "Brief for channel 7 failed" in caplog.text


def test_real_recaps_through_the_schedule(tmp_path, monkeypatch):
    snap, sp500, ndx = big_snapshot()
    bot = make_bot(tmp_path, desk=FakeDesk(snap, sp500, ndx))
    bot.channels.set(7, "trends")
    now = SimpleNamespace(utc=utc(2026, 10, 30, 20, 20))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.utc.astimezone(tz) if tz else now.utc.replace(tzinfo=None)

    monkeypatch.setattr(botmod, "datetime", Frozen)
    for hh, mm in ((16, 20), (16, 35), (16, 50)):
        when = ny(2026, 10, 30, hh, mm)
        now.utc = when.astimezone(timezone.utc)
        asyncio.run(bot._trend_recaps(7, when, True))
    assert [len(p.embeds) for _, p in bot.sent] == [2, 3, 4]
    for _, post in bot.sent:
        assert sum(len(e) for e in post.embeds) <= TOTAL
        for e in post.embeds:
            assert_fits(e)


def test_directory_refresh_reaches_the_trends_desk(tmp_path, monkeypatch):
    import marketbot.directory as dirmod
    old = make_directory()
    new = make_directory([Listing("NEW", "Newly Listed", STOCKS, "stock", 9e10, ("sp500",))])
    data = FakeData()
    data.directory, data.nasdaq, data.http, data.yahoo = old, object(), object(), object()
    engine = FakeEngine(data)
    engine.directory = old
    forgot = []
    engine.forget_lookups = lambda: forgot.append(True)
    bot = MarketBot(tmp_path, engine=engine, ai=NewsAI(readers=[]))
    assert bot.trends.directory is old and "NEW" not in bot.trends.universe()

    async def same(directory, *args):
        return directory

    monkeypatch.setattr(dirmod, "refresh", same)
    asyncio.run(bot.job_directory())
    assert bot.trends.directory is old and forgot == []

    async def fresh(directory, data_dir, http, nasdaq, yahoo):
        assert directory is old and data_dir == tmp_path
        return new

    monkeypatch.setattr(dirmod, "refresh", fresh)
    asyncio.run(bot.job_directory())
    assert bot.trends.directory is new and data.directory is new and engine.directory is new and forgot == [True]
    assert "NEW" in bot.trends.universe() and bot.trends.index_members("sp500")[-1] == "NEW"


# ----- channels and commands -----

def test_channel_store_keeps_trends_and_nvidia_channels(tmp_path):
    store = ChannelStore(tmp_path / "c.json")
    store.set(1, "trends", 9)
    store.set(2, "nvidia", 9)
    store.update(1, briefs=False, board_message_id=55)
    again = ChannelStore(tmp_path / "c.json")
    t, n = again.get(1), again.get(2)
    assert (t.kind, t.guild_id, t.briefs, t.board_message_id) == ("trends", 9, False, 55)
    assert n.kind == "nvidia" and t.market is None and n.market is None and t.symbols() == [] == n.symbols()
    assert [c for c, _ in again.of_kind("trends")] == [1] and [c for c, _ in again.of_kind("nvidia")] == [2]


def test_switching_a_stocks_channel_to_trends_resets_its_list_and_board(tmp_path):
    store = ChannelStore(tmp_path / "c.json")
    store.set(1, "stocks")
    store.update(1, watchlist=("AAPL",), board_message_id=77)
    cfg = store.set(1, "trends")
    assert cfg.kind == "trends" and cfg.watchlist == () and cfg.board_message_id is None


def test_channel_store_drops_unknown_kinds_and_settings(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"3": {"kind": "sports"}, "4": {"kind": "trends", "bogus": 1, "watchlist": []},
                                "5": {"kind": "nvidia", "alerts": False}, "6": {}}))
    store = ChannelStore(path)
    assert sorted(c for c, _ in store.all()) == [4, 5]
    assert store.get(4).kind == "trends" and store.get(5).alerts is False
    assert set(KINDS) == {"stocks", "crypto", "news", "research", "trends", "nvidia", "congress", "calendar",
                         "league"}
    assert KIND_NAMES["trends"] == "🔥 Trends" and KIND_NAMES["nvidia"] == "🟩 NVIDIA"


def test_setup_channel_list():
    assert len(SETUP_CHANNELS) == 9
    assert [k for k, _, _ in SETUP_CHANNELS] == list(KINDS)
    names = [n for _, n, _ in SETUP_CHANNELS]
    assert len(set(names)) == 9 and all(1 <= len(n) <= 100 and " " not in n for n in names)
    assert all(len(topic) <= 1024 for _, _, topic in SETUP_CHANNELS)  # Discord's channel topic limit
    assert set(INTROS) == set(KINDS) == set(KIND_NAMES)
    assert all(len(text) <= DESCRIPTION for text in INTROS.values())
    assert [c.value for c in KIND_CHOICES] == list(KINDS) and len(KIND_CHOICES) <= 25


def test_trends_and_nvidia_commands_are_registered(tmp_path):
    bot = make_bot(tmp_path)
    register_commands(bot)
    names = {c.name for c in bot.tree.get_commands()}
    assert {"trends", "nvidia", "setup", "channel", "brief", "status"} <= names
    trends = bot.tree.get_command("trends")
    period, market = trends.parameters
    assert [c.value for c in period.choices] == ["1D", "WTD", "MTD", "1W", "1M", "3M", "YTD", "1Y"] == list(PERIODS)
    assert not period.required and all(1 <= len(c.name) <= 100 for c in period.choices)
    assert [c.value for c in market.choices] == ["stocks", "sectors", "crypto"] and not market.required
    kinds = [c.value for c in bot.tree.get_command("brief").parameters[0].choices]
    assert {"trends-1D", "trends-1W", "trends-1M", "trends-crypto", "nvidia"} <= set(kinds)
    assert [c.value for c in bot.tree.get_command("channel").parameters[0].choices] == list(KINDS)


class Response:
    def __init__(self):
        self.deferred = None
        self.messages = []

    async def defer(self, **kwargs):
        self.deferred = kwargs

    def is_done(self):
        return self.deferred is not None or bool(self.messages)

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


def interaction(channel_id=1, guild=None):
    return SimpleNamespace(response=Response(), followup=Followup(), channel_id=channel_id, guild=guild,
                           guild_id=getattr(guild, "id", None))


def command(bot, name):
    register_commands(bot)
    return bot.tree.get_command(name).callback


def choice(value):
    return app_commands.Choice(name=value, value=value)


def test_trends_command_defaults_to_stocks_today(tmp_path):
    snap, sp500, ndx = big_snapshot()
    desk = FakeDesk(snap, sp500, ndx)
    bot = make_bot(tmp_path, desk=desk)
    trends = command(bot, "trends")
    it = interaction(channel_id=5)
    asyncio.run(trends(it))
    (content, kw), = it.followup.messages
    assert content is None and kw["embed"].title == "📈 Stocks · biggest moves today"
    assert desk.calls == [180] and it.response.deferred == {"thinking": True}


def test_trends_command_in_a_crypto_channel_shows_crypto(tmp_path):
    snap, sp500, ndx = big_snapshot()
    bot = make_bot(tmp_path, desk=FakeDesk(snap, sp500, ndx))
    bot.channels.set(5, CRYPTO)
    trends = command(bot, "trends")
    for period, words in (("1W", "over 7 days"), ("WTD", "over 7 days"), ("MTD", "over 30 days"),
                          ("1D", "in 24 hours")):
        it = interaction(channel_id=5)
        asyncio.run(trends(it, choice(period)))
        assert it.followup.messages[0][1]["embed"].title == f"🪙 Crypto · biggest moves {words}"
    it = interaction(channel_id=5)
    asyncio.run(trends(it))  # no period: today
    assert it.followup.messages[0][1]["embed"].title == "🪙 Crypto · biggest moves in 24 hours"


@pytest.mark.parametrize("period", list(PERIODS))
@pytest.mark.parametrize("market", ["stocks", "sectors", "crypto"])
def test_trends_command_any_period_and_market(tmp_path, period, market):
    snap, sp500, ndx = big_snapshot()
    bot = make_bot(tmp_path, desk=FakeDesk(snap, sp500, ndx))
    trends = command(bot, "trends")
    it = interaction()
    asyncio.run(trends(it, choice(period), choice(market)))
    e = it.followup.messages[0][1]["embed"]
    assert e.title == f"{E.TREND_MARKETS[market]} · biggest moves {label(period, market)}"
    assert_fits(e)


@pytest.mark.parametrize("outage, shown", [("Yahoo Finance: HTTP 429 Too Many Requests", "HTTP 429"),
                                           (None, "no data came back")])
def test_trends_command_with_no_data_says_the_sources_are_down(tmp_path, outage, shown):
    bot = make_bot(tmp_path, data=FakeData(outage=outage), desk=FakeDesk(Snapshot(NOW)))
    trends = command(bot, "trends")
    it = interaction()
    asyncio.run(trends(it, choice("1M"), choice("sectors")))
    (content, kw), = it.followup.messages
    assert "aren't answering" in content and shown in content and "embed" not in kw


def test_trends_command_shows_coins_when_only_coingecko_answered(tmp_path):
    bot = make_bot(tmp_path, desk=FakeDesk(Snapshot(NOW, coins=[coin("BTC", "Bitcoin", 60000, c24=3.0)])))
    trends = command(bot, "trends")
    it = interaction()
    asyncio.run(trends(it, None, choice("crypto")))
    e = it.followup.messages[0][1]["embed"]
    assert row_labels(e.fields[0].value) == ["BTC"]


class FakeChannel:
    def __init__(self, cid, name, topic=None):
        self.id, self.name, self.topic, self.mention = cid, name, topic, f"<#{cid}>"


class FakeCategory:
    def __init__(self, name):
        self.name = name
        self.text_channels = []


class FakeGuild:
    id = 99

    def __init__(self):
        self.categories = []
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True))
        self.next_id = 1000

    async def create_category(self, name):
        cat = FakeCategory(name)
        self.categories.append(cat)
        return cat

    async def create_text_channel(self, name, category=None, topic=None):
        self.next_id += 1
        ch = FakeChannel(self.next_id, name, topic)
        category.text_channels.append(ch)
        return ch


def test_setup_makes_the_six_channels_once(tmp_path):
    bot = make_bot(tmp_path)
    setup = command(bot, "setup")
    guild = FakeGuild()
    bot._last.update({"trends": 1.0, "nvidia": 1.0, "live": 1.0, "news": 1.0})
    it = interaction(guild=guild)
    asyncio.run(setup(it))
    cat, = guild.categories
    assert cat.name == "📊 Markets"
    assert [(c.name, c.topic) for c in cat.text_channels] == [(n, t) for _, n, t in SETUP_CHANNELS]
    assert [bot.channels.get(c.id).kind for c in cat.text_channels] == list(KINDS)
    assert all(bot.channels.get(c.id).guild_id == 99 for c in cat.text_channels)
    assert [p.embeds[0].title for _, p in bot.sent] == [f"{KIND_NAMES[k]} channel" for k in KINDS]
    assert [p.embeds[0].description for _, p in bot.sent] == [INTROS[k] for k in KINDS]
    assert "trends" not in bot._last and "nvidia" not in bot._last and bot._last == {"news": 1.0}
    assert it.followup.messages[0][0].startswith("Done: <#1001> <#1002>")
    asyncio.run(setup(interaction(guild=guild)))  # again: the same channels, no copies
    assert len(guild.categories) == 1 and len(cat.text_channels) == 9 and len(bot.channels.all()) == 9


def test_setup_without_manage_channels_asks_for_it(tmp_path):
    bot = make_bot(tmp_path)
    setup = command(bot, "setup")
    guild = FakeGuild()
    guild.me.guild_permissions.manage_channels = False
    it = interaction(guild=guild)
    asyncio.run(setup(it))
    assert "Manage Channels" in it.response.messages[0][0] and not guild.categories and not bot.channels.all()


def test_channel_command_makes_a_trends_channel(tmp_path):
    bot = make_bot(tmp_path)
    channel = command(bot, "channel")
    bot._last.update({"trends": 1.0, "live": 1.0, "nvidia": 1.0, "mood": 1.0})
    it = interaction(channel_id=42)
    asyncio.run(channel(it, choice("trends")))
    assert bot.channels.get(42).kind == "trends" and bot._last == {"mood": 1.0}
    (content, kw), = it.response.messages
    assert kw["embed"].title == "🔥 Trends channel" and kw["embed"].description == INTROS["trends"]
    asyncio.run(channel(interaction(channel_id=42), choice("trends"), False))
    assert bot.channels.get(42) is None


@pytest.mark.parametrize("kind, called", [("trends-1D", "1D"), ("trends-1W", "1W"), ("trends-1M", "1M"),
                                          ("trends-crypto", "crypto")])
def test_brief_command_posts_trend_recaps_on_demand(tmp_path, monkeypatch, kind, called):
    bot = make_bot(tmp_path)
    calls = []

    async def trends_recap(b, period):
        calls.append(period)
        return [Post([discord.Embed(title=f"recap {period}")]), Post([discord.Embed(title="more")])]

    async def crypto_trends(b):
        calls.append("crypto")
        return [Post([discord.Embed(title="crypto recap")])]

    monkeypatch.setattr(briefs, "trends_recap", trends_recap)
    monkeypatch.setattr(briefs, "crypto_trends", crypto_trends)
    brief = command(bot, "brief")
    bot.channels.set(3, "trends")
    it = interaction(channel_id=3)
    asyncio.run(brief(it, choice(kind)))
    assert calls == [called]
    (content, kw), = it.followup.messages
    assert kw["embeds"][0].title in (f"recap {called}", "crypto recap") and kw["files"] == []
    assert [(c, p.embeds[0].title) for c, p in bot.sent] == ([(3, "more")] if called != "crypto" else [])
