"""US Congress members' stock trades, read from the official disclosures (free, no key).

- House: the Clerk's yearly filing index (a ZIP of XML, rebuilt daily around 13:00 UTC) lists every Periodic
  Transaction Report (PTR). E-filed PTRs are PDFs with a text layer, read here with pdfplumber; scanned paper
  filings (7-digit IDs) have no text and are listed as a link only.
- Senate: the eFD site (efdsearch.senate.gov) after accepting its terms; electronic PTRs are HTML tables.
- Party and state come from the public congress-legislators list.

Members must report a trade within 45 days, so this is history, not a live feed. The reports are public records;
the law forbids using them for commercial purposes, credit ratings or soliciting money, so the bot shows them for
information only. House/Senate Stock Watcher's feeds died in 2020-2021 and the paid APIs aren't needed.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser

from ..http import Http, HttpError

log = logging.getLogger(__name__)

HOUSE_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SENATE = "https://efdsearch.senate.gov"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

HOUSE_TYPES = {"ST": "stock", "OP": "option", "EF": "etf", "MF": "fund", "GS": "treasury", "CS": "bond",
               "CT": "crypto", "PS": "private", "HN": "fund", "OT": "other", "AB": "bond"}
HOUSE_KINDS = {"P": "purchase", "S": "sale", "S (PARTIAL)": "sale_partial", "E": "exchange"}
OWNERS = {"": "Self", "SP": "Spouse", "JT": "Joint", "DC": "Child", "SELF": "Self", "SPOUSE": "Spouse",
          "JOINT": "Joint", "CHILD": "Child", "DEPENDENT CHILD": "Child"}
SENATE_KINDS = {"purchase": "purchase", "sale (full)": "sale", "sale": "sale", "sale (partial)": "sale_partial",
                "exchange": "exchange"}
KIND_WORDS = {"purchase": "Bought", "sale": "Sold", "sale_partial": "Sold some", "exchange": "Exchanged"}


@dataclass
class Filing:
    id: str  # "H:20035580" or "S:<uuid>"
    chamber: str  # house or senate
    member: str  # "Lloyd Doggett"
    last: str
    state: str  # "TX"
    district: str  # "TX37" for the House, "" for the Senate
    filed: str  # ISO date
    url: str
    paper: bool = False  # a scanned filing: no machine-readable trades
    amendment: bool = False
    party: str = ""  # D, R or I


@dataclass
class Trade:
    filing: str
    chamber: str
    member: str
    state: str
    party: str
    owner: str  # Self, Spouse, Joint or Child
    asset: str
    asset_type: str  # stock, option, etf, fund, bond, treasury, crypto, private, other
    ticker: str | None  # Yahoo's symbol
    kind: str  # purchase, sale, sale_partial or exchange
    tx_date: str  # ISO
    filed: str  # ISO
    amount_low: int
    amount_high: int | None
    amount: str
    note: str = ""

    @property
    def key(self) -> tuple:
        """The same trade restated in an amendment has the same key."""
        return (self.chamber, self.member.lower(), self.tx_date, self.ticker or self.asset.lower()[:40], self.kind,
                self.amount, self.owner)

    @property
    def amount_mid(self) -> float:
        return (self.amount_low + self.amount_high) / 2 if self.amount_high else float(self.amount_low)


# ----- small parsers -----

def parse_amount(text: str) -> tuple[int, int | None]:
    """"$1,001 - $15,000" -> (1001, 15000); "Over $50,000,000" -> (50000000, None); unknown -> (0, None)."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$\s*([\d,]+)", text or "") if n.replace(",", "")]
    if len(nums) >= 2:
        return min(nums[:2]), max(nums[:2])
    if len(nums) == 1:
        return nums[0], None
    return 0, None


def iso_date(text: str) -> str:
    """M/D/YYYY or MM/DD/YYYY (optionally with " @ time") -> YYYY-MM-DD, or ""."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if not m:
        return ""
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return ""


def yahoo_ticker(text: str | None) -> str | None:
    """A disclosure's ticker as Yahoo writes it (BRK.B -> BRK-B), or None if it doesn't look like one."""
    t = (text or "").strip().upper().replace(".", "-").replace("/", "-")
    return t if re.fullmatch(r"[A-Z][A-Z0-9-]{0,7}", t) and t not in ("N-A", "NA", "NONE") else None


def ticker_from_name(name: str) -> str | None:
    """"Electronic Arts Inc. (EA)" or "EA - Electronic Arts Inc" -> EA."""
    m = re.findall(r"\(([A-Z][A-Z0-9.\-]{0,6})\)", name or "")
    if m:
        return yahoo_ticker(m[-1])
    m = re.match(r"\s*([A-Z][A-Z0-9.]{0,5})\s+-\s+\S", name or "")
    return yahoo_ticker(m.group(1)) if m else None


# ----- House -----

def parse_house_index(data: bytes) -> list[dict]:
    """The PTR rows of a yearly index ZIP: [{doc, year, first, last, district, state, filed, paper}]."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
        if name is None:
            raise ValueError("no XML in the House index")
        root = ET.fromstring(z.read(name))
    rows = []
    for m in root.iter("Member"):
        def get(tag):
            return (m.findtext(tag) or "").strip()
        if get("FilingType") != "P":
            continue
        doc, dst = get("DocID"), get("StateDst").upper()
        filed = iso_date(get("FilingDate"))
        if not doc.isdigit() or not filed:
            continue
        rows.append({"doc": doc, "year": get("Year") or filed[:4], "first": get("First"), "last": get("Last"),
                     "district": dst, "state": dst[:2], "filed": filed,
                     "paper": not (len(doc) == 8 and doc.startswith("2"))})
    return rows


DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)")
ATYPE_RE = re.compile(r"\[([A-Za-z]{2})[\]\}]")


def parse_house_ptr(data: bytes) -> list[dict]:
    """The transactions in an e-filed House PTR PDF: [{owner, asset, ticker, asset_type, type, tx_date, amount,
    filing_status, description}]. Columns are found from the bold header words; 9-pt lines are transactions and
    8.5-pt lines their notes (Filing Status, Subholding Of, Description). Empty for a scanned filing."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [page.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False, use_text_flow=False)
                 for page in pdf.pages]
    cols, rows, in_table = None, [], False
    for ws in pages:
        hdr = {w["text"]: w["x0"] for w in ws if "Bold" in w["fontname"] and w["text"] in (
            "ID", "Owner", "Asset", "Transaction", "Notification", "Amount", "Cap.")}
        if "Asset" in hdr and "Amount" in hdr and "Owner" in hdr and "Transaction" in hdr:
            cols, in_table = hdr, True
            top_hdr = max((w["bottom"] for w in ws if w["text"] == "$200?"), default=max(
                (w["bottom"] for w in ws if "Bold" in w["fontname"] and w["text"] == "Asset"), default=0))
        elif in_table and cols:
            top_hdr = 0  # the table goes on from the previous page
        else:
            continue
        foot = min((w["top"] for w in ws if w["text"].startswith("*") and w["size"] < 8.6), default=1e9)
        sections = [w["top"] for w in ws if "Bold" in w["fontname"] and w["size"] > 11 and w["top"] > top_hdr]
        end = min([foot] + sections)
        if end < 1e9:
            in_table = False
        body = [w for w in ws if top_hdr < w["top"] < end and 8.3 <= w["size"] <= 9.3 and "Bold" not in w["fontname"]]
        body.sort(key=lambda w: (round(w["top"]), w["x0"]))
        lines: list[list] = []
        for w in body:
            if lines and abs(lines[-1][0] - w["top"]) < 3:
                lines[-1][1].append(w)
            else:
                lines.append([w["top"], [w]])
        for _, lw in lines:
            if lw[0]["size"] < 8.8:  # a note under the last transaction
                if rows:
                    t = re.sub(r"\s+", " ", " ".join(w["text"] for w in lw).replace("\x00", "")).strip()
                    m = re.match(r"^(F S|S O|D|C|L)\s*:\s*(.*)$", t)
                    key = {"F S": "filing_status", "S O": "subholding_of", "D": "description", "C": "comments",
                           "L": "location"}.get(m.group(1)) if m else None
                    if key:
                        rows[-1].setdefault("meta", {})[key] = m.group(2)
                        rows[-1]["_last"] = key
                    elif rows[-1].get("_last"):
                        rows[-1]["meta"][rows[-1]["_last"]] += " " + t
                continue
            cell = {"owner": [], "asset": [], "type": [], "dates": [], "amount": []}
            for w in lw:
                x = w["x0"]
                if x < cols["Owner"] - 2:
                    continue  # the filer's own account IDs
                elif x < cols["Asset"] - 2:
                    cell["owner"].append(w["text"])
                elif x < cols["Transaction"] - 2:
                    cell["asset"].append(w["text"])
                elif x < cols["Transaction"] + 30 and not DATE_RE.match(w["text"]):
                    cell["type"].append(w["text"])
                elif x < cols["Amount"] - 2:
                    cell["dates"].append(w["text"])
                elif x < cols.get("Cap.", 1e9) - 2:
                    cell["amount"].append(w["text"])
            if cell["type"] or cell["dates"]:
                rows.append(cell)
            elif rows:
                for k in ("owner", "asset", "amount"):
                    rows[-1][k] += cell[k]
    out = []
    for r in rows:
        asset = " ".join(r["asset"])
        at = ATYPE_RE.search(asset)
        tickers = TICKER_RE.findall(asset)
        dates = [d for d in r["dates"] if DATE_RE.match(d)]
        meta = r.get("meta", {})
        out.append({"owner": " ".join(r["owner"]).strip(), "asset": ATYPE_RE.sub("", asset).strip(),
                    "ticker": tickers[-1] if tickers else None, "asset_type": at.group(1).upper() if at else "",
                    "type": " ".join(r["type"]).strip(), "tx_date": dates[0] if dates else "",
                    "amount": " ".join(r["amount"]).strip(), "filing_status": meta.get("filing_status", ""),
                    "description": meta.get("description", "")})
    return out


def house_trades(f: Filing, raw: list[dict]) -> list[Trade]:
    trades = []
    for r in raw:
        if (r.get("filing_status") or "").lower().startswith("deleted"):
            continue
        kind = HOUSE_KINDS.get((r.get("type") or "").upper().strip())
        tx = iso_date(r.get("tx_date") or "")
        if kind is None or not tx:
            continue
        low, high = parse_amount(r.get("amount") or "")
        atype = HOUSE_TYPES.get((r.get("asset_type") or "").upper(), "other")
        desc = (r.get("description") or "").strip()
        if atype == "stock" and re.search(r"\b(call|put)s?\b.*\boption", desc, re.I):
            atype = "option"
        trades.append(Trade(f.id, f.chamber, f.member, f.state, f.party,
                            OWNERS.get((r.get("owner") or "").upper().strip(), "Self"), r.get("asset") or "", atype,
                            yahoo_ticker(r.get("ticker")), kind, tx, f.filed, low, high, r.get("amount") or "",
                            desc[:160] if atype == "option" else ""))
    return trades


class House:
    def __init__(self, http: Http):
        self.http = http
        self.last_modified: dict[str, str] = {}  # year -> the index's Last-Modified

    async def index(self, year: int, force: bool = False) -> list[dict] | None:
        """The year's PTRs, or None when the index hasn't changed since the last call. A cache-buster beats
        stale copies of the index."""
        resp = await self.http.get(HOUSE_INDEX.format(year=year), params={"cb": str(int(time.time()))},
                                   headers={"User-Agent": UA}, source="House Clerk", timeout=60)
        if resp.status == 404:
            return []
        if resp.status != 200 or not resp.content:
            raise HttpError("House Clerk", f"index HTTP {resp.status}", resp.status)
        stamp = (resp.headers or {}).get("last-modified") or (resp.headers or {}).get("etag") or ""
        if stamp and not force and self.last_modified.get(str(year)) == stamp:
            return None
        rows = parse_house_index(resp.content)
        if stamp:
            self.last_modified[str(year)] = stamp
        return rows

    async def ptr(self, year: str, doc: str) -> bytes | None:
        resp = await self.http.get(HOUSE_PTR.format(year=year, doc=doc), headers={"User-Agent": UA},
                                   source="House Clerk", timeout=60)
        if resp.status == 404:
            return None
        if resp.status != 200 or not resp.content.startswith(b"%PDF"):
            raise HttpError("House Clerk", f"PTR {doc}: HTTP {resp.status}", resp.status)
        return resp.content


# ----- Senate -----

class _Table(HTMLParser):
    """The rows of the first table, as lists of cell text."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self.depth = 0
        self.cell: list[str] | None = None
        self.row: list[str] | None = None
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "table":
            self.depth += 1
        elif self.depth and tag == "tr":
            self.row = []
        elif self.depth and tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if self.done:
            return
        if tag == "table" and self.depth:
            self.depth -= 1
            self.done = self.depth == 0
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def parse_senate_report(html: str) -> list[dict]:
    """The transactions table of an electronic Senate PTR: [{Transaction Date, Owner, Ticker, Asset Name,
    Asset Type, Type, Amount, Comment}]."""
    p = _Table()
    p.feed(html or "")
    if not p.rows:
        return []
    header, *rows = p.rows
    return [dict(zip(header, r)) for r in rows if len(r) == len(header)]


def senate_trades(f: Filing, rows: list[dict]) -> list[Trade]:
    trades = []
    for r in rows:
        kind = SENATE_KINDS.get((r.get("Type") or "").strip().lower())
        tx = iso_date(r.get("Transaction Date") or "")
        if kind is None or not tx:
            continue
        name = r.get("Asset Name") or ""
        atype_text = (r.get("Asset Type") or "").lower()
        atype = ("option" if "option" in atype_text else "stock" if atype_text == "stock" else
                 "crypto" if "crypto" in atype_text else "bond" if "bond" in atype_text or "municipal" in atype_text
                 else "fund" if "fund" in atype_text else "private" if "non-public" in atype_text else "other")
        cell = (r.get("Ticker") or "").replace("--", " ").split()
        ticker = yahoo_ticker(cell[-1]) if cell else None
        ticker = ticker or (ticker_from_name(name) if atype in ("stock", "option") else None)
        low, high = parse_amount(r.get("Amount") or "")
        trades.append(Trade(f.id, f.chamber, f.member, f.state, f.party,
                            OWNERS.get((r.get("Owner") or "").upper().strip(), "Self"),
                            re.sub(r"\s+", " ", name).strip()[:160], atype, ticker, kind, tx, f.filed, low, high,
                            r.get("Amount") or "", name[:160] if atype == "option" else ""))
    return trades


class Senate:
    """The eFD site: accept its terms (a CSRF form), then search and read reports. It needs cookies and form
    posts, so it has its own aiohttp session; its health is recorded on the shared Http for /status."""

    def __init__(self, http: Http | None = None, session=None):
        self.http = http
        self._session = session
        self._csrf: str | None = None

    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(), headers={"User-Agent": UA},
                                                  timeout=aiohttp.ClientTimeout(total=45), trust_env=True)
            self._csrf = None
        return self._session

    def _record(self, ok: bool, error: str = "") -> None:
        if self.http is None:
            return
        if ok:
            self.http.record_ok("Senate eFD")
        else:
            self.http.record_failure("Senate eFD", HttpError("Senate eFD", error[:100]))

    async def handshake(self) -> None:
        s = await self._sess()
        async with s.get(f"{SENATE}/search/home/") as r:
            page = await r.text()
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        if not m:
            raise HttpError("Senate eFD", "no terms form on the home page")
        async with s.post(f"{SENATE}/search/home/", data={"prohibition_agreement": "1",
                                                          "csrfmiddlewaretoken": m.group(1)},
                          headers={"Referer": f"{SENATE}/search/home/"}) as r:
            await r.read()
        self._csrf = next((c.value for c in s.cookie_jar if c.key == "csrftoken"), None)
        if not self._csrf:
            raise HttpError("Senate eFD", "no CSRF cookie after accepting the terms")

    async def search(self, since: date, start: int = 0, length: int = 100) -> tuple[int, list[list]]:
        """(total, rows) of PTRs filed since `since`, newest first. A row is [first, last, office, link html,
        filed MM/DD/YYYY]."""
        try:
            if not self._csrf:
                await self.handshake()
            for attempt in range(2):
                s = await self._sess()
                form = {"draw": "1", "start": str(start), "length": str(length), "order[0][column]": "4",
                        "order[0][dir]": "desc", "report_types": "[11]", "filer_types": "[1,5]",
                        "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"), "submitted_end_date": "",
                        "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "",
                        "last_name": "", "csrfmiddlewaretoken": self._csrf or ""}
                async with s.post(f"{SENATE}/search/report/data/", data=form, allow_redirects=False,
                                  headers={"Referer": f"{SENATE}/search/", "X-CSRFToken": self._csrf or "",
                                           "X-Requested-With": "XMLHttpRequest"}) as r:
                    status, text = r.status, await r.text()
                if status in (302, 403) and attempt == 0:
                    await self.handshake()  # the session ran out
                    continue
                if status != 200:
                    raise HttpError("Senate eFD", f"search HTTP {status}", status)
                data = json.loads(text)
                rows = [row for row in data.get("data") or [] if isinstance(row, list) and len(row) >= 5]
                self._record(True)
                return int(data.get("recordsTotal") or 0), rows
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record(False, f"{type(exc).__name__}: {exc}")
            raise
        return 0, []

    async def report(self, path: str) -> str | None:
        """An electronic report's HTML (None if the session needs renewing and it still fails)."""
        for attempt in range(2):
            s = await self._sess()
            async with s.get(f"{SENATE}{path}", allow_redirects=False,
                             headers={"Referer": f"{SENATE}/search/"}) as r:
                status, text = r.status, await r.text()
            if status in (302, 403) and attempt == 0:
                await self.handshake()
                continue
            if status != 200:
                self._record(False, f"report HTTP {status}")
                return None
            self._record(True)
            return text
        return None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def senate_filing(row: list) -> Filing | None:
    """A search row as a Filing (party filled in later)."""
    first, last, office, link, filed = (str(x) for x in row[:5])
    last = re.sub(r",.*$|\s+(jr\.?|sr\.?|ii|iii|iv)$", "", last.strip(), flags=re.I).strip()
    first = first.strip()
    m = re.search(r'href="([^"]+)"', link)
    if not m:
        return None
    path = m.group(1)
    uuid = path.strip("/").split("/")[-1]
    state = ""
    sm = re.search(r"\(([A-Z]{2})\)", office)
    if sm:
        state = sm.group(1)
    return Filing(f"S:{uuid}", "senate", f"{first} {last}".strip(), last.strip(), state, "", iso_date(filed),
                  f"{SENATE}{path}", paper="/paper/" in path, amendment="Amendment" in link)


# ----- who's who -----

@dataclass
class Roster:
    """Current members: party and state by House seat (TX37) and by Senate last name."""
    house: dict[str, dict] = field(default_factory=dict)
    senate: dict[str, list[dict]] = field(default_factory=dict)

    def find(self, chamber: str, last: str, state: str = "", district: str = "", first: str = "") -> dict | None:
        if chamber == "house":
            return self.house.get(district.upper())
        cands = self.senate.get(last.lower().strip(), [])
        if state:
            cands = [c for c in cands if c["state"] == state] or cands
        if len(cands) > 1 and first:
            cands = [c for c in cands if c["first"].lower().startswith(first.lower()[:3])] or cands
        return cands[0] if len(cands) == 1 else None


def parse_roster(data) -> Roster:
    r = Roster()
    for m in data if isinstance(data, list) else []:
        try:
            term = m["terms"][-1]
            party = {"Democrat": "D", "Republican": "R", "Independent": "I"}.get(term.get("party", ""), "")
            info = {"name": m["name"].get("official_full") or f"{m['name']['first']} {m['name']['last']}",
                    "last": m["name"]["last"], "first": m["name"].get("first", ""), "party": party,
                    "state": term["state"],
                    "bioguide": m["id"].get("bioguide", "")}
            if term["type"] == "rep":
                r.house[f"{term['state']}{int(term.get('district') or 0):02d}"] = info
            else:
                r.senate.setdefault(m["name"]["last"].lower(), []).append(info)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return r


async def fetch_roster(http: Http) -> Roster:
    resp = await http.get(LEGISLATORS, source="congress-legislators", timeout=60)
    if resp.status != 200:
        raise HttpError("congress-legislators", f"HTTP {resp.status}", resp.status)
    return parse_roster(resp.json())


def today_iso() -> str:
    return datetime.now().date().isoformat()
