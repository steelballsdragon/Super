"""SEC EDGAR (free, no key): insiders' trades (Form 4) and big funds' quarterly holdings (13F-HR).

EDGAR asks for a User-Agent naming the app and a contact email (set SEC_USER_AGENT to e.g. "MarketBot
you@example.com") and at most 10 requests a second per IP; the bot keeps to 4. CUSIPs in 13F tables are mapped to
tickers through OpenFIGI (keyless: 25 requests a minute, 10 CUSIPs each) and the answers are kept for good.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from ..http import Http, HttpError
from ..storage import read_json, write_json
from . import Api, ApiError

log = logging.getLogger(__name__)

WWW = "https://www.sec.gov"
DATA = "https://data.sec.gov"
FORM4_FEED = f"{WWW}/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count=100&output=atom"
TICKERS = f"{WWW}/files/company_tickers.json"
OPENFIGI = "https://api.openfigi.com/v3/mapping"

FUNDS = {  # name -> CIK (checked against EDGAR)
    "Berkshire Hathaway (Buffett)": 1067983, "Bridgewater Associates (Dalio)": 1350694,
    "Pershing Square (Ackman)": 2026053, "ARK Invest (Wood)": 1697748, "Scion (Burry)": 1649339,
    "Renaissance Technologies": 1037389, "Citadel Advisors (Griffin)": 1423053, "Soros Fund Management": 1029160,
    "Tiger Global": 1167483, "Appaloosa (Tepper)": 1656456, "Duquesne (Druckenmiller)": 1536411,
    "Third Point (Loeb)": 1040273, "Baupost Group (Klarman)": 1061768, "Coatue Management": 1135730,
    "Gates Foundation Trust": 1166559, "Elliott Management": 1791786, "Icahn Capital": 921669,
    "Lone Pine Capital": 1061165, "Viking Global": 1103804,
}


def user_agent() -> str:
    ua = (os.environ.get("SEC_USER_AGENT") or "").strip()
    return ua or "MarketBot personal Discord market bot (set SEC_USER_AGENT to add a contact email)"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, *path):
    """The first element down a path of local names (namespaces and prefixes ignored)."""
    for name in path:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el, *path) -> str:
    found = _find(el, *path)
    if found is None:
        return ""
    v = _find(found, "value")
    return ((v if v is not None else found).text or "").strip()


def _num(text: str) -> float | None:
    try:
        v = float((text or "").replace(",", ""))
    except ValueError:
        return None
    return v if v == v else None


def _bool(text: str) -> bool:
    return (text or "").strip().lower() in ("1", "true", "yes", "y")


@dataclass
class Owner:
    name: str
    director: bool
    officer: bool
    ten_percent: bool
    title: str

    @property
    def role(self) -> str:
        if self.officer and self.title:
            return self.title
        return "Director" if self.director else "10% owner" if self.ten_percent else "Officer" if self.officer else \
            "Insider"


@dataclass
class InsiderTrade:
    code: str  # P purchase, S sale
    day: str
    shares: float
    price: float
    acquired: bool
    owned_after: float | None
    planned: bool  # under a Rule 10b5-1 plan

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass
class Form4:
    accession: str
    issuer_cik: int
    issuer: str
    symbol: str
    owners: list[Owner] = field(default_factory=list)
    trades: list[InsiderTrade] = field(default_factory=list)
    url: str = ""


def parse_form4(xml: bytes, accession: str = "", url: str = "") -> Form4:
    """An ownershipDocument: the issuer, the reporting owners and the open-market buys and sells (codes P and S
    with a price), each flagged if a 10b5-1 plan footnote covers it."""
    root = ET.fromstring(xml)
    issuer = _find(root, "issuer")
    f = Form4(accession, int(_text(issuer, "issuerCik") or 0), _text(issuer, "issuerName"),
              _text(issuer, "issuerTradingSymbol").upper(), url=url)
    for o in (c for c in root if _local(c.tag) == "reportingOwner"):
        rel = _find(o, "reportingOwnerRelationship")
        f.owners.append(Owner(_text(o, "reportingOwnerId", "rptOwnerName"), _bool(_text(rel, "isDirector")),
                              _bool(_text(rel, "isOfficer")), _bool(_text(rel, "isTenPercentOwner")),
                              _text(rel, "officerTitle")))
    plan_all = _bool(_text(root, "aff10b5One"))
    notes = {}
    fn = _find(root, "footnotes")
    for n in (fn if fn is not None else []):
        notes[n.get("id", "")] = "".join(n.itertext())
    table = _find(root, "nonDerivativeTable")
    for t in (c for c in (table if table is not None else []) if _local(c.tag) == "nonDerivativeTransaction"):
        code = _text(t, "transactionCoding", "transactionCode").upper()
        shares = _num(_text(t, "transactionAmounts", "transactionShares"))
        price = _num(_text(t, "transactionAmounts", "transactionPricePerShare"))
        ad = _text(t, "transactionAmounts", "transactionAcquiredDisposedCode").upper()
        if code not in ("P", "S") or not shares or not price or price <= 0:
            continue
        ids = {x.get("id") for x in t.iter() if _local(x.tag) == "footnoteId"}
        planned = plan_all and (any("10b5-1" in notes.get(i, "") for i in ids) or not any(
            "10b5-1" in v for v in notes.values()))
        f.trades.append(InsiderTrade(code, _text(t, "transactionDate"), shares, price, ad == "A",
                                     _num(_text(t, "postTransactionAmounts", "sharesOwnedFollowingTransaction")),
                                     planned))
    return f


def parse_feed(xml: bytes) -> list[dict]:
    """The getcurrent Atom feed: [{accession, cik, name, role (Issuer/Reporting), folder, updated}]."""
    root = ET.fromstring(xml)
    out = []
    for e in (c for c in root if _local(c.tag) == "entry"):
        title = _text(e, "title")
        m = re.match(r"(\S+) - (.+) \((\d{10})\) \((Issuer|Reporting)\)", title)
        link = _find(e, "link")
        idm = re.search(r"accession-number=([\d-]+)", _text(e, "id"))
        if not m or link is None or not idm:
            continue
        href = link.get("href") or ""
        out.append({"form": m.group(1), "name": m.group(2), "cik": int(m.group(3)), "role": m.group(4),
                    "accession": idm.group(1), "folder": href.rsplit("/", 1)[0], "updated": _text(e, "updated")})
    return out


@dataclass
class Holding:
    cusip: str
    name: str
    title: str
    value: float  # dollars
    shares: float
    put_call: str  # "", "Put" or "Call"
    figi: str = ""


def parse_13f(xml: bytes, in_thousands: bool = False) -> list[Holding]:
    """An information table, rows with the same CUSIP and put/call summed (some funds split them by manager)."""
    root = ET.fromstring(xml)
    agg: dict[tuple, Holding] = {}
    for row in (c for c in root.iter() if _local(c.tag) == "infoTable"):
        cusip = _text(row, "cusip").upper()
        pc = _text(row, "putCall").title()
        value = (_num(_text(row, "value")) or 0.0) * (1000 if in_thousands else 1)
        shares = _num(_text(row, "shrsOrPrnAmt", "sshPrnamt")) or 0.0
        if not cusip:
            continue
        h = agg.get((cusip, pc))
        if h is None:
            agg[(cusip, pc)] = Holding(cusip, _text(row, "nameOfIssuer"), _text(row, "titleOfClass"), value, shares,
                                       pc, _text(row, "figi"))
        else:
            h.value += value
            h.shares += shares
    return sorted(agg.values(), key=lambda h: -h.value)


class SEC:
    def __init__(self, http: Http, data_dir=None):
        self.api = Api("SEC EDGAR", http, WWW, needs_key=False, limits=((4, 1.0), (240, 60.0)),
                       headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"})
        self.figi = Api("OpenFIGI", http, OPENFIGI, needs_key=False, limits=((20, 60.0),))
        self.http = http
        self.data_dir = data_dir
        self._tickers: dict[int, str] | None = None
        self._cusips: dict[str, str] = read_json(data_dir / "cusip_tickers.json", {}) if data_dir else {}

    async def _get(self, url: str, raw: bool = False, wait: float = 30.0):
        if raw:
            resp = await self.http.get(url, headers=self.api.headers, source="SEC EDGAR", timeout=30)
            if resp.status != 200:
                raise ApiError("SEC EDGAR", f"HTTP {resp.status}", "missing" if resp.status == 404 else "bad",
                               resp.status)
            return resp.content or resp.text.encode()
        return await self.api.get(url, wait=wait)

    async def feed(self, start: int = 0) -> list[dict]:
        await self.api.limiters[0].acquire(30)
        return parse_feed(await self._get(f"{FORM4_FEED}&start={start}", raw=True))

    async def xml_in(self, folder: str, prefer: str = "") -> bytes | None:
        """The filing's XML document (not the HTML rendering), from its folder's index.json."""
        idx = await self._get(f"{folder}/index.json")
        items = ((idx or {}).get("directory") or {}).get("item") or []
        names = [i.get("name", "") for i in items if str(i.get("name", "")).lower().endswith(".xml")]
        if prefer:
            names.sort(key=lambda n: prefer not in n.lower())
        if not names:
            return None
        await self.api.limiters[0].acquire(30)
        return await self._get(f"{folder}/{names[0]}", raw=True)

    async def form4(self, entry: dict) -> Form4 | None:
        xml = await self.xml_in(entry["folder"])
        if xml is None:
            return None
        f = parse_form4(xml, entry["accession"], f"{entry['folder']}/{entry['accession']}-index.htm")
        if f.issuer_cik:
            f.symbol = (await self.ticker_of(f.issuer_cik)) or f.symbol
        return f

    async def ticker_of(self, cik: int) -> str | None:
        if self._tickers is None:
            try:
                data = await self._get(TICKERS)
                self._tickers = {}
                for row in (data or {}).values():
                    if isinstance(row, dict) and row.get("cik_str") and row.get("ticker"):
                        self._tickers.setdefault(int(row["cik_str"]), str(row["ticker"]).upper())
            except Exception:
                log.warning("SEC ticker list unavailable", exc_info=True)
                return None
        return self._tickers.get(int(cik))

    async def filings(self, cik: int) -> dict:
        return await self._get(f"{DATA}/submissions/CIK{int(cik):010d}.json")

    async def latest_13f(self, cik: int, count: int = 2) -> list[dict]:
        """The fund's last `count` 13F-HR filings (amendments that restate skipped): [{accession, filed, period}]."""
        rec = ((await self.filings(cik)).get("filings") or {}).get("recent") or {}
        forms, accs = rec.get("form") or [], rec.get("accessionNumber") or []
        filed, period = rec.get("filingDate") or [], rec.get("reportDate") or []
        out, seen = [], set()
        for i, form in enumerate(forms):
            if form != "13F-HR" or i >= len(accs):
                continue
            p = period[i] if i < len(period) else ""
            if p in seen:
                continue
            seen.add(p)
            out.append({"accession": accs[i], "filed": filed[i] if i < len(filed) else "", "period": p})
            if len(out) >= count:
                break
        return out

    async def holdings(self, cik: int, filing: dict) -> list[Holding]:
        folder = f"{WWW}/Archives/edgar/data/{int(cik)}/{filing['accession'].replace('-', '')}"
        idx = await self._get(f"{folder}/index.json")
        items = ((idx or {}).get("directory") or {}).get("item") or []
        names = [i.get("name", "") for i in items if str(i.get("name", "")).lower().endswith(".xml")
                 and "primary_doc" not in str(i.get("name", "")).lower()]
        if not names:
            raise ApiError("SEC EDGAR", "no information table in the 13F", "missing")
        xml = await self._get(f"{folder}/{names[0]}", raw=True)
        return parse_13f(xml, in_thousands=filing.get("filed", "9999") < "2023-01-03")

    async def tickers_for(self, holdings: list[Holding]) -> dict[str, str]:
        """CUSIP -> ticker for these holdings, asking OpenFIGI for the unknown ones (10 a request)."""
        unknown = [h for h in holdings if h.cusip not in self._cusips]
        for i in range(0, len(unknown), 10):
            chunk = unknown[i:i + 10]
            jobs = [{"idType": "ID_CINS" if h.cusip[:1].isalpha() else "ID_CUSIP", "idValue": h.cusip,
                     "exchCode": "US"} for h in chunk]
            try:
                await self.figi.limiters[0].acquire(90)
                resp = await self.http.post_json(OPENFIGI, jobs, source="OpenFIGI", timeout=30)
            except Exception as exc:
                log.info("OpenFIGI unavailable: %s", exc)
                break
            if resp is None or not isinstance(resp, list):
                break
            for h, ans in zip(chunk, resp):
                data = (ans or {}).get("data") if isinstance(ans, dict) else None
                ticker = str(data[0].get("ticker") or "") if data else ""
                self._cusips[h.cusip] = ticker.replace("/", "-").upper() if ticker else ""
        if self.data_dir and unknown:
            write_json(self.data_dir / "cusip_tickers.json", self._cusips)
        return {h.cusip: self._cusips.get(h.cusip, "") for h in holdings}


__all__ = ["SEC", "FUNDS", "Form4", "Holding", "Owner", "InsiderTrade", "parse_form4", "parse_feed", "parse_13f",
           "HttpError"]
