"""📅 The market calendar: US economic releases and earnings, with playbooks before and results after.

Before: what the market usually did on the last dozen days the same release came out (the S&P 500's move, against
an ordinary day's), the options market's expected move for a company reporting, and its last four earnings
reactions. After: the actual number against the forecast and how the market took it, and a company's move after
its report against what options expected. The channel gets a week-ahead preview on Sunday evening, each trading
day's agenda before the open, results as they come out, and earnings reactions after the open and after the close.

Schedules come from FRED (official release dates, FRED_API_KEY) and the Fed's FOMC calendar; forecasts, actuals
and earnings from Nasdaq's public calendars.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import discord
import numpy as np
from discord import app_commands

from .. import embeds as E, stats
from ..apis.calendars import NEW_YORK, Calendars, EconEvent, Earning, surprise
from ..briefs import Post
from ..hours import is_trading_day
from ..limits import clip, fit_embed
from ..universe import STOCKS, short
from . import Feature

log = logging.getLogger(__name__)

KIND = "calendar"
BIG_CAP = 50e9  # earnings shown in the week ahead without being on a watchlist
DAY_CAP = 10e9  # ... and in a day's agenda
RELEASES = {  # FRED release id -> (event key, name, ET time)
    10: ("cpi", "CPI", (8, 30)), 50: ("payrolls", "Jobs report", (8, 30)), 53: ("gdp", "GDP", (8, 30)),
    54: ("pce", "PCE inflation", (8, 30)), 9: ("retail", "Retail sales", (8, 30)), 46: ("ppi", "PPI", (8, 30)),
    180: ("claims", "Jobless claims", (8, 30)), 192: ("jolts", "JOLTS job openings", (10, 0)),
}
FAMILY = {"core_cpi": "cpi", "unemployment": "payrolls", "core_pce": "pce", "core_ppi": "ppi"}
IMPORTANCE = {"cpi": 3, "payrolls": 3, "fomc": 3, "gdp": 2, "pce": 2, "retail": 2, "ppi": 2, "ism": 2,
              "minutes": 2, "claims": 1, "jolts": 1, "sentiment": 1}
INFLATION = {"cpi", "core_cpi", "pce", "core_pce", "ppi", "core_ppi"}


@dataclass
class Playbook:
    name: str
    days: int  # how many past releases
    avg_move: float  # the S&P 500's average absolute move on those days, %
    normal_move: float  # on an ordinary day over the last year, %
    up: int
    biggest: tuple[str, float] | None = None


@dataclass
class Item:
    day: str
    at: float  # epoch seconds (0 when unknown)
    key: str  # event family, or "earnings"
    title: str
    detail: str = ""
    importance: int = 1
    earning: Earning | None = None
    events: list[EconEvent] = field(default_factory=list)


def day_moves(bars, days: list[str]) -> list[tuple[str, float]]:
    """The close-to-close % move on each of `days` (those the history covers)."""
    d = stats.days(bars)
    c = np.asarray(bars.close, dtype=float)
    out = []
    for day in days:
        i = int(np.searchsorted(d, np.datetime64(day)))
        if 0 < i < len(d) and str(d[i]) == day and c[i - 1] > 0:
            out.append((day, float(c[i] / c[i - 1] - 1) * 100))
    return out


def make_playbook(name: str, bars, days: list[str]) -> Playbook | None:
    moves = day_moves(bars, days)
    if len(moves) < 4:
        return None
    c = np.asarray(bars.close, dtype=float)[-253:]
    normal = float(np.mean(np.abs(np.diff(c) / c[:-1]))) * 100 if len(c) > 20 else 0.0
    big = max(moves, key=lambda m: abs(m[1]))
    return Playbook(name, len(moves), float(np.mean([abs(m) for _, m in moves])), normal,
                    sum(m > 0 for _, m in moves), big)


def playbook_text(p: Playbook | None) -> str:
    if p is None:
        return ""
    ratio = p.avg_move / p.normal_move if p.normal_move else 1.0
    feel = "bigger than" if ratio >= 1.2 else "smaller than" if ratio <= 0.85 else "about"
    return (f"S&P 500 moved ±{p.avg_move:.1f}% on the last {p.days} ({feel} a normal day's ±{p.normal_move:.1f}%), "
            f"up {p.up} of {p.days}")


def reaction(bars, reported: str, timing: str) -> float | None:
    """The stock's % move on the earnings: the report day for "before open", the next day for "after close",
    else whichever of the two moved more."""
    d = stats.days(bars)
    c = np.asarray(bars.close, dtype=float)
    i = int(np.searchsorted(d, np.datetime64(reported)))
    if i >= len(d) or i == 0 or str(d[i]) != reported:
        return None
    same = c[i] / c[i - 1] - 1
    nxt = c[i + 1] / c[i] - 1 if i + 1 < len(d) else None
    if timing == "before open":
        move = same
    elif timing == "after close":
        move = nxt
    else:
        move = same if nxt is None or abs(same) >= abs(nxt) else nxt
    return None if move is None or not np.isfinite(move) else float(move) * 100


def et(t: float) -> str:
    return datetime.fromtimestamp(t, NEW_YORK).strftime("%-I:%M %p").replace(":00 ", " ") if t else ""


class CalendarDesk(Feature):
    name = "calendar"
    help_group = "📅 Calendar"

    def __init__(self, bot):
        super().__init__(bot)
        http = getattr(bot.engine.data, "http", None)
        if http is None:
            raise RuntimeError("no shared Http")
        self.cal = Calendars(http, state_file=bot.data_dir / "apis.json")
        self._books: dict[str, tuple[float, Playbook | None]] = {}

    def jobs(self):
        return [("calendar", 300, self.job)]

    def help(self):
        return [("calendar", "the week's economic releases and big earnings, with what usually happens"),
                ("earnings", "a company's next report, expected move and last 4 reactions")]

    def status(self):
        return [f"**FRED** {self.cal.fred.status_line()}"]

    # ----- the facts -----

    def watchlist(self) -> set[str]:
        try:
            return set(self.bot._watchlist(STOCKS))
        except Exception:
            return set()

    async def schedule(self, start: date, days: int, cap: float = BIG_CAP) -> list[Item]:
        """Economic releases (Nasdaq's with forecasts, else FRED's dates), FOMC decisions and notable earnings."""
        items: list[Item] = []
        end = start + timedelta(days=days)
        watch = self.watchlist()
        seen: set[tuple[str, str]] = set()
        fomc = set()
        try:
            fomc = {d for d in await self.cal.fomc_days() if start.isoformat() <= d < end.isoformat()}
        except Exception as exc:
            log.info("FOMC calendar unavailable: %s", exc)
        for i in range(days):
            day = start + timedelta(days=i)
            if day.weekday() >= 5:
                continue
            try:
                events = await self.cal.econ(day)
            except Exception as exc:
                log.info("Economic calendar for %s unavailable: %s", day, exc)
                events = []
            groups: dict[str, list[EconEvent]] = {}
            for e in events:
                groups.setdefault(FAMILY.get(e.key, e.key), []).append(e)
            for fam, evs in groups.items():
                seen.add((day.isoformat(), fam))
                lead = max(evs, key=lambda e: e.importance)
                items.append(Item(day.isoformat(), min(e.at for e in evs), fam, lead.short if fam == lead.key else
                                  {"cpi": "CPI", "payrolls": "Jobs report", "pce": "PCE inflation", "ppi": "PPI"}.get(
                                      fam, lead.short), importance=IMPORTANCE.get(fam, 1), events=evs))
            if day.isoformat() in fomc and (day.isoformat(), "fomc") not in seen:
                at = datetime(day.year, day.month, day.day, 14, 0, tzinfo=NEW_YORK).timestamp()
                items.append(Item(day.isoformat(), at, "fomc", "Fed decision", importance=3))
                seen.add((day.isoformat(), "fomc"))
            try:
                for er in await self.cal.earnings(day):
                    if er.symbol in watch or (er.market_cap or 0) >= cap:
                        items.append(Item(day.isoformat(), 0, "earnings", er.symbol, importance=2, earning=er))
            except Exception as exc:
                log.info("Earnings calendar for %s unavailable: %s", day, exc)
        if self.cal.fred.enabled:  # official dates for releases Nasdaq hasn't listed yet
            for rid, (key, name, (hh, mm)) in RELEASES.items():
                try:
                    dates = await self.cal.release_dates_ahead(rid, start, end)
                except Exception as exc:
                    log.info("FRED dates for %s unavailable: %s", name, exc)
                    continue
                for d in dates:
                    if (d, key) in seen:
                        continue
                    y, m, dd = (int(x) for x in d.split("-"))
                    items.append(Item(d, datetime(y, m, dd, hh, mm, tzinfo=NEW_YORK).timestamp(), key, name,
                                      importance=IMPORTANCE.get(key, 1)))
                    seen.add((d, key))
        return sorted(items, key=lambda it: (it.day, it.at or 9e12, -(it.earning.market_cap or 0) if it.earning
                                             else 0))

    async def playbook(self, key: str) -> Playbook | None:
        hit = self._books.get(key)
        if hit and time.time() - hit[0] < 12 * 3600:
            return hit[1]
        book = None
        try:
            today = date.today()
            if key == "fomc":
                days = [d for d in await self.cal.fomc_days() if d < today.isoformat()][-12:]
            else:
                rid = next((r for r, (k, _, _) in RELEASES.items() if k == key), None)
                days = await self.cal.release_dates(rid, today, 12) if rid and self.cal.fred.enabled else []
            if days:
                bars = await self.bot.engine.cache.daily("^GSPC", fresh=12 * 3600)
                names = {k: n for k, n, _ in RELEASES.values()}
                name = "Fed decision days" if key == "fomc" else f"{names.get(key, key)} days"
                book = await self.bot.engine.run(make_playbook, name, bars, days)
        except Exception:
            log.info("No playbook for %s", key, exc_info=True)
        self._books[key] = (time.time(), book)
        return book

    async def earnings_view(self, symbol: str, timing: str = "") -> dict:
        """Expected move from options, and the last 4 reports' EPS surprises and price reactions."""
        out: dict = {"symbol": symbol, "past": []}
        try:
            past = await self.cal.surprises(symbol)
        except Exception as exc:
            log.info("Earnings history for %s unavailable: %s", symbol, exc)
            past = []
        bars = None
        try:
            bars = await self.bot.engine.cache.daily(symbol, fresh=12 * 3600)
        except Exception:
            pass
        for p in past[:4]:
            move = reaction(bars, p["reported"], timing) if bars is not None else None
            out["past"].append({**p, "move": move})
        try:
            ov = await asyncio.wait_for(self.bot.engine.options(symbol), 15)
            if ov and ov.expected_move and ov.spot:
                out["expected"] = ov.expected_move / ov.spot * 100
        except Exception:
            log.debug("No options for %s", symbol, exc_info=True)
        return out

    # ----- what gets posted -----

    def econ_line(self, it: Item, book: Playbook | None) -> str:
        head = f"`{et(it.at) or '—':>8}` {'🔴' if it.importance == 3 else '🟠' if it.importance == 2 else '⚪'} **{it.title}**"
        bits = []
        for e in it.events[:3]:
            if e.consensus or e.previous:
                bits.append(f"{clip(e.name, 40)}: " + " · ".join(
                    x for x in (f"est **{e.consensus}**" if e.consensus else "",
                                f"prev {e.previous}" if e.previous else "") if x))
        text = head + ("\n-# " + " | ".join(bits) if bits else "")
        pb = playbook_text(book)
        return text + (f"\n-# {pb}" if pb else "")

    def earning_line(self, er: Earning, view: dict | None) -> str:
        cap = f"${er.market_cap / 1e9:,.0f}B" if er.market_cap else ""
        bits = [x for x in (cap, f"EPS est ${er.eps_forecast:.2f}" if er.eps_forecast is not None else "") if x]
        line = f"**{short(er.symbol)}** {clip(er.name, 30)}" + (f" · {' · '.join(bits)}" if bits else "")
        if view:
            if view.get("expected"):
                line += f" · options expect ±{view['expected']:.1f}%"
            moves = [f"{p['move']:+.1f}%" for p in view["past"] if p.get("move") is not None]
            if moves:
                line += f"\n-# last {len(moves)} reactions: {', '.join(moves)}"
        return line

    async def agenda(self, day: date) -> discord.Embed:
        items = await self.schedule(day, 1, cap=DAY_CAP)
        e = discord.Embed(title=f"📅 Today · {day:%A, %B %-d}", color=E.BLUE)
        econ = [it for it in items if it.key != "earnings"]
        if econ:
            lines = [self.econ_line(it, await self.playbook(it.key) if it.importance >= 2 else None) for it in econ]
            e.add_field(name="Economy (ET)", value=clip("\n".join(lines), 1024), inline=False)
        earn = [it.earning for it in items if it.earning]
        earn.sort(key=lambda x: -(x.market_cap or 0))
        for label in ("before open", "after close", ""):
            group = [x for x in earn if x.timing == label][:8]
            if not group:
                continue
            views = await asyncio.gather(*(self.earnings_view(x.symbol, x.timing) for x in group[:4]))
            lines = [self.earning_line(x, views[i] if i < len(views) else None) for i, x in enumerate(group)]
            e.add_field(name={"before open": "Earnings before the open", "after close": "Earnings after the close",
                              "": "Earnings (time not set)"}[label], value=clip("\n".join(lines), 1024), inline=False)
        if not e.fields:
            e.description = "No major US releases or big earnings today."
        e.set_footer(text="Forecasts: Nasdaq · dates: FRED & the Federal Reserve · playbooks from the S&P 500's past "
                          "moves · not financial advice")
        return fit_embed(e)

    async def week_ahead(self, start: date) -> discord.Embed:
        items = await self.schedule(start, 7, cap=BIG_CAP)
        e = discord.Embed(title=f"🗓️ The week ahead · {start:%b %-d}–{start + timedelta(days=4):%b %-d}",
                          color=E.BLUE)
        by_day: dict[str, list[Item]] = {}
        for it in items:
            by_day.setdefault(it.day, []).append(it)
        for day, its in sorted(by_day.items()):
            econ = [it for it in its if it.key != "earnings" and it.importance >= 2]
            earn = [it.earning for it in its if it.earning][:8]
            lines = []
            for it in econ:
                book = await self.playbook(it.key)
                lines.append(f"{'🔴' if it.importance == 3 else '🟠'} **{it.title}** {et(it.at)}"
                             + (f"\n-# {playbook_text(book)}" if book else ""))
            if earn:
                lines.append("💼 " + ", ".join(f"**{short(x.symbol)}**" + (" (am)" if x.timing == "before open" else
                                                                         " (pm)" if x.timing == "after close" else "")
                                              for x in earn))
            if lines:
                d = date.fromisoformat(day)
                e.add_field(name=f"{d:%A %-d}", value=clip("\n".join(lines), 1024), inline=False)
        if not e.fields:
            e.description = "A quiet week: no major US releases or big earnings."
        e.set_footer(text="🔴 market-moving · 🟠 important · (am) before the open, (pm) after the close · "
                          "/earnings for any company")
        return fit_embed(e)

    def result_embed(self, fam: str, events: list[EconEvent], market_move: float | None) -> discord.Embed:
        lead = max(events, key=lambda e: e.importance)
        lines = []
        for e in events:
            s = surprise(e)
            words = ""
            if s and e.hot and e.consensus:
                if e.key == "fomc":
                    up, down = "more hawkish", "more dovish"
                elif e.key in INFLATION:
                    up, down = "hotter", "cooler"
                else:
                    up, down = "stronger", "weaker"
                words = f" · **{up if s * e.hot > 0 else down} than expected**"
            lines.append(f"**{clip(e.name, 50)}**: **{e.actual}**" + (f" vs {e.consensus} expected" if e.consensus else "")
                         + (f" (previous {e.previous})" if e.previous else "") + words)
        e = discord.Embed(title=f"📊 {lead.short if FAMILY.get(lead.key, lead.key) == lead.key else fam.upper()} is out",
                          color=E.BLUE, description="\n".join(lines))
        if market_move is not None:
            e.add_field(name="Market now", value=f"S&P 500 {E.arrow(market_move)} {market_move:+.2f}% today",
                        inline=False)
        e.set_footer(text="Forecasts and actuals: Nasdaq · not financial advice")
        return fit_embed(e)

    def reaction_embed(self, er: Earning, view: dict, move: float | None) -> discord.Embed:
        past = view.get("past") or []
        last = past[0] if past else {}
        e = discord.Embed(title=f"💼 {short(er.symbol)} after earnings: " + (f"{move:+.1f}%" if move is not None else "?"),
                          color=E.GREEN if (move or 0) >= 0 else E.RED)
        bits = []
        if view.get("expected"):
            bits.append(f"options expected ±{view['expected']:.1f}%"
                        + (" — **a bigger move than priced**" if move is not None and abs(move) > view["expected"]
                           else ""))
        if last.get("eps") is not None and last.get("consensus") is not None:
            bits.append(f"EPS ${last['eps']:.2f} vs ${last['consensus']:.2f} expected"
                        + (f" ({last['surprise_pct']:+.1f}%)" if last.get("surprise_pct") is not None else ""))
        e.description = "\n".join(bits) or None
        e.set_footer(text="EPS: Nasdaq · not financial advice")
        return fit_embed(e)

    # ----- the job -----

    async def job(self) -> None:
        channels = [(cid, cfg) for cid, cfg in self.bot.channels.of_kind(KIND)]
        if not channels:
            return
        ny = datetime.now(NEW_YORK)
        trading = is_trading_day(ny.date())
        for cid, cfg in channels:
            try:
                if cfg.briefs and ny.weekday() == 6 and self.bot._due(cid, "cal-week", ny, 18, 0, 240):
                    await self.bot.send(cid, Post([await self.week_ahead(ny.date() + timedelta(days=1))]))
                if cfg.briefs and trading and self.bot._due(cid, "cal-day", ny, 7, 45, 90):
                    await self.bot.send(cid, Post([await self.agenda(ny.date())]))
            except Exception:
                log.exception("Calendar post for %s failed", cid)
        if trading and 8 <= ny.hour < 17:
            await self.post_results(ny, channels)
        if trading and ((ny.hour, ny.minute) >= (9, 50) and ny.hour < 11 or (ny.hour, ny.minute) >= (16, 20)
                        and ny.hour < 18):
            await self.post_reactions(ny, channels)

    async def post_results(self, ny: datetime, channels) -> None:
        try:
            events = await self.cal.econ(ny.date(), ttl=240)
        except Exception as exc:
            log.info("Economic results unavailable: %s", exc)
            return
        groups: dict[str, list[EconEvent]] = {}
        for e in events:
            if e.released and e.importance >= 2:
                groups.setdefault(FAMILY.get(e.key, e.key), []).append(e)
        for fam, evs in groups.items():
            key = f"{ny:%Y-%m-%d}|{fam}"
            if self.bot.state.get("calendar_results", key):
                continue
            self.bot.state.set("calendar_results", key, time.time())
            q = (await self.bot.engine.data.quotes(["^GSPC"])).get("^GSPC")
            embed = self.result_embed(fam, evs, q.change_pct if q else None)
            for cid, cfg in channels:
                if cfg.alerts:
                    await self.bot.send(cid, Post([embed]))

    async def post_reactions(self, ny: datetime, channels) -> None:
        """After the open: yesterday's after-close reporters and today's before-open ones; after the close: today's
        before-open ones again are skipped, so only names not yet posted."""
        today = ny.date()
        prev = today - timedelta(days=1)
        while not is_trading_day(prev):
            prev -= timedelta(days=1)
        watch = self.watchlist()
        wanted: list[Earning] = []
        try:
            for er in await self.cal.earnings(prev):
                if er.timing == "after close":
                    wanted.append(er)
            for er in await self.cal.earnings(today):
                if er.timing == "before open":
                    wanted.append(er)
        except Exception as exc:
            log.info("Earnings for reactions unavailable: %s", exc)
            return
        wanted = [er for er in wanted if er.symbol in watch or (er.market_cap or 0) >= BIG_CAP]
        for er in wanted[:12]:
            key = f"{er.day}|{er.symbol}"
            if self.bot.state.get("calendar_reactions", key):
                continue
            q = (await self.bot.engine.data.quotes([er.symbol])).get(er.symbol)
            if q is None or q.change_pct is None:
                continue
            self.bot.state.set("calendar_reactions", key, time.time())
            view = await self.earnings_view(er.symbol, er.timing)
            embed = self.reaction_embed(er, view, q.change_pct)
            for cid, cfg in channels:
                if cfg.alerts:
                    await self.bot.send(cid, Post([embed]))

    # ----- commands -----

    def register(self, tree) -> None:
        bot = self.bot

        @tree.command(name="calendar", description="The week's US economic releases and big earnings, with playbooks")
        async def calendar_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(embed=await self.week_ahead(datetime.now(NEW_YORK).date()))

        @tree.command(name="earnings", description="A company's next report, expected move and last 4 reactions")
        @app_commands.describe(symbol="Ticker or name, e.g. NVDA, apple")
        async def earnings_cmd(interaction: discord.Interaction, symbol: str):
            await interaction.response.defer(thinking=True)
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return
            q = (await bot.engine.data.quotes([r.symbol])).get(r.symbol)
            view = await self.earnings_view(r.symbol)
            e = discord.Embed(title=f"💼 {r.name or r.symbol} ({short(r.symbol)}) earnings", color=E.BLUE)
            t = q.extra.get("earningsTimestamp") or q.extra.get("earningsTimestampStart") if q else None
            lines = []
            if isinstance(t, (int, float)) and t > time.time() - 86400:
                lines.append(f"Next report: **{E.ts(t, 'D')}** ({E.ts(t)})")
            if view.get("expected"):
                lines.append(f"Options expect a move of about **±{view['expected']:.1f}%** by the nearest expiry")
            e.description = "\n".join(lines) or "No upcoming report date found."
            rows = [f"`{p['reported']}` {p['quarter']}: EPS "
                    + (f"${p['eps']:.2f} vs ${p['consensus']:.2f}" if p.get("eps") is not None and p.get("consensus")
                       is not None else "?")
                    + (f" ({p['surprise_pct']:+.1f}%)" if p.get("surprise_pct") is not None else "")
                    + (f" · stock **{p['move']:+.1f}%**" if p.get("move") is not None else "")
                    for p in view["past"]]
            if rows:
                e.add_field(name="Last reports", value="\n".join(rows), inline=False)
            e.set_footer(text="EPS: Nasdaq · moves: the report day (before the open) or the next day (after the "
                              "close) · not financial advice")
            await interaction.followup.send(embed=fit_embed(e))

        suggest = getattr(bot, "symbol_suggestions", None)
        if suggest is not None:
            earnings_cmd.autocomplete("symbol")(suggest)

