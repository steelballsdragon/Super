"""Calendars: US economic releases (with consensus forecasts), earnings dates and past earnings surprises from
Nasdaq's public API (no key; the bot already uses it for prices), past release dates from FRED (FRED_API_KEY) and
FOMC meeting dates from the Federal Reserve's calendar page."""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..backup import NASDAQ, NASDAQ_HEADERS, from_nasdaq_symbol, number
from ..http import Http, HttpError
from . import Api, env_key

NEW_YORK = ZoneInfo("America/New_York")
FED_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED = "https://api.stlouisfed.org/fred"

# (key, pattern on Nasdaq's event name, short name, FRED release id for past dates, importance 1-3,
#  which way is "hot": +1 when a higher number means hotter inflation or a stronger economy, -1 when it means
#  weaker (unemployment, claims), 0 when it isn't a number to compare)
EVENTS = (
    ("minutes", r"^fomc (meeting )?minutes$", "Fed minutes", None, 2, 0),
    ("fomc", r"^fed interest rate decision$", "Fed decision", None, 3, 1),
    ("core_cpi", r"^core cpi( \((mom|yoy)\))?$", "Core CPI", 10, 3, 1),
    ("cpi", r"^cpi( \((mom|yoy)\))?$", "CPI", 10, 3, 1),
    ("payrolls", r"^non-?farm payrolls$", "Jobs report", 50, 3, 1),
    ("unemployment", r"^unemployment rate$", "Unemployment rate", 50, 2, -1),
    ("core_pce", r"^core pce price index( \((mom|yoy)\))?$", "Core PCE inflation", 54, 2, 1),
    ("pce", r"^pce price index( \((mom|yoy)\))?$", "PCE inflation", 54, 2, 1),
    ("gdp", r"^gdp( \((qoq|yoy)\))?$", "GDP", 53, 2, 1),
    ("retail", r"^(core )?retail sales( \((mom|yoy)\))?$", "Retail sales", 9, 2, 1),
    ("core_ppi", r"^core ppi( \((mom|yoy)\))?$", "Core PPI", 46, 1, 1),
    ("ppi", r"^ppi( \((mom|yoy)\))?$", "PPI", 46, 2, 1),
    ("claims", r"^initial jobless claims$", "Jobless claims", 180, 1, -1),
    ("ism", r"^ism (manufacturing|non-manufacturing|services) pmi$", "ISM", None, 2, 1),
    ("jolts", r"^jolts job openings$", "JOLTS job openings", 192, 1, 1),
    ("sentiment", r"^(michigan consumer sentiment|cb consumer confidence)$", "Consumer sentiment", None, 1, 1),
)


@dataclass
class EconEvent:
    key: str
    name: str  # Nasdaq's name, e.g. "Core CPI (MoM) (Sep)"
    short: str  # "Core CPI"
    at: float  # epoch seconds
    importance: int
    actual: str
    consensus: str
    previous: str
    rid: int | None
    hot: int

    @property
    def released(self) -> bool:
        return bool(self.actual)


@dataclass
class Earning:
    symbol: str
    name: str
    day: str  # ISO date
    timing: str  # "before open", "after close" or ""
    market_cap: float | None
    eps_forecast: float | None
    estimates: int | None


MONTH_TAG = re.compile(r"\s*\((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\)|\s*\(q[1-4]\)", re.I)


def classify(name: str) -> tuple | None:
    low = MONTH_TAG.sub("", name).strip().lower()
    for key, pattern, short, rid, imp, hot in EVENTS:
        if re.search(pattern, low):
            return key, short, rid, imp, hot
    return None


def value(text: str) -> float | None:
    """"0.4%", "254K", "-1.2B", "4.10%" -> a number (K/M/B/T scaled), or None."""
    t = html.unescape(text or "").strip().replace(",", "")
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([%KMBT])?", t)
    if not m:
        return None
    scale = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(m.group(2) or "", 1.0)
    return float(m.group(1)) * scale


def surprise(e: EconEvent) -> int:
    """+1 if the actual came in above the consensus (or the previous, without one), -1 below, 0 the same or
    unknown."""
    a = value(e.actual)
    b = value(e.consensus)
    if b is None:
        b = value(e.previous)
    if a is None or b is None or abs(a - b) < 1e-9:
        return 0
    return 1 if a > b else -1


def _clean(text) -> str:
    t = html.unescape(str(text or "")).replace("\xa0", " ").strip()
    return "" if t in ("-", "--", "N/A") else t


def parse_econ(data: dict, day: date) -> list[EconEvent]:
    """Nasdaq's economic events for a day, US ones we track only."""
    rows = ((data or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or (r.get("country") or "").strip() != "United States":
            continue
        name = _clean(r.get("eventName"))
        hit = classify(name)
        if not hit:
            continue
        key, short, rid, imp, hot = hit
        # The column says "gmt" but the times are US Eastern (claims 08:30, the Fed's decision 14:00).
        m = re.match(r"(\d{1,2}):(\d{2})", r.get("gmt") or "")
        hh, mm = (int(m.group(1)), int(m.group(2))) if m else (8, 30)
        at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK).timestamp()
        out.append(EconEvent(key, name, short, at, imp, _clean(r.get("actual")), _clean(r.get("consensus")),
                             _clean(r.get("previous")), rid, hot))
    return out


def parse_earnings(data: dict, day: date) -> list[Earning]:
    rows = ((data or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        timing = {"time-pre-market": "before open", "time-after-hours": "after close"}.get(r.get("time") or "", "")
        est = number(r.get("noOfEsts"))
        out.append(Earning(from_nasdaq_symbol(str(r["symbol"]).strip()), _clean(r.get("name")), day.isoformat(),
                           timing, number(r.get("marketCap")), number(r.get("epsForecast")),
                           int(est) if est is not None else None))
    return out


def parse_surprises(data: dict) -> list[dict]:
    """Past reports, newest first: [{reported (ISO), quarter, eps, consensus, surprise_pct}]."""
    rows = (((data or {}).get("data") or {}).get("earningsSurpriseTable") or {}).get("rows") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(r.get("dateReported") or ""))
        if not m:
            continue
        try:
            reported = date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            continue
        out.append({"reported": reported, "quarter": _clean(r.get("fiscalQtrEnd")), "eps": number(r.get("eps")),
                    "consensus": number(r.get("consensusForecast")), "surprise_pct": number(r.get("percentageSurprise"))})
    return sorted(out, key=lambda r: r["reported"], reverse=True)


MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"), 1)}


def parse_fomc(page: str) -> list[str]:
    """FOMC decision days (ISO, the meeting's last day) from the Fed's calendar page, oldest first."""
    out = []
    for year_m in re.finditer(r"(\d{4}) FOMC Meetings", page):
        year = int(year_m.group(1))
        end = page.find("FOMC Meetings", year_m.end())
        block = page[year_m.end(): end if end > 0 else len(page)]
        for m in re.finditer(r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)\s*(?:</strong>)?.*?'
                             r'fomc-meeting__date[^>]*>\s*([^<]+)<', block, re.S):
            months, days = m.group(1).lower().split("/"), m.group(2).strip()
            if "notation" in days.lower() or "unscheduled" in days.lower():
                continue
            nums = [int(n) for n in re.findall(r"\d+", days)]
            if not nums:
                continue
            wraps = len(nums) > 1 and nums[-1] < nums[0]  # "Apr/May 30-1": the decision is in May
            month = MONTHS.get(months[-1] if wraps else months[0]) or MONTHS.get(months[0][:3] + "x") or next(
                (v for k, v in MONTHS.items() if k.startswith((months[-1] if wraps else months[0])[:3])), None)
            if month is None:
                continue
            try:
                out.append(date(year, month, nums[-1]).isoformat())
            except ValueError:
                continue
    return sorted(set(out))


class Calendars:
    """Nasdaq's calendars (cached), FOMC dates and FRED's past release dates."""

    def __init__(self, http: Http, fred_key: str | None = None, state_file=None):
        self.http = http
        self.fred = Api("FRED", http, FRED, env_key("FRED_API_KEY", "FRED_KEY") if fred_key is None else fred_key,
                        key_param="api_key", limits=((100, 60.0),), headers={}, state_file=state_file)
        self._cache: dict[tuple, tuple[float, object]] = {}

    async def _nasdaq(self, path: str, ttl: float):
        hit = self._cache.get(("n", path))
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        resp = await self.http.get(f"{NASDAQ}{path}", headers=NASDAQ_HEADERS, source="Nasdaq")
        if resp.status != 200:
            raise HttpError("Nasdaq", f"Nasdaq: HTTP {resp.status}", resp.status)
        data = resp.json()
        self._cache[("n", path)] = (time.monotonic(), data)
        if len(self._cache) > 500:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                del self._cache[k]
        return data

    async def econ(self, day: date, ttl: float = 900.0) -> list[EconEvent]:
        return parse_econ(await self._nasdaq(f"/calendar/economicevents?date={day.isoformat()}", ttl), day)

    async def earnings(self, day: date, ttl: float = 3 * 3600.0) -> list[Earning]:
        return parse_earnings(await self._nasdaq(f"/calendar/earnings?date={day.isoformat()}", ttl), day)

    async def surprises(self, symbol: str) -> list[dict]:
        from ..backup import nasdaq_symbol
        sym = nasdaq_symbol(symbol) or symbol
        return parse_surprises(await self._nasdaq(f"/company/{sym}/earnings-surprise", 12 * 3600.0))

    async def fomc_days(self) -> list[str]:
        hit = self._cache.get(("fomc",))
        if hit and time.monotonic() - hit[0] < 7 * 86400:
            return hit[1]
        resp = await self.http.get(FED_CALENDAR, source="Federal Reserve")
        if resp.status != 200:
            raise HttpError("Federal Reserve", f"HTTP {resp.status}", resp.status)
        days = parse_fomc(resp.text)
        self._cache[("fomc",)] = (time.monotonic(), days)
        return days

    async def release_dates_ahead(self, rid: int, start: date, end: date) -> list[str]:
        """FRED's scheduled publication dates of a release from `start` up to `end` (ISO, oldest first)."""
        key = ("fred+", rid, start.isoformat(), end.isoformat())
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 12 * 3600:
            return hit[1]
        data = await self.fred.get("release/dates", {"release_id": rid, "file_type": "json", "sort_order": "asc",
                                                      "realtime_start": start.isoformat(),
                                                      "include_release_dates_with_no_data": "true", "limit": 100},
                                   wait=10)
        days = sorted({d["date"] for d in (data or {}).get("release_dates") or []
                       if isinstance(d, dict) and start.isoformat() <= str(d.get("date", "")) < end.isoformat()})
        self._cache[key] = (time.monotonic(), days)
        return days

    async def release_dates(self, rid: int, before: date, count: int = 16) -> list[str]:
        """FRED's last `count` publication dates of a release before `before` (ISO, newest first)."""
        key = ("fred", rid, before.isoformat())
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 7 * 86400:
            return hit[1]
        data = await self.fred.get("release/dates", {"release_id": rid, "file_type": "json", "sort_order": "desc",
                                                      "realtime_start": (before - timedelta(days=900)).isoformat(),
                                                      "include_release_dates_with_no_data": "false",
                                                      "limit": 120}, wait=10)
        days = sorted({d["date"] for d in (data or {}).get("release_dates") or []
                       if isinstance(d, dict) and str(d.get("date", "")) < before.isoformat()}, reverse=True)[:count]
        self._cache[key] = (time.monotonic(), days)
        return days
