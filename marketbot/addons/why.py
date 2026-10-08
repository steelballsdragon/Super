"""/why: why a stock, ETF, index or coin is moving.

The move is split into what the market explains (the stock's beta to the S&P 500 times the S&P's move), what its
sector explains beyond the market (its sensitivity to the sector ETF's own move), and the rest, which is the
company's own news. The betas come from the last year of daily returns. Then the bot gathers what could explain
the company part: headlines in the bot's news feed and Finnhub's company news, earnings, analyst rating changes,
trading volume and 52-week highs or lows. The free AI reader (Groq or Gemini) puts it in two or three plain
sentences from those facts alone; without one, a template does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

import discord
import numpy as np
from discord import app_commands

from .. import embeds as E, stats
from ..apis.finnhub import Finnhub
from ..limits import clip, fit_embed
from ..universe import CRYPTO, SECTORS, short, title_of
from . import Feature

log = logging.getLogger(__name__)

# Yahoo's sector and industry names -> the ETF that tracks them
SECTOR_ETF = {"Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
              "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Industrials": "XLI", "Utilities": "XLU",
              "Basic Materials": "XLB", "Real Estate": "XLRE", "Communication Services": "XLC"}
INDUSTRY_ETF = {"Semiconductors": "SMH", "Semiconductor Equipment & Materials": "SMH"}
# Finnhub's industry names (its profile's finnhubIndustry) -> ETF, for when Yahoo has no profile
FINNHUB_ETF = {"Semiconductors": "SMH", "Technology": "XLK", "Banking": "XLF", "Financial Services": "XLF",
               "Insurance": "XLF", "Pharmaceuticals": "XLV", "Biotechnology": "XLV", "Health Care": "XLV",
               "Life Sciences Tools & Services": "XLV", "Energy": "XLE", "Oil & Gas": "XLE", "Utilities": "XLU",
               "Real Estate": "XLRE", "Retail": "XLY", "Automobiles": "XLY", "Hotels, Restaurants & Leisure": "XLY",
               "Media": "XLC", "Telecommunication": "XLC", "Chemicals": "XLB", "Metals & Mining": "XLB",
               "Aerospace & Defense": "XLI", "Machinery": "XLI", "Airlines": "XLI", "Logistics & Transportation": "XLI",
               "Beverages": "XLP", "Food Products": "XLP", "Tobacco": "XLP"}
MARKET = "^GSPC"
MIN_DAYS = 60  # fewer common days than this: no betas (the market part assumes a beta of 1)
NEWS_HOURS = 36
SYSTEM = """You explain in two or three plain sentences why a stock, ETF, index or coin moved, for a Discord \
channel of retail investors. Use ONLY the facts given: the breakdown of the move (market part, sector part, \
company-specific part, in percentage points), the headlines with their times, earnings, analyst changes, volume \
and 52-week levels. Lead with the biggest driver. If the company-specific part is small, say the move is mostly \
the market or the sector. If the company-specific part is large but no headline or event explains it, say the \
cause isn't clear from the news. Never invent facts, give advice or predict prices."""
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "driver": {"type": "string", "enum": ["market", "sector", "company news", "earnings", "analysts", "unclear"]},
        "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
    },
    "required": ["summary", "driver", "confidence"],
    "additionalProperties": False,
}


@dataclass
class Headline:
    title: str
    source: str
    at: float
    url: str = ""


@dataclass
class Why:
    symbol: str
    name: str
    market: str  # stocks or crypto
    move: float  # % over the session
    session: str  # "today", "last session" or "24h"
    benchmark: str = ""  # ^GSPC or BTC-USD
    benchmark_move: float | None = None
    sector: str = ""  # its sector ETF
    sector_move: float | None = None
    market_beta: float | None = None
    sector_beta: float | None = None
    parts: dict[str, float] = field(default_factory=dict)  # market / sector / own, in percentage points
    ext_move: float | None = None  # pre-market or after-hours
    volume_ratio: float | None = None
    headlines: list[Headline] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # earnings, analysts, 52-week levels...
    sectors: list[tuple[str, float]] = field(default_factory=list)  # for an index: each sector's move
    summary: str = ""
    driver: str = ""
    by: str = ""  # which AI wrote the summary ("" for the template)


def _returns(bars) -> tuple[np.ndarray, np.ndarray]:
    c = np.asarray(bars.close, dtype=float)
    r = c[1:] / c[:-1] - 1
    return stats.days(bars)[1:], r


def fit_betas(stock, market, sector=None, days: int = 250) -> tuple[float | None, float | None]:
    """The stock's sensitivity to the market and to its sector's own move beyond the market, from the last
    `days` common daily returns: r = a + b_m * market + b_s * (sector - market). (None, None) with too little
    history; the sector beta is None without a sector."""
    d_s, r_s = _returns(stock)
    d_m, r_m = _returns(market)
    common = np.intersect1d(d_s, d_m)
    if sector is not None:
        d_x, r_x = _returns(sector)
        common = np.intersect1d(common, d_x)
    common = common[-days:]
    if len(common) < MIN_DAYS:
        return None, None
    y = r_s[np.searchsorted(d_s, common)]
    m = r_m[np.searchsorted(d_m, common)]
    cols = [np.ones_like(m), m]
    if sector is not None:
        x = r_x[np.searchsorted(d_x, common)]
        cols.append(x - m)
    ok = np.all(np.isfinite(np.column_stack(cols + [y])), axis=1)
    if ok.sum() < MIN_DAYS:
        return None, None
    coef, *_ = np.linalg.lstsq(np.column_stack(cols)[ok], y[ok], rcond=None)
    b_m = float(coef[1])
    b_s = float(coef[2]) if sector is not None else None
    clamp = lambda b: None if b is None or not math.isfinite(b) else max(-3.0, min(5.0, b))  # noqa: E731
    return clamp(b_m), clamp(b_s)


def decompose(move: float, market_move: float | None, sector_move: float | None, b_m: float | None,
              b_s: float | None) -> dict[str, float]:
    """The move in percentage points: what the market explains, what the sector adds, and the rest."""
    parts: dict[str, float] = {}
    if market_move is None:
        return {"own": move}
    parts["market"] = (1.0 if b_m is None else b_m) * market_move
    if sector_move is not None and b_s is not None:
        parts["sector"] = b_s * (sector_move - market_move)
    parts["own"] = move - sum(parts.values())
    return parts


def template_summary(w: Why) -> tuple[str, str]:
    """The plain-words explanation without an AI: (summary, driver)."""
    way = "up" if w.move >= 0 else "down"
    name = w.name or short(w.symbol)
    if w.sectors:  # an index or broad ETF
        best, worst = w.sectors[0], w.sectors[-1]
        text = (f"{name} is {way} {abs(w.move):.1f}% {w.session}. Leading: {SECTORS.get(best[0], best[0])} "
                f"({best[1]:+.1f}%); lagging: {SECTORS.get(worst[0], worst[0])} ({worst[1]:+.1f}%).")
        if w.headlines:
            text += f" The biggest market story: \"{clip(w.headlines[0].title, 120)}\"."
        return text, "market"
    p = w.parts
    own = p.get("own", w.move)
    market = p.get("market", 0.0)
    sector = p.get("sector", 0.0)
    bench = "Bitcoin" if w.benchmark == "BTC-USD" else "the S&P 500"
    bits = []
    if "market" in p and w.benchmark_move is not None:
        bits.append(f"{bench} ({w.benchmark_move:+.1f}%) accounts for about {market:+.1f} points")
    if "sector" in p and abs(sector) >= 0.1:
        bits.append(f"its sector ({SECTORS.get(w.sector, w.sector)}, {w.sector_move:+.1f}%) about {sector:+.1f} more")
    text = f"{name} is {way} {abs(w.move):.1f}% {w.session}."
    if bits:
        joined = "; ".join(bits)
        text += " " + joined[:1].upper() + joined[1:] + "."
    if abs(own) < max(0.5, abs(w.move) * 0.35) and bits:
        driver = "sector" if abs(sector) > abs(market) else "market"
        text += " So it's mostly moving with the " + ("sector." if driver == "sector" else "market.")
        return text, driver
    if bits:
        text += f" The remaining {own:+.1f} points are its own."
    earnings = next((e for e in w.events if e.startswith("Earnings")), None)
    if earnings:
        text += f" {earnings}."
        return text, "earnings"
    if w.headlines:
        text += f" The likely reason in the news: \"{clip(w.headlines[0].title, 120)}\" ({w.headlines[0].source})."
        return text, "company news"
    text += " No headline in the news explains it yet."
    return text, "unclear"


def why_embed(w: Why) -> discord.Embed:
    way = "up" if w.move >= 0 else "down"
    e = discord.Embed(title=f"🤔 Why is {title_of(w.symbol, w.name)} {way} {abs(w.move):.1f}% {w.session}?",
                      color=E.GREEN if w.move >= 0 else E.RED, description=clip(w.summary, 1500))
    if w.parts and not w.sectors:
        rows = []
        labels = {"market": "Market" if w.benchmark != "BTC-USD" else "Bitcoin", "sector": "Sector",
                  "own": "Its own"}
        for key in ("market", "sector", "own"):
            if key in w.parts:
                rows.append((labels[key], w.parts[key]))
        bars = "\n".join(f"`{name:<8}` {E.arrow(v)} **{v:+.2f}** pts" for name, v in rows)
        notes = []
        if w.market_beta is not None:
            notes.append(f"beta {w.market_beta:.2f} to {'Bitcoin' if w.benchmark == 'BTC-USD' else 'the S&P 500'}")
        if w.sector and w.sector_move is not None:
            notes.append(f"sector {short(w.sector)} {w.sector_move:+.1f}%")
        e.add_field(name="Breakdown of the move", value=bars + (f"\n-# {' · '.join(notes)}" if notes else ""),
                    inline=False)
    if w.sectors:
        e.add_field(name="Sectors", value=" · ".join(f"{E.arrow(v)} {SECTORS.get(s, s)} {v:+.1f}%"
                                                     for s, v in w.sectors), inline=False)
    if w.headlines:
        e.add_field(name="Headlines", value=clip("\n".join(
            (f"[{clip(h.title, 110)}]({h.url})" if h.url and len(h.url) <= 160 else f"**{clip(h.title, 110)}**")
            + f" · *{h.source}* {E.ts(h.at)}" for h in w.headlines[:5]), 1024), inline=False)
    extra = list(w.events)
    if w.volume_ratio is not None:
        extra.append(f"Volume {w.volume_ratio:.1f}× its 3-month average")
    if w.ext_move is not None:
        extra.append(f"Outside market hours: {w.ext_move:+.1f}%")
    if extra:
        e.add_field(name="Also", value=clip("\n".join(f"• {x}" for x in extra), 1024), inline=False)
    e.set_footer(text=(f"Summary by {w.by} from these facts" if w.by else "Summary from these facts")
                 + " · betas from a year of daily moves · not financial advice")
    return fit_embed(e)


class WhyDesk(Feature):
    name = "why"
    help_group = "🔍 Explain"

    def __init__(self, bot):
        super().__init__(bot)
        data = bot.engine.data
        self.finnhub = Finnhub(data.http, state_file=bot.data_dir / "apis.json") if hasattr(data, "http") else None
        self._sector: dict[str, tuple[float, str]] = {}  # symbol -> (when, sector ETF or "")

    def help(self):
        return [("why", "why a stock or coin is moving: market, sector or its own news")]

    def status(self):
        return [f"**Finnhub** {self.finnhub.status_line()}"] if self.finnhub else []

    def register(self, tree) -> None:
        bot = self.bot

        @tree.command(name="why", description="Why is it moving? The market, its sector, or its own news")
        @app_commands.describe(symbol="Ticker or name, e.g. NVDA, tesla, BTC")
        async def why_cmd(interaction: discord.Interaction, symbol: str):
            await interaction.response.defer(thinking=True)
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return
            w = await self.explain(r.symbol, r.name, r.market)
            if w is None:
                await interaction.followup.send(f"I couldn't get a price for **{clip(r.name or r.symbol, 40)}** "
                                                "right now. Try again in a minute.")
                return
            await interaction.followup.send(embed=why_embed(w))

        suggest = getattr(bot, "symbol_suggestions", None)
        if suggest is not None:
            why_cmd.autocomplete("symbol")(suggest)

    # ----- gathering the facts -----

    async def explain(self, symbol: str, name: str, market: str) -> Why | None:
        crypto = market == CRYPTO
        bench = "BTC-USD" if crypto else MARKET
        is_index = symbol.startswith("^") or symbol in ("SPY", "QQQ", "DIA", "IWM", "VOO", "VTI")
        sector = "" if crypto or is_index else await self.sector_of(symbol)
        wanted = [symbol] + ([bench] if symbol != bench else []) + ([sector] if sector else [])
        if is_index:
            wanted += list(SECTORS)
        quotes = await self.bot.engine.data.quotes(list(dict.fromkeys(wanted)))
        q = quotes.get(symbol)
        if q is None or q.change_pct is None or not math.isfinite(q.change_pct):
            return None
        session = "in 24h" if crypto else ("today" if q.market_state in ("REGULAR", "") else "last session")
        w = Why(symbol, name or q.name, market, q.change_pct, session, bench if symbol != bench else "")
        if q.ext_price and q.ext_change_pct is not None and q.market_state not in ("REGULAR", "") and not crypto:
            w.ext_move = q.ext_change_pct
        bq = quotes.get(bench)
        w.benchmark_move = bq.change_pct if bq is not None and symbol != bench else None
        sq = quotes.get(sector) if sector else None
        if sq is not None and sq.change_pct is not None:
            w.sector, w.sector_move = sector, sq.change_pct
        if is_index:
            w.sectors = sorted(((s, quotes[s].change_pct) for s in SECTORS
                                if s in quotes and quotes[s].change_pct is not None), key=lambda x: -x[1])
        elif w.benchmark_move is not None:
            w.market_beta, w.sector_beta = await self.betas(symbol, bench, w.sector or None)
            w.parts = decompose(w.move, w.benchmark_move, w.sector_move if w.sector else None, w.market_beta,
                                w.sector_beta)
        if q.volume and q.avg_volume and q.avg_volume > 0 and not crypto:
            w.volume_ratio = q.volume / q.avg_volume
        if q.high52 and q.price >= q.high52 * 0.995:
            w.events.append("At a 52-week high")
        elif q.low52 and q.price <= q.low52 * 1.005:
            w.events.append("At a 52-week low")
        w.headlines = await self.headlines(symbol, name, market, is_index)
        if not crypto and not is_index:
            w.events = await self.company_events(symbol, q) + w.events
        await self.summarise(w)
        return w

    async def sector_of(self, symbol: str) -> str:
        hit = self._sector.get(symbol)
        if hit and time.monotonic() - hit[0] < 7 * 86400:
            return hit[1]
        etf = ""
        try:
            prof = (await asyncio.wait_for(self.bot.engine.data.summary(symbol, ("assetProfile",)), 8)
                    ).get("assetProfile") or {}
            etf = INDUSTRY_ETF.get(prof.get("industry") or "") or SECTOR_ETF.get(prof.get("sector") or "", "")
        except Exception:
            log.debug("No Yahoo profile for %s", symbol, exc_info=True)
        if not etf and self.finnhub and self.finnhub.enabled:
            try:
                etf = FINNHUB_ETF.get((await self.finnhub.profile(symbol)).get("finnhubIndustry") or "", "")
            except Exception:
                log.debug("No Finnhub profile for %s", symbol, exc_info=True)
        self._sector[symbol] = (time.monotonic(), etf)
        return etf

    async def betas(self, symbol: str, bench: str, sector: str | None) -> tuple[float | None, float | None]:
        cache = self.bot.engine.cache
        wanted = [symbol, bench] + ([sector] if sector else [])
        found = await asyncio.gather(*(cache.daily(s) for s in wanted), return_exceptions=True)
        if any(isinstance(b, BaseException) for b in found[:2]):
            return None, None
        sector_bars = found[2] if sector and not isinstance(found[2], BaseException) else None
        try:
            return await self.bot.engine.run(fit_betas, found[0], found[1], sector_bars)
        except Exception:
            log.warning("Betas for %s failed", symbol, exc_info=True)
            return None, None

    async def headlines(self, symbol: str, name: str, market: str, is_index: bool) -> list[Headline]:
        now = time.time()
        tick = short(symbol).upper()
        words = {tick} | ({(name or "").split()[0].lower()} if name else set())
        out: list[Headline] = []
        for a in reversed(getattr(self.bot, "recent_news", [])):
            h = a.headline
            if now - h.published > NEWS_HOURS * 3600:
                continue
            if is_index:
                hit = a.market in ("stocks", "macro") and a.importance >= 55
            else:
                hit = symbol in a.tickers or tick in a.tickers or any(
                    re.search(rf"\b{re.escape(wd)}\b", h.title, re.I) for wd in words if len(wd) >= 3)
            if hit:
                out.append(Headline(h.title, h.source, h.published, h.link))
        if self.finnhub and self.finnhub.enabled and market != CRYPTO and not is_index:
            try:
                for n in await self.finnhub.company_news(tick, days=2):
                    t = float(n.get("datetime") or 0)
                    if now - t <= NEWS_HOURS * 3600:
                        out.append(Headline(str(n["headline"]), str(n.get("source") or "Finnhub"), t,
                                            str(n.get("url") or "")))
            except Exception as exc:
                log.info("Finnhub news for %s unavailable: %s", tick, exc)
        seen, unique = set(), []
        for h in sorted(out, key=lambda h: -h.at):
            key = re.sub(r"\W+", " ", h.title.lower()).strip()[:60]
            if key and key not in seen:
                seen.add(key)
                unique.append(h)
        return unique[:6]

    async def company_events(self, symbol: str, q) -> list[str]:
        events = []
        now = time.time()
        t = q.extra.get("earningsTimestamp") or q.extra.get("earningsTimestampStart")
        if isinstance(t, (int, float)) and -3 * 86400 <= t - now <= 2 * 86400:
            when = "reported" if t <= now else "due"
            text = f"Earnings {when} {E.ts(t)}"
            if when == "reported" and self.finnhub and self.finnhub.enabled:
                try:
                    last = (await self.finnhub.earnings(short(symbol)))[:1]
                    if last and last[0].get("surprisePercent") is not None:
                        s = float(last[0]["surprisePercent"])
                        text += f": EPS {'beat' if s >= 0 else 'missed'} estimates by {abs(s):.1f}%"
                except Exception as exc:
                    log.info("Finnhub earnings for %s unavailable: %s", symbol, exc)
            events.append(text)
        if self.finnhub and self.finnhub.enabled:
            try:
                rec = await self.finnhub.recommendation(short(symbol))
                if len(rec) >= 2:
                    def bulls(r):
                        return int(r.get("strongBuy") or 0) + int(r.get("buy") or 0)

                    def bears(r):
                        return int(r.get("strongSell") or 0) + int(r.get("sell") or 0)
                    db, ds = bulls(rec[0]) - bulls(rec[1]), bears(rec[0]) - bears(rec[1])
                    if db or ds:
                        events.append(f"Analysts this month: {db:+d} buy, {ds:+d} sell ratings vs last month")
            except Exception as exc:
                log.info("Finnhub ratings for %s unavailable: %s", symbol, exc)
        return events

    async def summarise(self, w: Why) -> None:
        w.summary, w.driver = template_summary(w)
        ai = getattr(self.bot, "ai", None)
        if ai is None or not ai.enabled:
            return
        facts = {
            "symbol": short(w.symbol), "name": w.name, "move_pct": round(w.move, 2), "session": w.session,
            "benchmark": "Bitcoin" if w.benchmark == "BTC-USD" else ("S&P 500" if w.benchmark else ""),
            "benchmark_move_pct": None if w.benchmark_move is None else round(w.benchmark_move, 2),
            "sector": SECTORS.get(w.sector, w.sector), "sector_move_pct": None if w.sector_move is None else round(
                w.sector_move, 2),
            "breakdown_points": {k: round(v, 2) for k, v in w.parts.items()},
            "sectors_today": [(SECTORS.get(s, s), round(v, 2)) for s, v in w.sectors],
            "headlines": [{"title": h.title, "source": h.source, "hours_ago": round((time.time() - h.at) / 3600, 1)}
                          for h in w.headlines[:6]],
            "events": w.events, "volume_vs_average": None if w.volume_ratio is None else round(w.volume_ratio, 2),
            "outside_hours_move_pct": w.ext_move,
        }
        try:
            out = await ai.complete(SYSTEM, json.dumps(facts), SCHEMA, "why", wait=8)
        except Exception:
            log.warning("AI summary for %s failed", w.symbol, exc_info=True)
            out = None
        summary = str((out or {}).get("summary") or "").strip()
        if summary:
            w.summary = clip(summary, 1200)
            w.driver = str(out.get("driver") or w.driver)
            readers = [s for s in ai.statuses() if s.last_ok and not s.last_error]
            w.by = max(readers, key=lambda s: s.last_ok).name if readers else "AI"

