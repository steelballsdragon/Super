"""Insiders (SEC Form 4) and funds (13F): parsing real SEC samples (tests/data), what's worth a post, cluster
buys, quarterly fund changes and the desk's first run."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from marketbot.addons import insiders as INS  # noqa: E402
from marketbot.apis import sec as S  # noqa: E402

DATA = Path(__file__).parent / "data"


def read(name):
    return (DATA / name).read_bytes()


# ----- parsing -----

def test_open_market_purchase_by_a_director():
    f = S.parse_form4(read("form4_open_market_purchase_VRA.xml"))
    assert (f.issuer, f.symbol) == ("Vera Bradley, Inc.", "VRA")
    assert [(o.name, o.role) for o in f.owners] == [("Brockman Ivan", "Director")]
    [t] = f.trades
    assert (t.code, t.day, t.shares, t.price, t.acquired, t.planned) == ("P", "2026-10-06", 10000, 5.02, True, False)
    assert t.value == pytest.approx(50200)


def test_planned_sale_by_an_officer():
    f = S.parse_form4(read("form4_aapl_10b5-1_sale_trimmed.xml"))
    assert f.symbol == "AAPL" and f.owners[0].role == "Executive Chair"
    assert all(t.code == "S" and not t.acquired and t.planned for t in f.trades)


def test_feed_entries():
    feed = S.parse_feed(read("getcurrent_form4_owner_only.atom"))
    assert {e["role"] for e in feed} == {"Issuer", "Reporting"}
    issuer = next(e for e in feed if e["role"] == "Issuer")
    assert issuer["name"] == "BioNTech SE" and issuer["accession"] == "0002123975-26-000033"
    assert issuer["folder"].endswith("/1776985/000212397526000033")


def test_13f_tables_in_dollars_thousands_and_namespaces():
    b = S.parse_13f(read("13f_infotable_berkshire_2026Q2_trimmed.xml"))
    assert (b[0].name, b[0].cusip, b[0].value) == ("ALLY FINL INC", "02005N100", 706049871.0)
    bw = S.parse_13f(read("13f_infotable_bridgewater_ns1_figi_trimmed.xml"))
    assert len(bw) == 3 and bw[0].figi == "BBG001S5T7X2" and bw == sorted(bw, key=lambda h: -h.value)
    old = S.parse_13f(read("13f_infotable_berkshire_2022Q3_THOUSANDS_trimmed.xml"), in_thousands=True)
    assert old[0].value == 1_906_458_000  # $74/share for 25.6M shares: the old filings were in thousands


def test_13f_rows_with_the_same_cusip_are_summed():
    xml = b"""<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
        <value>100</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt></shrsOrPrnAmt></infoTable>
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
        <value>50</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt></shrsOrPrnAmt></infoTable>
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
        <value>7</value><shrsOrPrnAmt><sshPrnamt>1</sshPrnamt></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
    </informationTable>"""
    rows = S.parse_13f(xml)
    assert [(h.value, h.shares, h.put_call) for h in rows] == [(150, 15, ""), (7, 1, "Put")]


# ----- what's worth a post -----

def form4(code="P", shares=10000, price=50.0, planned=False, director=True, officer=False, ten=False, title=""):
    f = S.Form4("acc", 1, "Acme Corp", "ACME", url="https://x.test/f")
    f.owners = [S.Owner("DOE JANE", director, officer, ten, title)]
    f.trades = [S.InsiderTrade(code, "2026-10-06", shares, price, code == "P", 50000, planned)]
    return f


def test_buys_of_100k_by_insiders_are_notable():
    assert INS.notable(form4(shares=10000, price=50))[0] == "buy"  # $500K
    assert INS.notable(form4(shares=1000, price=50)) is None  # $50K
    assert INS.notable(form4(director=False, ten=True))[0] == "buy"  # a 10% owner
    assert INS.notable(form4(director=False)) is None  # no insider role


def test_only_big_unplanned_sales_are_notable():
    assert INS.notable(form4("S", shares=300_000, price=50, officer=True)) == ("sale", form4("S").trades) or \
        INS.notable(form4("S", shares=300_000, price=50, officer=True))[0] == "sale"
    assert INS.notable(form4("S", shares=300_000, price=50, planned=True, officer=True)) is None
    assert INS.notable(form4("S", shares=1000, price=50, officer=True)) is None


def test_insider_embed():
    f = form4(shares=20000, price=25.5, officer=True, title="CEO")
    e = INS.insider_embed(f, "buy", f.trades, ["Smith John"])
    assert e.title == "🟢 Cluster buy: ACME insider bought $510K"
    assert "**Doe Jane (CEO)**" in e.description and "Also buying" in e.description and len(e) <= 6000


def holding(cusip, name, value, shares, pc=""):
    return S.Holding(cusip, name, "COM", value, shares, pc)


def test_fund_changes_find_new_added_trimmed_and_sold():
    before = [holding("A", "ALPHA", 100, 10), holding("B", "BETA", 100, 10), holding("C", "GAMMA", 100, 10),
              holding("D", "DELTA", 100, 10)]
    now = [holding("A", "ALPHA", 200, 20), holding("B", "BETA", 50, 5), holding("C", "GAMMA", 101, 10.2),
           holding("E", "EPSILON", 300, 30), holding("F", "PUTS", 999, 9, "Put")]
    ch = INS.fund_changes(now, before, {"A": "AAA", "E": "EEE"})
    assert [n for n, _ in ch["new"]] == ["EEE"] and ch["gone"] == ["Delta"]
    assert [(n, round(p, 2)) for n, p, _ in ch["adds"]] == [("AAA", 1.0)]
    assert [(n, round(p, 2)) for n, p, _ in ch["trims"]] == [("Beta", -0.5)]
    assert ch["count"] == 4 and ch["total"] == 651  # put rows left out
    e = INS.fund_embed("Test Fund", {"period": "2026-06-30", "filed": "2026-08-14"}, ch, {"period": "2026-03-31"})
    assert [f.name for f in e.fields] == ["Top holdings", "🆕 New positions", "➕ Added to", "➖ Trimmed",
                                          "❌ Sold out of"]
    first = INS.fund_embed("Test Fund", {"period": "x", "filed": "y"}, ch, None)
    assert "first filing on record" in first.description and len(first.fields) == 1


# ----- the desk (SEC faked) -----

class FakeSEC:
    def __init__(self, feed, forms, filings=None):
        self._feed, self._forms, self._filings = feed, forms, filings or {}
        self.api = SimpleNamespace(status_line=lambda: "ok")

    async def feed(self, start=0):
        return self._feed if start == 0 else []

    async def form4(self, entry):
        return self._forms.get(entry["accession"])

    async def latest_13f(self, cik, count=2):
        return self._filings.get(cik, [])[:count]

    async def holdings(self, cik, filing):
        return [holding("A", "ALPHA", 100, 10)]

    async def tickers_for(self, holdings):
        return {h.cusip: "AAA" for h in holdings}


def make_desk(tmp_path, sec):
    from marketbot.channels import ChannelStore
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(3, "congress", 1)
    bot = SimpleNamespace(engine=SimpleNamespace(data=SimpleNamespace(http=object())), data_dir=tmp_path,
                          channels=channels, send=send)
    desk = INS.InsiderDesk.__new__(INS.InsiderDesk)
    INS.Feature.__init__(desk, bot)
    desk.sec = sec
    desk.finnhub = SimpleNamespace(enabled=False)
    desk.path = tmp_path / "insiders.json"
    desk.seen, desk.buys, desk.funds, desk.started = {}, [], {}, False
    return desk, sent


def entry(acc, role="Issuer"):
    return {"accession": acc, "role": role, "folder": f"https://x.test/{acc}", "cik": 1, "name": "X", "form": "4"}


def test_first_run_is_silent_then_buys_post_with_clusters(tmp_path, monkeypatch):
    class Weekday(INS.datetime):
        @classmethod
        def now(cls, tz=None):
            return INS.datetime(2026, 10, 7, 12, 0, tzinfo=tz)

    monkeypatch.setattr(INS, "datetime", Weekday)
    a = form4(shares=10000, price=50)
    b = form4(shares=10000, price=50)
    b.owners = [S.Owner("ROE RICHARD", True, False, False, "")]
    small = form4(shares=10, price=5)
    sec = FakeSEC([entry("old1"), entry("old2")], {"old1": a})
    desk, sent = make_desk(tmp_path, sec)
    asyncio.run(desk.job_form4())
    assert not sent and set(desk.seen) == {"old1", "old2"}
    sec._feed = [entry("n1"), entry("n1", "Reporting"), entry("n2"), entry("n3"), entry("old1")]
    sec._forms.update({"n1": a, "n2": b, "n3": small})
    asyncio.run(desk.job_form4())
    titles = [p.embeds[0].title for _, p in sent]
    assert titles == ["🟢 ACME insider bought $500K", "🟢 Cluster buy: ACME insider bought $500K"]
    assert "Doe Jane" in sent[1][1].embeds[0].description
    asyncio.run(desk.job_form4())
    assert len(sent) == 2  # seen ones aren't posted again
    saved = INS.read_json(tmp_path / "insiders.json", {})
    assert {"n1", "n2", "n3"} <= set(saved["seen"]) and len(saved["buys"]) == 2


def test_funds_post_only_new_filings_after_the_first_run(tmp_path, monkeypatch):
    sec = FakeSEC([], {}, {1067983: [{"accession": "q2", "filed": "2026-08-14", "period": "2026-06-30"},
                                     {"accession": "q1", "filed": "2026-05-15", "period": "2026-03-31"}]})
    desk, sent = make_desk(tmp_path, sec)
    monkeypatch.setattr(INS, "FUNDS", {"Berkshire Hathaway (Buffett)": 1067983})
    monkeypatch.setattr(INS.asyncio, "sleep", _no_sleep)
    asyncio.run(desk.job_funds())
    assert not sent and desk.funds == {"1067983": "q2"}
    sec._filings[1067983].insert(0, {"accession": "q3", "filed": "2026-11-14", "period": "2026-09-30"})
    asyncio.run(desk.job_funds())
    asyncio.run(desk.job_funds())
    assert [p.embeds[0].title for _, p in sent] == ["🏦 Berkshire Hathaway (Buffett): holdings at 2026-09-30"]


def test_no_smart_money_channel_means_no_feed_calls(tmp_path):
    sec = FakeSEC([entry("x")], {})
    desk, sent = make_desk(tmp_path, sec)
    desk.bot.channels.remove(3)
    asyncio.run(desk.job_form4())
    assert not desk.seen and not sent


def test_money():
    assert [INS.money(v) for v in (950, 50200, 1_500_000, 25_000_000_000)] == ["$950", "$50K", "$1.5M", "$25B"]


async def _no_sleep(*_):
    pass
