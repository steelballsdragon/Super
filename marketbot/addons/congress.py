"""🏛️ Congress trades: what members of the House and Senate disclose buying and selling.

Pollers read the official disclosures (marketbot/apis/congress.py): the House index hourly and the Senate every
half hour. The first runs fill in this year and last year a batch at a time, newest first, without posting; after
that each new filing is posted to the Congress channel. Returns are estimates: a filing gives the trade date and
an amount band, not the price or share count, so a buy is valued from that day's close to the latest close and
compared with the S&P 500 over the same days. A copy-trader could only start on the filing date, so that return
is shown too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import discord
import numpy as np
from discord import app_commands

from .. import embeds as E, stats
from ..briefs import Post
from ..apis import congress as C
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from . import Feature

log = logging.getLogger(__name__)

KIND = "congress"
PER_RUN = {"house": 80, "senate": 60}  # filings read per run (the first fill spreads over several runs)
ALERT_DAYS = 4  # a newly found filing older than this isn't posted (it's history, not news)
LATE_DAYS = 45  # the STOCK Act's deadline
PRICED_TYPES = ("stock", "option", "etf")
PARTY = {"D": "🔵", "R": "🔴", "I": "🟣", "": "⚪"}
TITLE = {"house": "Rep.", "senate": "Sen."}
MAX_RETURN_TICKERS = 400
FOOTER = ("Official House Clerk & Senate eFD disclosures · trades are reported up to 45 days late · returns are "
          "estimates from closing prices · not investment advice")


def money(v: float | None) -> str:
    if not v:
        return "$0"
    for div, unit in ((1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            return f"${x:.0f}{unit}" if x >= 10 else f"${x:.1f}".rstrip("0").rstrip(".") + unit
    return f"${v:,.0f}"


def band(t: dict) -> str:
    low, high = t.get("amount_low") or 0, t.get("amount_high")
    return f"{money(low)}–{money(high)}" if high else (f"over {money(low)}" if low else (t.get("amount") or "?"))


def who(f: dict) -> str:
    where = f.get("district") if f.get("chamber") == "house" and f.get("district") else f.get("state") or ""
    tag = f"{f.get('party') or '?'}-{where}" if where else (f.get("party") or "")
    return f"{TITLE.get(f.get('chamber'), '')} {f.get('member')}" + (f" ({tag})" if tag else "")


def _close_on(bars, day: str) -> float | None:
    """The close on `day` or the next trading day."""
    d = np.datetime64(day)
    days = stats.days(bars)
    i = int(np.searchsorted(days, d))
    if i >= len(days) or (days[i] - d).astype(int) > 7:
        return None
    c = float(bars.close[i])
    return c if np.isfinite(c) and c > 0 else None


def score_trade(t: dict, bars, spx) -> dict | None:
    """Estimated returns from the trade date and from the filing date, the S&P 500's over the same days and the
    excess; None without prices."""
    entry, now = _close_on(bars, t["tx_date"]), float(bars.close[-1])
    if entry is None or not np.isfinite(now):
        return None
    out = {"ret": now / entry - 1}
    filed = _close_on(bars, t["filed"]) if t.get("filed") else None
    if filed:
        out["follow"] = now / filed - 1
    if spx is not None:
        s0, s1 = _close_on(spx, t["tx_date"]), float(spx.close[-1])
        if s0:
            out["spx"] = s1 / s0 - 1
            out["excess"] = out["ret"] - out["spx"]
    return out


def member_stats(trades: list[dict], scores: dict, since: str) -> dict:
    """A member's record since `since`: counts, the amount-weighted estimated return of their buys and its
    excess over the S&P 500, the share of buys that are up, and the most traded tickers."""
    recent = [t for t in trades if t["tx_date"] >= since]
    buys = [t for t in recent if t["kind"] == "purchase"]
    scored = [(t, scores[tkey(t)]) for t in buys if tkey(t) in scores]

    def weight(t):
        mid = (t["amount_low"] + t["amount_high"]) / 2 if t.get("amount_high") else t["amount_low"]
        return max(1000.0, float(mid or 0))
    out = {"trades": len(recent), "buys": len(buys), "sells": sum(t["kind"].startswith("sale") for t in recent),
           "scored": len(scored), "volume_low": sum(t["amount_low"] for t in recent),
           "tickers": Counter(t["ticker"] for t in recent if t.get("ticker")).most_common(5)}
    if scored:
        w = np.array([weight(t) for t, _ in scored])
        out["ret"] = float(np.average([s["ret"] for _, s in scored], weights=w))
        ex = [(s["excess"], weight(t)) for t, s in scored if "excess" in s]
        if ex:
            out["excess"] = float(np.average([e for e, _ in ex], weights=[x for _, x in ex]))
        out["hit"] = float(np.mean([s["ret"] > 0 for _, s in scored]))
    return out


def tkey(t: dict) -> str:
    return "|".join(str(x) for x in (t["filing"], t["tx_date"], t.get("ticker") or t["asset"][:30], t["kind"],
                                     t["amount"], t["owner"]))


class CongressDesk(Feature):
    name = "congress"
    help_group = "🏛️ Congress"

    def __init__(self, bot):
        super().__init__(bot)
        http = getattr(bot.engine.data, "http", None)
        if http is None:
            raise RuntimeError("no shared Http")
        self.house = C.House(http)
        self.senate = C.Senate(http)
        self.http = http
        self.path = bot.data_dir / "congress.json"
        data = read_json(self.path, {})
        self.filings: dict[str, dict] = data.get("filings") or {}
        self.trades: list[dict] = data.get("trades") or []
        self.synced: dict[str, bool] = data.get("synced") or {}
        self.house_index: dict[str, list] = data.get("house_index") or {}
        self.house.last_modified = data.get("house_stamps") or {}
        self.roster: C.Roster | None = None
        self.roster_at = 0.0
        self.scores: dict[str, dict] = {}
        self.scores_at = 0.0
        self._lock = asyncio.Lock()

    # ----- plumbing -----

    def jobs(self):
        return [("congress_house", 3600, self.job_house), ("congress_senate", 1800, self.job_senate),
                ("congress_returns", 6 * 3600, self.job_returns), ("congress_board", 1800, self.job_board)]

    def help(self):
        return [("congress", "Congress members' trades: the latest, a member's record, or who traded a ticker")]

    def status(self):
        backlog = "up to date" if all(self.synced.get(c) for c in ("house", "senate")) else "filling in history"
        return [f"**Congress** {len(self.filings):,} filings, {len(self.trades):,} trades · {backlog}"]

    def save(self) -> None:
        write_json(self.path, {"filings": self.filings, "trades": self.trades, "synced": self.synced,
                               "house_index": self.house_index, "house_stamps": self.house.last_modified})

    async def close(self) -> None:
        await self.senate.close()

    async def get_roster(self) -> C.Roster:
        if self.roster is None or time.time() - self.roster_at > 7 * 86400:
            try:
                self.roster = await C.fetch_roster(self.http)
                self.roster_at = time.time()
            except Exception:
                log.warning("Couldn't load the congress-legislators list", exc_info=True)
                self.roster = self.roster or C.Roster()
        return self.roster

    def start_date(self) -> date:
        return date(date.today().year - 1, 1, 1)

    def add(self, f: C.Filing, trades: list[C.Trade]) -> list[dict]:
        """Stores a filing and its trades, and returns them. The same trade restated by a later filing (an
        amendment) replaces the earlier copy; restated by an earlier filing, the later copy stays."""
        new = [asdict(t) for t in trades]
        keys = {t.key: t for t in trades}
        if keys:
            keep, newer = [], set()
            for t in self.trades:
                k = C.Trade(**t).key
                if k in keys and t["filed"] > f.filed:
                    newer.add(k)  # a later filing already has it
                    keep.append(t)
                elif k not in keys:
                    keep.append(t)
            self.trades = keep
            new = [asdict(t) for t in trades if t.key not in newer]
        self.trades.extend(new)
        self.filings[f.id] = {**asdict(f), "count": len(trades), "seen": time.time()}
        return new

    # ----- pollers -----

    async def job_house(self) -> None:
        async with self._lock:
            roster = await self.get_roster()
            years = [date.today().year] + ([date.today().year - 1] if not self.synced.get("house")
                                           or date.today().month == 1 else [])
            for year in years:
                rows = await self.house.index(year)
                if rows is not None:
                    self.house_index[str(year)] = rows
            pending = [r for y in years for r in self.house_index.get(str(y), [])
                       if f"H:{r['doc']}" not in self.filings and r["filed"] >= self.start_date().isoformat()]
            pending.sort(key=lambda r: r["filed"], reverse=True)
            fresh = []
            for r in pending[:PER_RUN["house"]]:
                info = roster.find("house", r["last"], district=r["district"]) or {}
                name = info.get("name") or f"{r['first']} {r['last']}".strip()
                f = C.Filing(f"H:{r['doc']}", "house", name, r["last"], r["state"], r["district"], r["filed"],
                             C.HOUSE_PTR.format(year=r["year"], doc=r["doc"]), paper=r["paper"],
                             party=info.get("party", ""))
                trades: list[C.Trade] = []
                if not f.paper:
                    try:
                        pdf = await self.house.ptr(r["year"], r["doc"])
                        if pdf is None:
                            continue  # listed but not posted yet: try again next run
                        raw = await asyncio.to_thread(C.parse_house_ptr, pdf)
                        trades = C.house_trades(f, raw)
                        if not raw:
                            f.paper = True  # no text layer after all
                    except Exception:
                        log.warning("House PTR %s couldn't be read", r["doc"], exc_info=True)
                        continue
                    await asyncio.sleep(0.6)
                fresh.append((f, self.add(f, trades)))
            done = len(pending) <= PER_RUN["house"]
            await self._finish("house", fresh, done)

    async def job_senate(self) -> None:
        async with self._lock:
            roster = await self.get_roster()
            since = date.today() - timedelta(days=14) if self.synced.get("senate") else self.start_date()
            rows, start = [], 0
            while True:
                total, page = await self.senate.search(since, start=start)
                rows += page
                start += len(page)
                if not page or start >= total or start >= 1000:
                    break
                await asyncio.sleep(1)
            filings = [f for f in (C.senate_filing(r) for r in rows) if f and f.id not in self.filings]
            filings.sort(key=lambda f: f.filed, reverse=True)
            fresh = []
            for f in filings[:PER_RUN["senate"]]:
                info = roster.find("senate", f.last, first=f.member.split()[0] if f.member else "") or {}
                f.party, f.state = info.get("party", ""), info.get("state", f.state)
                f.member = info.get("name") or f.member
                trades: list[C.Trade] = []
                if not f.paper:
                    html = await self.senate.report(f.url.replace(C.SENATE, ""))
                    if html is None:
                        continue
                    trades = C.senate_trades(f, C.parse_senate_report(html))
                    await asyncio.sleep(1)
                fresh.append((f, self.add(f, trades)))
            await self._finish("senate", fresh, len(filings) <= PER_RUN["senate"])

    async def _finish(self, chamber: str, fresh: list[tuple[C.Filing, list[dict]]], done: bool) -> None:
        was_synced = self.synced.get(chamber, False)
        if done and not was_synced:
            self.synced[chamber] = True
            log.info("Congress %s history filled in: %d filings", chamber, len(self.filings))
        self.save()
        if not was_synced:
            return  # history, not news: nothing posted while filling in
        cutoff = (date.today() - timedelta(days=ALERT_DAYS)).isoformat()
        for f, trades in fresh:
            if f.filed < cutoff:
                continue
            embed = filing_embed(asdict(f), trades, self.scores)
            for cid, cfg in self.bot.channels.of_kind(KIND):
                if cfg.alerts:
                    await self.bot.send(cid, Post([embed]))

    # ----- returns -----

    async def job_returns(self) -> None:
        """Prices every recent stock trade (most traded tickers first) against the S&P 500."""
        cache = self.bot.engine.cache
        since = (date.today() - timedelta(days=400)).isoformat()
        priced = [t for t in self.trades if t.get("ticker") and t["asset_type"] in PRICED_TYPES
                  and t["tx_date"] >= since]
        counts = Counter(t["ticker"] for t in priced)
        try:
            spx = await cache.daily("^GSPC", fresh=12 * 3600)
        except Exception:
            spx = None
        scores: dict[str, dict] = {}
        for ticker, _ in counts.most_common(MAX_RETURN_TICKERS):
            try:
                bars = await cache.daily(ticker, fresh=12 * 3600)
            except Exception:
                continue
            for t in priced:
                if t["ticker"] == ticker:
                    s = score_trade(t, bars, spx)
                    if s:
                        scores[tkey(t)] = s
            await asyncio.sleep(0)
        self.scores, self.scores_at = scores, time.time()

    # ----- views -----

    def by_member(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for t in self.trades:
            out[t["member"]].append(t)
        return out

    def leaderboard(self, days: int = 365, minimum: int = 3) -> list[tuple[str, dict]]:
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = []
        for member, trades in self.by_member().items():
            st = member_stats(trades, self.scores, since)
            if st["scored"] >= minimum and "excess" in st:
                rows.append((member, st))
        return sorted(rows, key=lambda r: -r[1]["excess"])

    def flows(self, days: int = 30) -> tuple[list, list]:
        since = (date.today() - timedelta(days=days)).isoformat()
        buys: dict[str, set] = defaultdict(set)
        sells: dict[str, set] = defaultdict(set)
        for t in self.trades:
            if t["tx_date"] < since or not t.get("ticker") or t["asset_type"] not in PRICED_TYPES:
                continue
            if t["kind"] == "purchase":
                buys[t["ticker"]].add(t["member"])
            elif t["kind"].startswith("sale"):
                sells[t["ticker"]].add(t["member"])

        def rank(d):
            return sorted(((k, len(v)) for k, v in d.items()), key=lambda x: (-x[1], x[0]))[:8]
        return rank(buys), rank(sells)

    def board(self) -> discord.Embed:
        e = discord.Embed(title="🏛️ Congress Trades", color=E.BLUE, description=(
            "What members of the House and Senate disclose buying and selling. Trades are reported up to 45 days "
            "after they happen. `/congress member:` for anyone's record, `/congress ticker:` for who traded a stock."))
        latest = sorted(self.filings.values(), key=lambda f: (f["filed"], f.get("seen", 0)), reverse=True)[:8]
        if latest:
            lines = []
            for f in latest:
                trades = [t for t in self.trades if t["filing"] == f["id"]]
                tick = Counter(t["ticker"] for t in trades if t.get("ticker")).most_common(3)
                what = ("paper filing" if f.get("paper") else f"{len(trades)} trade{'s' if len(trades) != 1 else ''}"
                        + (f": {', '.join(k for k, _ in tick)}" if tick else ""))
                lines.append(f"{PARTY.get(f.get('party') or '', '⚪')} [{clip(who(f), 48)}]({f['url']}) · {what} · "
                             f"{f['filed'][5:].replace('-', '/')}")
            e.add_field(name="🆕 Latest filings", value=clip("\n".join(lines), 1024), inline=False)
        board = self.leaderboard()
        if board:
            e.add_field(name="🏆 Best stock pickers (buys over 12 months, est.)", value=clip("\n".join(
                f"`{i + 1}.` {PARTY.get(self.party_of(m), '⚪')} **{clip(m, 28)}** {st['excess']:+.1%} vs S&P · "
                f"{st['scored']} buys, {st['hit']:.0%} up" for i, (m, st) in enumerate(board[:5])), 1024), inline=False)
        buys, sells = self.flows()
        if buys:
            e.add_field(name="🟢 Most bought (30 days)", value=" · ".join(f"**{k}** ×{n}" for k, n in buys), inline=False)
        if sells:
            e.add_field(name="🔴 Most sold (30 days)", value=" · ".join(f"**{k}** ×{n}" for k, n in sells), inline=False)
        if not e.fields:
            e.add_field(name="Filling in", value="Reading this year's disclosures; the board fills in within a few "
                                                 "hours.", inline=False)
        e.set_footer(text=FOOTER)
        return fit_embed(e)

    def party_of(self, member: str) -> str:
        return next((f.get("party") or "" for f in self.filings.values() if f["member"] == member), "")

    async def job_board(self) -> None:
        channels = self.bot.channels.of_kind(KIND)
        if not channels:
            return
        embed = self.board()
        for cid, _ in channels:
            await self.bot.show_board(cid, embed)

    def member_embed(self, member: str) -> discord.Embed:
        trades = sorted(self.by_member().get(member, []), key=lambda t: t["tx_date"], reverse=True)
        f = next((f for f in self.filings.values() if f["member"] == member), {"member": member})
        e = discord.Embed(title=f"🏛️ {who(f)}", color=E.BLUE)
        year = member_stats(trades, self.scores, (date.today() - timedelta(days=365)).isoformat())
        lines = [f"**{year['trades']}** trades in 12 months ({year['buys']} buys, {year['sells']} sells), at least "
                 f"**{money(year['volume_low'])}** in total"]
        if "ret" in year:
            lines.append(f"Buys since bought: **{year['ret']:+.1%}** on average (amount-weighted)"
                         + (f", **{year['excess']:+.1%}** vs the S&P 500" if "excess" in year else "")
                         + f" · {year['hit']:.0%} are up · {year['scored']} priced")
        if year["tickers"]:
            lines.append("Most traded: " + ", ".join(f"**{k}** ×{n}" for k, n in year["tickers"]))
        e.description = "\n".join(lines)
        rows = []
        for t in trades[:12]:
            s = self.scores.get(tkey(t))
            since = f" · {s['ret']:+.0%} since" if s and t["kind"] == "purchase" else ""
            rows.append(f"{'🟢' if t['kind'] == 'purchase' else '🔴' if t['kind'].startswith('sale') else '🔁'} "
                        f"{C.KIND_WORDS[t['kind']]} **{t.get('ticker') or clip(t['asset'], 30)}** {band(t)} · "
                        f"{t['tx_date'][5:].replace('-', '/')}/{t['tx_date'][2:4]}"
                        + (f" ({t['owner']})" if t["owner"] != "Self" else "") + since)
        if rows:
            e.add_field(name="Latest trades", value=clip("\n".join(rows), 1024), inline=False)
        e.set_footer(text=FOOTER)
        return fit_embed(e)

    def ticker_embed(self, ticker: str) -> discord.Embed:
        since = (date.today() - timedelta(days=365)).isoformat()
        trades = sorted((t for t in self.trades if t.get("ticker") == ticker and t["tx_date"] >= since),
                        key=lambda t: t["tx_date"], reverse=True)
        e = discord.Embed(title=f"🏛️ Congress trades in {ticker} (12 months)", color=E.BLUE)
        if not trades:
            e.description = "No member of Congress disclosed a trade in it in the last 12 months."
        else:
            buyers = {t["member"] for t in trades if t["kind"] == "purchase"}
            sellers = {t["member"] for t in trades if t["kind"].startswith("sale")}
            e.description = f"**{len(trades)}** trades by **{len({t['member'] for t in trades})}** members · " \
                            f"{len(buyers)} bought, {len(sellers)} sold"
            rows = []
            for t in trades[:15]:
                s = self.scores.get(tkey(t))
                rows.append(f"{'🟢' if t['kind'] == 'purchase' else '🔴' if t['kind'].startswith('sale') else '🔁'} "
                            f"{PARTY.get(t.get('party') or '', '⚪')} {clip(t['member'], 26)} · "
                            f"{C.KIND_WORDS[t['kind']].lower()} {band(t)} · {t['tx_date']}"
                            + (f" · {s['ret']:+.0%} since" if s and t["kind"] == "purchase" else ""))
            e.add_field(name="Trades", value=clip("\n".join(rows), 1024), inline=False)
        e.set_footer(text=FOOTER)
        return fit_embed(e)

    def members_matching(self, text: str) -> list[str]:
        q = text.lower().strip()
        names = sorted({t["member"] for t in self.trades} | {f["member"] for f in self.filings.values()})
        return [n for n in names if q in n.lower()] if q else names

    def register(self, tree) -> None:
        @tree.command(name="congress", description="Congress members' trades: the latest, someone's record, or a ticker")
        @app_commands.describe(member="A member's name, e.g. Pelosi", ticker="A ticker, e.g. NVDA")
        async def congress_cmd(interaction: discord.Interaction, member: str | None = None, ticker: str | None = None):
            if member:
                found = self.members_matching(member)
                exact = [n for n in found if n.lower() == member.lower().strip()]
                if not found:
                    await interaction.response.send_message(
                        f"No disclosed trades for **{clip(member, 40)}** yet (this year and last).", ephemeral=True)
                    return
                await interaction.response.send_message(embed=self.member_embed((exact or found)[0]))
            elif ticker:
                t = C.yahoo_ticker(ticker.replace("$", ""))
                if not t:
                    await interaction.response.send_message("That doesn't look like a ticker.", ephemeral=True)
                    return
                await interaction.response.send_message(embed=self.ticker_embed(t))
            else:
                await interaction.response.send_message(embed=self.board())

        @congress_cmd.autocomplete("member")
        async def member_auto(interaction: discord.Interaction, current: str):
            return [app_commands.Choice(name=n[:100], value=n[:100]) for n in self.members_matching(current)[:25]]


def filing_embed(f: dict, trades: list[dict], scores: dict) -> discord.Embed:
    n = len(trades)
    e = discord.Embed(title=f"🏛️ {who(f)} disclosed "
                            + ("a paper filing" if f.get("paper") else f"{n} trade{'s' if n != 1 else ''}"),
                      url=f.get("url") or None, color=E.BLUE)
    if f.get("paper"):
        e.description = "A scanned paper filing: its trades aren't machine-readable. Open it above."
    lines, flags = [], set()
    for t in sorted(trades, key=lambda t: -(t["amount_low"] or 0))[:12]:
        lag = (date.fromisoformat(f["filed"]) - date.fromisoformat(t["tx_date"])).days if f.get("filed") else 0
        if lag > LATE_DAYS:
            flags.add(f"⏰ filed {lag} days after the trade (the deadline is {LATE_DAYS})")
        if (t["amount_low"] or 0) >= 1_000_001:
            flags.add("💰 includes trades over $1M")
        if t["asset_type"] == "option":
            flags.add("🎯 includes options")
        lines.append(f"{'🟢' if t['kind'] == 'purchase' else '🔴' if t['kind'].startswith('sale') else '🔁'} "
                     f"**{C.KIND_WORDS[t['kind']]}** {t.get('ticker') or clip(t['asset'], 40)}"
                     + (" (option)" if t["asset_type"] == "option" else "")
                     + f" · {band(t)} · {t['tx_date'][5:].replace('-', '/')}"
                     + (f" · {t['owner']}" if t["owner"] != "Self" else ""))
    if n > 12:
        lines.append(f"…and {n - 12} more")
    if lines:
        e.description = "\n".join(lines)
    if flags:
        e.add_field(name="Worth noting", value="\n".join(sorted(flags)), inline=False)
    e.set_footer(text=FOOTER)
    e.timestamp = datetime.now(timezone.utc)
    return fit_embed(e)
