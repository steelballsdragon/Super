"""Congress trades: parsing the official House and Senate disclosures (real samples in tests/data), the pollers'
first fill and alerts, amendments, estimated returns and the views."""

import asyncio
import io
import json
import zipfile
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("numpy")

from marketbot.addons import congress as CG  # noqa: E402
from marketbot.apis import congress as C  # noqa: E402
from marketbot.yahoo import Bars  # noqa: E402

DATA = Path(__file__).parent / "data"


# ----- small parsers -----

@pytest.mark.parametrize("text, expected", [
    ("$1,001 - $15,000", (1001, 15000)), ("$15,001 - $50,000", (15001, 50000)),
    ("Over $50,000,000", (50_000_000, None)), ("Spouse/DC Over $1,000,000", (1_000_000, None)),
    ("$5,000,001 - $25,000,000", (5_000_001, 25_000_000)), ("", (0, None)), ("unknown", (0, None)),
])
def test_amounts(text, expected):
    assert C.parse_amount(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("10/5/2026", "2026-10-05"), ("09/17/2026", "2026-09-17"), ("10/01/2026 @ 3:04 PM", "2026-10-01"),
    ("13/40/2026", ""), ("", ""), ("yesterday", ""),
])
def test_dates(text, expected):
    assert C.iso_date(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("BRK.B", "BRK-B"), ("nvda", "NVDA"), ("MOG.A", "MOG-A"), ("--", None), ("N/A", None), ("", None),
    (None, None), ("TOO LONG TICKER", None), ("123", None),
])
def test_yahoo_tickers(text, expected):
    assert C.yahoo_ticker(text) == expected


def test_ticker_from_asset_names():
    assert C.ticker_from_name("Electronic Arts Inc. (EA)") == "EA"
    assert C.ticker_from_name("EA - Electronic Arts Inc") == "EA"
    assert C.ticker_from_name("GS Managed Structured Note") is None


# ----- House -----

def index_zip(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", b"\xef\xbb\xbf" + xml)  # the real files start with a BOM
        z.writestr("2026FD.txt", b"ignored")
    return buf.getvalue()


def test_house_index_keeps_ptrs_and_spots_paper_filings():
    rows = C.parse_house_index(index_zip((DATA / "house_index.xml").read_bytes()))
    assert [(r["last"], r["doc"], r["filed"], r["paper"]) for r in rows] == [
        ("Doggett", "20035580", "2026-10-05", False), ("Pelosi", "20035553", "2026-10-02", False),
        ("Wied", "9116361", "2026-10-02", True)]
    assert rows[0]["district"] == "TX37" and rows[0]["state"] == "TX" and rows[0]["year"] == "2026"


def test_house_index_without_xml_is_an_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError):
        C.parse_house_index(buf.getvalue())


def test_real_house_ptr_pdf_is_read():
    raw = C.parse_house_ptr((DATA / "house_ptr_20035580.pdf").read_bytes())
    assert [(r["ticker"], r["type"], r["tx_date"], r["amount"]) for r in raw] == [
        ("HD", "P", "09/17/2026", "$1,001 - $15,000"), ("IBM", "P", "09/10/2026", "$1,001 - $15,000"),
        ("JNJ", "P", "09/08/2026", "$1,001 - $15,000"), ("PPG", "P", "09/11/2026", "$1,001 - $15,000")]
    assert all(r["asset_type"] == "ST" and r["filing_status"] == "New" for r in raw)
    f = C.Filing("H:20035580", "house", "Lloyd Doggett", "Doggett", "TX", "TX37", "2026-10-05", "u", party="D")
    trades = C.house_trades(f, raw)
    assert [(t.kind, t.ticker, t.asset_type, t.owner, t.amount_low, t.amount_high) for t in trades][0] == (
        "purchase", "HD", "stock", "Self", 1001, 15000)


def test_house_rows_are_normalised():
    f = C.Filing("H:1", "house", "A B", "B", "CA", "CA11", "2026-10-02", "u", party="D")
    raw = [{"owner": "SP", "asset": "NVIDIA Corporation (NVDA)", "ticker": "NVDA", "asset_type": "OP", "type": "P",
            "tx_date": "06/20/2026", "amount": "$1,000,001 - $5,000,000", "description": "Purchased 50 call options"},
           {"owner": "JT", "asset": "Apple Inc. (AAPL)", "ticker": "AAPL", "asset_type": "ST", "type": "S (partial)",
            "tx_date": "06/21/2026", "amount": "$250,001 - $500,000"},
           {"owner": "", "asset": "x", "ticker": None, "asset_type": "ST", "type": "P", "tx_date": "06/21/2026",
            "amount": "$1,001 - $15,000", "filing_status": "Deleted"},
           {"owner": "", "asset": "y", "ticker": None, "asset_type": "GS", "type": "Q", "tx_date": "06/21/2026",
            "amount": "?"},
           {"owner": "", "asset": "z", "ticker": None, "asset_type": "GS", "type": "P", "tx_date": "", "amount": "?"}]
    trades = C.house_trades(f, raw)
    assert [(t.kind, t.ticker, t.asset_type, t.owner) for t in trades] == [
        ("purchase", "NVDA", "option", "Spouse"), ("sale_partial", "AAPL", "stock", "Joint")]
    assert trades[0].note.startswith("Purchased 50 call") and trades[0].amount_mid == 3_000_000.5


def test_a_scanned_pdf_has_no_rows():
    from matplotlib.figure import Figure
    buf = io.BytesIO()
    Figure().savefig(buf, format="pdf")  # a page with no text, like a scanned filing
    assert C.parse_house_ptr(buf.getvalue()) == []


# ----- Senate -----

def test_real_senate_report_is_read():
    rows = C.parse_senate_report((DATA / "senate_ptr_view.html").read_text())
    assert [(r["Ticker"], r["Type"], r["Amount"]) for r in rows] == [
        ("JPM", "Sale (Partial)", "$15,001 - $50,000"), ("ADI", "Sale (Partial)", "$1,001 - $15,000")]
    f = C.Filing("S:x", "senate", "Sheldon Whitehouse", "Whitehouse", "RI", "", "2026-10-01", "u", party="D")
    trades = C.senate_trades(f, rows)
    assert [(t.kind, t.ticker, t.owner, t.asset_type) for t in trades] == [
        ("sale_partial", "JPM", "Spouse", "stock"), ("sale_partial", "ADI", "Spouse", "stock")]


def test_senate_rows_find_tickers_in_names_and_skip_junk():
    f = C.Filing("S:y", "senate", "A", "A", "ME", "", "2026-09-14", "u")
    rows = [{"Transaction Date": "08/05/2026", "Owner": "Spouse", "Ticker": "--", "Asset Name": "EA - Electronic Arts Inc",
             "Asset Type": "Stock", "Type": "Sale (Full)", "Amount": "$15,001 - $50,000"},
            {"Transaction Date": "08/05/2026", "Owner": "Self", "Ticker": "-- AMCR",
             "Asset Name": "BERY (Exchanged) Amcor plc (Received)", "Asset Type": "Stock", "Type": "Exchange",
             "Amount": "$1,001 - $15,000"},
            {"Transaction Date": "08/05/2026", "Owner": "Self", "Ticker": "--", "Asset Name": "Some muni",
             "Asset Type": "Municipal Security", "Type": "Purchase", "Amount": "$1,001 - $15,000"},
            {"Transaction Date": "bad", "Owner": "Self", "Ticker": "X", "Asset Name": "x", "Asset Type": "Stock",
             "Type": "Purchase", "Amount": "$1,001 - $15,000"},
            {"Transaction Date": "08/05/2026", "Owner": "Self", "Ticker": "X", "Asset Name": "x",
             "Asset Type": "Stock", "Type": "Gift", "Amount": "$1,001 - $15,000"}]
    trades = C.senate_trades(f, rows)
    assert [(t.ticker, t.kind, t.asset_type) for t in trades] == [
        ("EA", "sale", "stock"), ("AMCR", "exchange", "stock"), (None, "purchase", "bond")]


def test_senate_search_rows_become_filings():
    data = json.loads((DATA / "senate_search.json").read_text())
    filings = [C.senate_filing(r) for r in data["data"]]
    assert [(f.member, f.last, f.filed, f.paper, f.amendment) for f in filings] == [
        ("Sheldon Whitehouse", "Whitehouse", "2026-10-01", False, False),
        ("James Conley Justice", "Justice", "2026-09-28", False, False),
        ("Richard Blumenthal", "Blumenthal", "2026-09-28", False, False),
        ("RICHARD BLUMENTHAL", "BLUMENTHAL", "2026-08-31", True, False),
        ("John Boozman", "Boozman", "2026-08-24", False, True)]
    assert filings[0].id == "S:6bf3b6f7-9e1b-499a-bd5a-990292ce2e72"
    assert C.senate_filing(["a", "b", "c", "no link", "01/01/2026"]) is None


def test_roster_finds_party_by_seat_and_by_name():
    data = [{"id": {"bioguide": "P000197"}, "name": {"first": "Nancy", "last": "Pelosi", "official_full": "Nancy Pelosi"},
             "terms": [{"type": "rep", "state": "CA", "district": 11, "party": "Democrat"}]},
            {"id": {"bioguide": "S1"}, "name": {"first": "Rick", "last": "Scott"},
             "terms": [{"type": "sen", "state": "FL", "party": "Republican"}]},
            {"id": {"bioguide": "S2"}, "name": {"first": "Tim", "last": "Scott"},
             "terms": [{"type": "sen", "state": "SC", "party": "Republican"}]},
            {"broken": True}]
    r = C.parse_roster(data)
    assert r.find("house", "Pelosi", district="ca11")["party"] == "D"
    assert r.find("senate", "Scott") is None  # two Scotts: no guessing
    assert r.find("senate", "Scott", first="Tim")["state"] == "SC"
    assert r.find("senate", "Scott", state="FL")["first"] == "Rick"


# ----- the desk (sources faked) -----

TODAY = date.today()


def d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


class FakeHouse:
    def __init__(self, rows, pdfs):
        self.rows, self.pdfs = rows, pdfs
        self.last_modified = {}
        self.fetched = []

    async def index(self, year, force=False):
        return [r for r in self.rows if r["year"] == str(year)]

    async def ptr(self, year, doc):
        self.fetched.append(doc)
        return self.pdfs.get(doc)


class FakeSenate:
    def __init__(self, rows, pages):
        self.rows, self.pages = rows, pages

    async def search(self, since, start=0, length=100):
        rows = [r for r in self.rows if C.iso_date(r[4]) >= since.isoformat()]
        return len(rows), rows[start:start + length]

    async def report(self, path):
        return self.pages.get(path)

    async def close(self):
        pass


def house_row(doc, filed, last="Doggett", district="TX37", paper=False):
    return {"doc": doc, "year": filed[:4], "first": "Lloyd", "last": last, "district": district,
            "state": district[:2], "filed": filed, "paper": paper}


def senate_row(uuid, filed_iso, first="Sheldon", last="Whitehouse", amend=False):
    y, m, dd = filed_iso.split("-")
    title = "Periodic Transaction Report" + (" (Amendment 1)" if amend else "")
    return [first, last, f"{last}, {first} (Senator)", f'<a href="/search/view/ptr/{uuid}/">{title}</a>',
            f"{m}/{dd}/{y}"]


def make_desk(tmp_path, house_rows=(), senate_rows=(), pages=None, pdf=None):
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    boards = []

    async def show_board(cid, embed):
        boards.append((cid, embed))

    from marketbot.channels import ChannelStore
    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(7, "congress", 1)
    bot = SimpleNamespace(engine=SimpleNamespace(data=SimpleNamespace(http=object()), cache=None), data_dir=tmp_path,
                          channels=channels, send=send, show_board=show_board)
    desk = CG.CongressDesk.__new__(CG.CongressDesk)
    CG.Feature.__init__(desk, bot)
    desk.http = None
    desk.house = FakeHouse(list(house_rows), {r["doc"]: pdf for r in house_rows if not r["paper"]})
    desk.senate = FakeSenate(list(senate_rows), pages or {})
    desk.path = tmp_path / "congress.json"
    desk.filings, desk.trades, desk.synced, desk.house_index = {}, [], {}, {}
    desk.roster = C.parse_roster([
        {"id": {}, "name": {"first": "Lloyd", "last": "Doggett", "official_full": "Lloyd Doggett"},
         "terms": [{"type": "rep", "state": "TX", "district": 37, "party": "Democrat"}]},
        {"id": {}, "name": {"first": "Sheldon", "last": "Whitehouse", "official_full": "Sheldon Whitehouse"},
         "terms": [{"type": "sen", "state": "RI", "party": "Democrat"}]}])
    desk.roster_at = 10 ** 12
    desk.scores, desk.scores_at = {}, 0
    desk._lock = asyncio.Lock()
    return desk, sent, boards


PDF = (DATA / "house_ptr_20035580.pdf").read_bytes()
SENATE_PAGE = (DATA / "senate_ptr_view.html").read_text()


def test_first_fill_posts_nothing_then_new_filings_are_posted(tmp_path, monkeypatch):
    monkeypatch.setattr(CG.asyncio, "sleep", _no_sleep)
    rows = [house_row("20030001", d(40)), house_row("20030002", d(10)), house_row("9100001", d(5), paper=True)]
    desk, sent, _ = make_desk(tmp_path, rows, pdf=PDF)
    asyncio.run(desk.job_house())
    assert len(desk.filings) == 3 and desk.synced["house"] and not sent
    assert desk.filings["H:9100001"]["paper"] and desk.filings["H:20030002"]["party"] == "D"
    assert len(desk.trades) == 4  # the same trades in two filings are kept once (from the later filing)
    assert {t["filing"] for t in desk.trades} == {"H:20030002"}
    desk.house.rows.append(house_row("20030003", d(0)))
    desk.house.pdfs["20030003"] = PDF
    desk.house.rows.append(house_row("20030004", d(20)))  # found late: history, not news
    desk.house.pdfs["20030004"] = PDF
    asyncio.run(desk.job_house())
    assert [p.embeds[0].title for _, p in sent] == ["🏛️ Rep. Lloyd Doggett (D-TX37) disclosed 4 trades"]
    saved = json.loads((tmp_path / "congress.json").read_text())
    assert set(saved["filings"]) == {"H:20030001", "H:20030002", "H:9100001", "H:20030003", "H:20030004"}


def test_a_filing_not_posted_yet_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr(CG.asyncio, "sleep", _no_sleep)
    desk, _, _ = make_desk(tmp_path, [house_row("20030009", d(1))], pdf=None)
    asyncio.run(desk.job_house())
    assert not desk.filings
    desk.house.pdfs["20030009"] = PDF
    asyncio.run(desk.job_house())
    assert "H:20030009" in desk.filings


def test_the_first_fill_spreads_over_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(CG.asyncio, "sleep", _no_sleep)
    monkeypatch.setitem(CG.PER_RUN, "house", 2)
    rows = [house_row(f"2003{i:04d}", d(i), paper=True) for i in range(5)]
    desk, sent, _ = make_desk(tmp_path, rows)
    asyncio.run(desk.job_house())
    assert len(desk.filings) == 2 and not desk.synced.get("house")
    assert set(desk.filings) == {"H:20030000", "H:20030001"}  # newest first
    asyncio.run(desk.job_house())
    asyncio.run(desk.job_house())
    assert len(desk.filings) == 5 and desk.synced["house"] and not sent


def test_senate_amendments_replace_restated_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(CG.asyncio, "sleep", _no_sleep)
    pages = {"/search/view/ptr/a1/": SENATE_PAGE, "/search/view/ptr/a2/": SENATE_PAGE}
    desk, sent, _ = make_desk(tmp_path, senate_rows=[senate_row("a1", d(30))], pages=pages)
    asyncio.run(desk.job_senate())
    assert len(desk.trades) == 2 and desk.synced["senate"]
    desk.senate.rows.append(senate_row("a2", d(0), amend=True))
    asyncio.run(desk.job_senate())
    assert len(desk.trades) == 2 and {t["filing"] for t in desk.trades} == {"S:a2"}
    assert desk.filings["S:a2"]["party"] == "D" and desk.filings["S:a2"]["state"] == "RI"
    assert sent[0][1].embeds[0].title == "🏛️ Sen. Sheldon Whitehouse (D-RI) disclosed 2 trades"


def test_alerts_respect_the_channel_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(CG.asyncio, "sleep", _no_sleep)
    desk, sent, _ = make_desk(tmp_path, [house_row("20030001", d(3))], pdf=PDF)
    desk.synced["house"] = True
    desk.bot.channels.update(7, alerts=False)
    asyncio.run(desk.job_house())
    assert not sent and "H:20030001" in desk.filings


def test_a_broken_pdf_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(CG.asyncio, "sleep", _no_sleep)
    desk, _, _ = make_desk(tmp_path, [house_row("20030001", d(3)), house_row("20030002", d(2))], pdf=b"%PDF-broken")
    desk.house.pdfs["20030002"] = PDF
    asyncio.run(desk.job_house())
    assert "H:20030002" in desk.filings and "H:20030001" not in desk.filings


async def _no_sleep(*_):
    pass


# ----- returns and views -----

def bars(closes, start_iso):
    t0 = int(np.datetime64(start_iso, "s").astype(int))
    t = t0 + np.arange(len(closes), dtype=np.int64) * 86400
    c = np.asarray(closes, dtype=float)
    return Bars("X", t, c, c, c, c, np.ones(len(c)))


def trade(member="A B", ticker="NVDA", kind="purchase", tx=None, filed=None, low=1001, high=15000, filing="H:1",
          atype="stock", party="D"):
    return asdict(C.Trade(filing, "house", member, "CA", party, "Self", f"{ticker} Inc", atype, ticker, kind,
                          tx or d(10), filed or d(5), low, high, f"${low:,} - ${high:,}"))


def test_score_trade_from_the_trade_and_the_filing_dates():
    start = d(20)
    b = bars([100.0] * 10 + [110.0] * 10 + [121.0], start)
    spx = bars([1000.0] * 15 + [1050.0] * 6, start)
    s = CG.score_trade(trade(tx=d(20), filed=d(10)), b, spx)
    assert s["ret"] == pytest.approx(0.21) and s["follow"] == pytest.approx(0.1)
    assert s["spx"] == pytest.approx(0.05) and s["excess"] == pytest.approx(0.16)
    assert CG.score_trade(trade(tx=d(400)), b, spx) is None  # before the history: no price


def test_member_stats_weigh_by_amount():
    big = trade(ticker="AAA", low=1_000_001, high=5_000_000)
    small = trade(ticker="BBB", low=1001, high=15000, kind="purchase")
    sale = trade(ticker="CCC", kind="sale")
    scores = {CG.tkey(big): {"ret": 0.10, "excess": 0.05}, CG.tkey(small): {"ret": -0.5, "excess": -0.6}}
    st = CG.member_stats([big, small, sale], scores, d(365))
    assert st["trades"] == 3 and st["buys"] == 2 and st["sells"] == 1 and st["scored"] == 2
    assert st["ret"] == pytest.approx(0.0975, abs=0.002) and st["hit"] == 0.5


def test_board_member_and_ticker_views_fit_discord(tmp_path):
    desk, _, _ = make_desk(tmp_path)
    for i in range(40):
        f = C.Filing(f"H:{i}", "house", f"Member Number {i % 7}", "N", "CA", "CA11", d(i % 30), "https://x.test/f",
                     party="DRI"[i % 3])
        desk.filings[f.id] = {**asdict(f), "count": 3, "seen": i}
        for k in range(3):
            desk.trades.append(trade(member=f.member, ticker=["NVDA", "AAPL", "MSFT", "TSLA"][(i + k) % 4],
                                     kind=["purchase", "sale", "sale_partial", "exchange"][k % 4], tx=d(i % 30 + 5),
                                     filing=f.id, party=f.party))
    for t in desk.trades:
        if t["kind"] == "purchase":
            desk.scores[CG.tkey(t)] = {"ret": 0.1, "spx": 0.05, "excess": 0.05}
    board = desk.board()
    names = [f.name for f in board.fields]
    assert names[:2] == ["🆕 Latest filings", "🏆 Best stock pickers (buys over 12 months, est.)"]
    assert "🟢 Most bought (30 days)" in names and len(board) <= 6000
    member = desk.member_embed("Member Number 3")
    assert member.title.startswith("🏛️ Rep. Member Number 3") and "vs the S&P 500" in member.description
    assert len(member) <= 6000
    tick = desk.ticker_embed("NVDA")
    assert "members" in tick.description and len(tick) <= 6000
    assert desk.ticker_embed("ZZZZ").description.startswith("No member")
    assert desk.members_matching("number 1") == ["Member Number 1"]


def test_empty_board_says_its_filling_in(tmp_path):
    desk, _, _ = make_desk(tmp_path)
    assert desk.board().fields[0].name == "Filling in"


def test_filing_alert_flags_late_big_and_option_trades():
    f = {"chamber": "house", "member": "Nancy Pelosi", "party": "D", "district": "CA11", "state": "CA",
         "filed": d(0), "url": "https://x.test/p", "paper": False}
    trades = [trade(member="Nancy Pelosi", tx=d(60), low=1_000_001, high=5_000_000, atype="option")] + \
             [trade(member="Nancy Pelosi", ticker=f"T{i}", tx=d(5)) for i in range(14)]
    e = CG.filing_embed(f, trades, {})
    assert e.title == "🏛️ Rep. Nancy Pelosi (D-CA11) disclosed 15 trades"
    assert "…and 3 more" in e.description and "(option)" in e.description.splitlines()[0]
    flags = e.fields[0].value
    assert "filed 60 days after" in flags and "over $1M" in flags and "options" in flags
    paper = CG.filing_embed({**f, "paper": True}, [], {})
    assert "paper filing" in paper.title and "scanned" in paper.description


def test_money_and_bands():
    assert [CG.money(v) for v in (0, 1001, 15000, 1_000_001, 5_000_000, 25_500_000)] == [
        "$0", "$1K", "$15K", "$1M", "$5M", "$26M"]
    assert CG.band({"amount_low": 1001, "amount_high": 15000}) == "$1K–$15K"
    assert CG.band({"amount_low": 50_000_000, "amount_high": None}) == "over $50M"
