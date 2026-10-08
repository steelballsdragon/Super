"""Insiders and big funds, from SEC filings, posted to the 🏛️ Smart money channel.

- Insider buys: executives and directors must file a Form 4 within two business days of trading their company's
  stock. Open-market purchases are rare and are the signal worth seeing (sales are mostly planned or for taxes),
  so every purchase of $100K+ is posted, flagged when several insiders buy the same stock within two weeks; only
  very large unplanned sales are posted.
- Funds: famous managers file their holdings (13F) 45 days after each quarter; a new filing gets a post with what
  they bought, sold, started and dropped. /fund shows any of them; /insiders shows a company's recent insider trades.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

import discord
from discord import app_commands

from .. import embeds as E
from ..apis.finnhub import Finnhub
from ..apis.sec import FUNDS, SEC, Form4, Holding
from ..briefs import Post
from ..hours import NEW_YORK
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from ..universe import short
from . import Feature

log = logging.getLogger(__name__)

KIND = "congress"  # the Smart money channel
MIN_BUY = 100_000
MIN_SALE = 10_000_000
CLUSTER_DAYS = 14
PER_RUN = 40
INACTIVE_DAYS = 200  # a fund with no 13F for this long has stopped filing


def money(v: float) -> str:
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            x = v / div
            return f"${x:.0f}{unit}" if abs(x) >= 10 else f"${x:.1f}".rstrip("0").rstrip(".") + unit
    return f"${v:,.0f}"


def notable(f: Form4) -> tuple[str, list] | None:
    """("buy" | "sale", trades) when the filing is worth a post."""
    insider = any(o.officer or o.director for o in f.owners)
    buys = [t for t in f.trades if t.code == "P" and t.acquired]
    if buys and sum(t.value for t in buys) >= MIN_BUY and (insider or any(o.ten_percent for o in f.owners)):
        return "buy", buys
    sales = [t for t in f.trades if t.code == "S" and not t.acquired and not t.planned]
    if sales and insider and sum(t.value for t in sales) >= MIN_SALE:
        return "sale", sales
    return None


def insider_embed(f: Form4, kind: str, trades: list, cluster: list[str]) -> discord.Embed:
    total = sum(t.value for t in trades)
    shares = sum(t.shares for t in trades)
    who = f.owners[0] if f.owners else None
    name = (who.name.title() if who else "An insider") + (f" ({who.role})" if who else "")
    sym = f.symbol or "?"
    verb = "bought" if kind == "buy" else "sold"
    e = discord.Embed(title=f"{'🟢' if kind == 'buy' else '🔴'} {'Cluster buy: ' if cluster else ''}{sym} insider {verb} "
                            f"{money(total)}", url=f.url or None, color=E.GREEN if kind == "buy" else E.RED)
    avg = total / shares if shares else 0
    lines = [f"**{clip(name, 80)}** at **{clip(f.issuer, 60)}** {verb} **{shares:,.0f}** shares at about "
             f"**${avg:,.2f}** ({money(total)})"]
    after = trades[-1].owned_after
    if after:
        before = after - shares if kind == "buy" else after + shares
        if before > 0:
            lines.append(f"Now owns {after:,.0f} shares ({(after / before - 1):+.0%} vs before)")
    days = sorted({t.day for t in trades})
    if days:
        lines.append(f"Traded {days[0]}" + (f" to {days[-1]}" if days[-1] != days[0] else ""))
    if cluster:
        lines.append(f"🔁 Also buying in the last {CLUSTER_DAYS} days: {', '.join(clip(c, 40) for c in cluster[:5])}")
    if kind == "sale":
        lines.append("Not under a pre-set (10b5-1) trading plan")
    e.description = "\n".join(lines)
    e.set_footer(text="SEC Form 4 · insiders report within 2 business days · open-market trades only · "
                      "not financial advice")
    return fit_embed(e)


def fund_changes(now: list[Holding], before: list[Holding], tickers: dict[str, str]) -> dict:
    """Started, dropped, added to and trimmed positions (shares, ignoring put/call rows), and the top holdings."""
    def key(h):
        return h.cusip

    stock_now = {key(h): h for h in now if not h.put_call}
    stock_before = {key(h): h for h in before if not h.put_call}
    total = sum(h.value for h in now if not h.put_call) or 1.0
    label = lambda h: tickers.get(h.cusip) or h.name.title()  # noqa: E731
    new = sorted((h for k, h in stock_now.items() if k not in stock_before), key=lambda h: -h.value)
    gone = sorted((h for k, h in stock_before.items() if k not in stock_now), key=lambda h: -h.value)
    changed = []
    for k, h in stock_now.items():
        b = stock_before.get(k)
        if b and b.shares > 0 and abs(h.shares / b.shares - 1) >= 0.05:
            changed.append((h, h.shares / b.shares - 1))
    adds = sorted((c for c in changed if c[1] > 0), key=lambda c: -c[0].value)
    trims = sorted((c for c in changed if c[1] < 0), key=lambda c: -c[0].value)
    top = sorted(stock_now.values(), key=lambda h: -h.value)[:10]
    return {"total": total, "count": len(stock_now),
            "top": [(label(h), h.value / total) for h in top],
            "new": [(label(h), h.value) for h in new[:8]], "gone": [label(h) for h in gone[:8]],
            "adds": [(label(h), pct, h.value / total) for h, pct in adds[:8]],
            "trims": [(label(h), pct, h.value / total) for h, pct in trims[:8]]}


def fund_embed(name: str, filing: dict, ch: dict, previous: dict | None) -> discord.Embed:
    e = discord.Embed(title=f"🏦 {name}: holdings at {filing.get('period', '?')}", color=E.BLUE,
                      description=f"Filed {filing.get('filed', '?')} · **{ch['count']}** US stock positions worth "
                                  f"**{money(ch['total'])}**" + ("" if previous else " · first filing on record"))
    if ch["top"]:
        e.add_field(name="Top holdings", value="\n".join(f"`{i + 1:>2}.` **{clip(n, 28)}** {w:.1%}"
                                                         for i, (n, w) in enumerate(ch["top"])), inline=False)
    if previous:
        if ch["new"]:
            e.add_field(name="🆕 New positions", value=", ".join(f"**{clip(n, 24)}** ({money(v)})" for n, v in ch["new"]),
                        inline=False)
        if ch["adds"]:
            e.add_field(name="➕ Added to", value=", ".join(f"**{clip(n, 24)}** {p:+.0%}" for n, p, _ in ch["adds"]),
                        inline=False)
        if ch["trims"]:
            e.add_field(name="➖ Trimmed", value=", ".join(f"**{clip(n, 24)}** {p:+.0%}" for n, p, _ in ch["trims"]),
                        inline=False)
        if ch["gone"]:
            e.add_field(name="❌ Sold out of", value=", ".join(f"**{clip(n, 24)}**" for n in ch["gone"]), inline=False)
    e.set_footer(text="SEC 13F · US-listed stocks only, as of the quarter's end, filed up to 45 days later · "
                      "options rows left out · not financial advice")
    return fit_embed(e)


class InsiderDesk(Feature):
    name = "insiders"
    help_group = "🏛️ Smart money"

    def __init__(self, bot):
        super().__init__(bot)
        http = getattr(bot.engine.data, "http", None)
        if http is None:
            raise RuntimeError("no shared Http")
        self.sec = SEC(http, bot.data_dir)
        self.finnhub = Finnhub(http, state_file=bot.data_dir / "apis.json")
        self.path = bot.data_dir / "insiders.json"
        data = read_json(self.path, {})
        self.seen: dict[str, float] = data.get("seen") or {}
        self.buys: list[dict] = data.get("buys") or []  # {symbol, owner, at}
        self.funds: dict[str, str] = data.get("funds") or {}  # CIK -> latest 13F accession seen
        self.started = bool(data)

    def jobs(self):
        return [("insiders", 300, self.job_form4), ("funds", 6 * 3600, self.job_funds)]

    def help(self):
        return [("insiders", "a company's recent insider buys and sells"),
                ("fund", "what a famous fund holds and changed last quarter (Buffett, Burry, Ackman…)")]

    def status(self):
        return [f"**SEC EDGAR** {self.sec.api.status_line()} · {len(self.seen):,} insider filings checked"]

    def save(self) -> None:
        cutoff = time.time() - 14 * 86400
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        self.buys = [b for b in self.buys if b["at"] >= time.time() - 60 * 86400]
        write_json(self.path, {"seen": self.seen, "buys": self.buys, "funds": self.funds})

    def channels(self):
        return [(cid, cfg) for cid, cfg in self.bot.channels.of_kind(KIND) if cfg.alerts]

    async def job_form4(self) -> None:
        if not self.bot.channels.of_kind(KIND):
            return
        ny = datetime.now(NEW_YORK)
        if ny.weekday() >= 5 and self.started:
            return  # EDGAR doesn't publish at weekends
        entries: list[dict] = []
        for start in (0, 100, 200):
            page = await self.sec.feed(start)
            entries += page
            if not page or any(e["accession"] in self.seen for e in page):
                break
        by_acc: dict[str, dict] = {}
        for e in entries:
            if e["accession"] not in self.seen:
                if e["role"] == "Issuer" or e["accession"] not in by_acc:
                    by_acc[e["accession"]] = e
        if not self.started:  # first run: today's backlog is history, not news
            for acc in by_acc:
                self.seen[acc] = time.time()
            self.started = True
            self.save()
            return
        for acc, entry in list(by_acc.items())[:PER_RUN]:
            self.seen[acc] = time.time()
            try:
                f = await self.sec.form4(entry)
            except Exception as exc:
                log.info("Form 4 %s unreadable: %s", acc, exc)
                continue
            if f is None:
                continue
            hit = notable(f)
            if hit is None:
                continue
            kind, trades = hit
            cluster = []
            if kind == "buy":
                owner = f.owners[0].name if f.owners else "?"
                cluster = sorted({b["owner"].title() for b in self.buys if b["symbol"] == f.symbol and
                                  b["owner"] != owner and b["at"] >= time.time() - CLUSTER_DAYS * 86400})
                self.buys.append({"symbol": f.symbol, "owner": owner, "at": time.time()})
            embed = insider_embed(f, kind, trades, cluster)
            for cid, _ in self.channels():
                await self.bot.send(cid, Post([embed]))
        self.save()

    async def fund_view(self, name: str, cik: int) -> tuple[dict, dict, dict | None] | None:
        filings = await self.sec.latest_13f(cik, 2)
        if not filings:
            return None
        now = await self.sec.holdings(cik, filings[0])
        before = await self.sec.holdings(cik, filings[1]) if len(filings) > 1 else []
        tickers = await self.sec.tickers_for([h for h in now + before if not h.put_call][:120])
        return filings[0], fund_changes(now, before, tickers), (filings[1] if len(filings) > 1 else None)

    async def job_funds(self) -> None:
        first = not self.funds
        for name, cik in FUNDS.items():
            try:
                filings = await self.sec.latest_13f(cik, 1)
            except Exception as exc:
                log.info("13F list for %s unavailable: %s", name, exc)
                continue
            if not filings or self.funds.get(str(cik)) == filings[0]["accession"]:
                continue
            self.funds[str(cik)] = filings[0]["accession"]
            if first or not self.channels():
                continue
            try:
                view = await self.fund_view(name, cik)
            except Exception:
                log.warning("13F for %s couldn't be read", name, exc_info=True)
                continue
            if view:
                embed = fund_embed(name, *view)
                for cid, _ in self.channels():
                    await self.bot.send(cid, Post([embed]))
            await asyncio.sleep(1)
        self.save()

    async def insider_rows(self, symbol: str) -> list[dict]:
        """Recent open-market insider trades: Finnhub's list when there's a key, else none (SEC lookups per
        company are slower)."""
        if not self.finnhub.enabled:
            return []
        rows = await self.finnhub.insider_transactions(short(symbol), days=180)
        out = []
        for r in rows:
            code = str(r.get("transactionCode") or "")
            price = r.get("transactionPrice") or 0
            change = r.get("change") or 0
            if code not in ("P", "S") or not price:
                continue
            out.append({"name": str(r.get("name") or "?"), "code": code, "day": str(r.get("transactionDate") or ""),
                        "shares": abs(float(change)), "value": abs(float(change)) * float(price)})
        return sorted(out, key=lambda r: r["day"], reverse=True)

    def register(self, tree) -> None:
        bot = self.bot

        @tree.command(name="insiders", description="A company's recent insider buys and sells (SEC Form 4)")
        @app_commands.describe(symbol="Ticker or name, e.g. NVDA")
        async def insiders_cmd(interaction: discord.Interaction, symbol: str):
            await interaction.response.defer(thinking=True)
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return
            if not self.finnhub.enabled:
                await interaction.followup.send("This needs a free Finnhub key: set FINNHUB_API_KEY in the bot's "
                                                "settings. Big insider buys are posted in the Smart money channel "
                                                "either way.")
                return
            rows = await self.insider_rows(r.symbol)
            e = discord.Embed(title=f"👔 Insider trades in {short(r.symbol)} (6 months)", color=E.BLUE)
            if not rows:
                e.description = "No open-market insider buys or sells in the last 6 months."
            else:
                buys = [x for x in rows if x["code"] == "P"]
                sells = [x for x in rows if x["code"] == "S"]
                e.description = (f"**{len(buys)}** buys ({money(sum(x['value'] for x in buys))}) · **{len(sells)}** "
                                 f"sells ({money(sum(x['value'] for x in sells))})")
                e.add_field(name="Latest", value=clip("\n".join(
                    f"{'🟢' if x['code'] == 'P' else '🔴'} `{x['day']}` **{clip(x['name'].title(), 30)}** "
                    f"{'bought' if x['code'] == 'P' else 'sold'} {x['shares']:,.0f} ({money(x['value'])})"
                    for x in rows[:15]), 1024), inline=False)
            e.set_footer(text="Form 4 data via Finnhub · open-market trades only (code P/S) · sales are often "
                              "pre-planned or for taxes · not financial advice")
            await interaction.followup.send(embed=fit_embed(e))

        suggest = getattr(bot, "symbol_suggestions", None)
        if suggest is not None:
            insiders_cmd.autocomplete("symbol")(suggest)

        @tree.command(name="fund", description="What a famous fund holds and changed last quarter (SEC 13F)")
        @app_commands.describe(name="The fund")
        @app_commands.choices(name=[app_commands.Choice(name=n, value=n) for n in list(FUNDS)[:25]])
        async def fund_cmd(interaction: discord.Interaction, name: app_commands.Choice[str]):
            await interaction.response.defer(thinking=True)
            cik = FUNDS[name.value]
            view = await self.fund_view(name.value, cik)
            if view is None:
                await interaction.followup.send(f"No 13F filings found for {name.value}.")
                return
            embed = fund_embed(name.value, *view)
            filed = view[0].get("filed") or ""
            if filed and filed < (date.today() - timedelta(days=INACTIVE_DAYS)).isoformat():
                embed.description = (embed.description or "") + f"\n⚠️ No 13F since {filed}: it has stopped filing them."
            await interaction.followup.send(embed=embed)
