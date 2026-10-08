"""Your real portfolio, privately: `/portfolio add`, `remove`, `show`, `stress` and `memo`. Replies are only visible
to you (and work in DMs with the bot).

- show: value, today's change, gain or loss against what you paid, each holding's weight, and the portfolio's beta
  (how much it tends to move when the S&P 500 moves 1%).
- stress: what the S&P 500 falling 10%, 20% or 35% would likely do to it (each holding's beta), and what the 2008
  crisis, the 2020 crash and the 2022 bear market actually did to these holdings (estimated from beta for those
  that weren't trading then).
- memo: a private weekly note by DM after Friday's close: the week's change, best and worst holdings, and the
  market memo's summary.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import discord
import numpy as np
from discord import app_commands

from .. import embeds as E
from ..hours import NEW_YORK, is_trading_day, next_trading_day
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from ..universe import short
from . import Feature

log = logging.getLogger(__name__)

BENCH = "^GSPC"
MAX_HOLDINGS = 40
SHOCKS = (-0.10, -0.20, -0.35)
EPISODES = (("2008 financial crisis", "2007-10-09", "2009-03-09"),
            ("2020 Covid crash", "2020-02-19", "2020-03-23"),
            ("2022 bear market", "2022-01-03", "2022-10-12"))


def money(v: float) -> str:
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def day_ts(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


def close_on(bars, day: str) -> float | None:
    """The close on that day, or the last one before it; None when the history starts later."""
    i = int(np.searchsorted(bars.t, day_ts(day) + 86400, side="left")) - 1
    if i < 0 or day_ts(day) - bars.t[i] > 7 * 86400:  # before the history starts, or long after it stopped
        return None
    return float(bars.close[i])


def daily_returns(bars) -> dict[str, float]:
    days = [datetime.fromtimestamp(t, timezone.utc).date().isoformat() for t in bars.t]
    c = bars.close
    return {days[i]: c[i] / c[i - 1] - 1 for i in range(max(1, len(c) - 260), len(c)) if c[i - 1] > 0}


def beta(bars, bench) -> float | None:
    """Slope of the holding's daily returns on the S&P 500's over about a year (dates both traded)."""
    a, b = daily_returns(bars), daily_returns(bench)
    common = sorted(set(a) & set(b))[-250:]
    if len(common) < 60:
        return None
    x = np.array([b[d] for d in common])
    y = np.array([a[d] for d in common])
    var = np.var(x)
    return float(np.cov(x, y, bias=True)[0, 1] / var) if var > 0 else None


def add(holdings: dict, symbol: str, shares: float, price: float) -> None:
    h = holdings.get(symbol)
    if h:
        h["cost"] += shares * price
        h["shares"] += shares
    else:
        holdings[symbol] = {"shares": shares, "cost": shares * price}


def remove(holdings: dict, symbol: str, shares: float | None) -> bool:
    h = holdings.get(symbol)
    if not h:
        return False
    if shares is None or shares >= h["shares"] - 1e-9:
        del holdings[symbol]
    else:
        h["cost"] *= 1 - shares / h["shares"]
        h["shares"] -= shares
    return True


def snapshot(holdings: dict, quotes: dict) -> dict:
    rows, total, day, cost = [], 0.0, 0.0, 0.0
    for sym, h in holdings.items():
        q = quotes.get(sym)
        price = q.price if q else h["cost"] / h["shares"]
        worth = h["shares"] * price
        prev = worth / (1 + q.change_pct / 100) if q and q.change_pct is not None else worth
        rows.append({"symbol": sym, "shares": h["shares"], "value": worth, "cost": h["cost"],
                     "day": (q.change_pct if q else None), "live": q is not None})
        total += worth
        day += worth - prev
        cost += h["cost"]
    for r in rows:
        r["weight"] = r["value"] / total if total else 0
    rows.sort(key=lambda r: -r["value"])
    return {"rows": rows, "total": total, "day": day, "cost": cost}


def show_embed(name: str, snap: dict, port_beta: float | None) -> discord.Embed:
    total, cost = snap["total"], snap["cost"]
    gain = total - cost
    e = discord.Embed(title=f"💼 {clip(name, 40)}'s portfolio", color=E.GREEN if snap["day"] >= 0 else E.RED)
    prev = total - snap["day"]
    lines = [f"Worth **{money(total)}** · today **{money(snap['day'])}** "
             f"({snap['day'] / prev if prev else 0:+.2%})",
             f"Gain since bought **{money(gain)}** ({gain / cost if cost else 0:+.1%})"]
    if port_beta is not None:
        lines.append(f"Beta **{port_beta:.2f}**: tends to move {port_beta:.1f}% when the S&P 500 moves 1%")
    top = snap["rows"][0] if snap["rows"] else None
    if top and top["weight"] >= 0.25 and len(snap["rows"]) > 1:
        lines.append(f"⚠️ {short(top['symbol'])} is {top['weight']:.0%} of it")
    e.description = "\n".join(lines)
    rows = [f"**{short(r['symbol'])}** {money(r['value'])} · {r['weight']:.0%} · "
            + (f"{r['day']:+.1f}% today · " if r["day"] is not None else "")
            + f"{(r['value'] / r['cost'] - 1) if r['cost'] else 0:+.1%} total" + ("" if r["live"] else " (no price)")
            for r in snap["rows"][:25]]
    if rows:
        e.add_field(name="Holdings", value=clip("\n".join(rows), 1024), inline=False)
    e.set_footer(text="Only you see this · prices from the bot's live sources · not financial advice")
    return fit_embed(e)


def stress(snap: dict, betas: dict[str, float | None], bars: dict, bench) -> dict:
    """{"shocks": [(S&P move, portfolio $, %)], "episodes": [(name, S&P %, portfolio %, estimated count)]}."""
    total = snap["total"] or 1.0
    weights = {r["symbol"]: r["value"] / total for r in snap["rows"]}
    b = {s: (betas.get(s) if betas.get(s) is not None else 1.0) for s in weights}
    shocks = []
    for move in SHOCKS:
        pct = sum(w * max(-0.95, b[s] * move) for s, w in weights.items())
        shocks.append((move, pct * total, pct))
    episodes = []
    for name, start, end in EPISODES:
        s0, s1 = close_on(bench, start), close_on(bench, end)
        if not s0 or not s1:
            continue
        spx = s1 / s0 - 1
        pct, guessed = 0.0, 0
        for s, w in weights.items():
            hb = bars.get(s)
            p0 = close_on(hb, start) if hb is not None else None
            p1 = close_on(hb, end) if hb is not None else None
            if p0 and p1:
                pct += w * (p1 / p0 - 1)
            else:
                pct += w * max(-0.95, b[s] * spx)
                guessed += 1
        episodes.append((name, spx, pct, guessed))
    return {"shocks": shocks, "episodes": episodes}


def stress_embed(snap: dict, result: dict, port_beta: float | None) -> discord.Embed:
    e = discord.Embed(title="🧯 Portfolio stress test", color=E.PURPLE,
                      description=f"Worth {money(snap['total'])}" +
                                  (f" · beta {port_beta:.2f}" if port_beta is not None else ""))
    e.add_field(name="If the S&P 500 fell…", value="\n".join(
        f"**{m:.0%}** → about **{money(d)}** ({p:+.1%})" for m, d, p in result["shocks"]), inline=False)
    if result["episodes"]:
        e.add_field(name="Replaying real sell-offs", value="\n".join(
            f"**{n}**: S&P {spx:+.0%} → yours **{p:+.0%}** ({money(p * snap['total'])})"
            + (f" · {g} estimated from beta" if g else "") for n, spx, p, g in result["episodes"]), inline=False)
    e.set_footer(text="Estimates from each holding's last year of moves vs the S&P 500 · real crashes differ · "
                      "not financial advice")
    return fit_embed(e)


def week_change(holdings: dict, quotes: dict, bars: dict, now: float) -> tuple[float, list[tuple[str, float]]]:
    """The portfolio's change over the last 7 days ($) and each holding's % change."""
    then_day = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).date().isoformat()
    moves, change = [], 0.0
    for sym, h in holdings.items():
        q, hb = quotes.get(sym), bars.get(sym)
        then = close_on(hb, then_day) if hb is not None else None
        if not q or not then:
            continue
        moves.append((sym, q.price / then - 1))
        change += h["shares"] * (q.price - then)
    return change, sorted(moves, key=lambda m: -m[1])


class PortfolioDesk(Feature):
    name = "portfolio"
    help_group = "💼 Your portfolio"

    def __init__(self, bot):
        super().__init__(bot)
        self.path = bot.data_dir / "portfolios.json"
        self.users: dict[str, dict] = read_json(self.path, {}).get("users") or {}

    def jobs(self):
        return [("portfolio_memo", 600, self.job_memo)]

    def help(self):
        return [("portfolio add", "add a holding (shares and what you paid); only you see your portfolio"),
                ("portfolio show", "value, today's change, gain and each holding's weight"),
                ("portfolio stress", "what a 10-35% drop, 2008, 2020 or 2022 would do to it"),
                ("portfolio memo", "a private weekly note by DM after Friday's close")]

    def status(self):
        return [f"**Portfolios** {len(self.users)} · {sum(bool(u.get('memo')) for u in self.users.values())} "
                "get the weekly DM"]

    def save(self) -> None:
        write_json(self.path, {"users": self.users})

    def user(self, uid: int) -> dict:
        return self.users.setdefault(str(uid), {"holdings": {}, "memo": False})

    async def quotes(self, symbols) -> dict:
        symbols = list(dict.fromkeys(symbols))
        return await self.bot.engine.data.quotes(symbols) if symbols else {}

    async def histories(self, symbols) -> dict:
        out = {}
        for s in dict.fromkeys(symbols):
            try:
                out[s] = await self.bot.engine.cache.daily(s)
            except Exception as exc:
                log.info("Portfolio history for %s unavailable: %s", s, exc)
        return out

    async def analysis(self, holdings: dict):
        quotes = await self.quotes(list(holdings))
        snap = snapshot(holdings, quotes)
        bars = await self.histories(list(holdings) + [BENCH])
        bench = bars.get(BENCH)
        betas = {s: beta(bars[s], bench) if s in bars and bench is not None else None for s in holdings}
        known = [(r["weight"], betas.get(r["symbol"])) for r in snap["rows"]]
        port_beta = sum(w * b for w, b in known if b is not None) if any(b is not None for _, b in known) else None
        return snap, bars, bench, betas, port_beta

    async def job_memo(self) -> None:
        ny = datetime.now(NEW_YORK)
        d = ny.date()
        if not (is_trading_day(d) and next_trading_day(d).isocalendar()[1] != d.isocalendar()[1]):
            return
        if (ny.hour, ny.minute) < (17, 0):
            return
        week = d.isoformat()
        memo = next((f for f in getattr(self.bot, "features", []) if f.name == "memo"), None)
        for uid, u in list(self.users.items()):
            if not u.get("memo") or not u["holdings"] or u.get("memo_sent") == week:
                continue
            u["memo_sent"] = week
            self.save()
            try:
                embed = await self.memo_embed(u, getattr(memo, "latest", None))
                user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                await user.send(embed=embed)
            except Exception:
                log.warning("Couldn't send the weekly portfolio memo to %s", uid, exc_info=True)

    async def memo_embed(self, u: dict, market: dict | None) -> discord.Embed:
        holdings = u["holdings"]
        quotes = await self.quotes(holdings)
        bars = await self.histories(holdings)
        snap = snapshot(holdings, quotes)
        change, moves = week_change(holdings, quotes, bars, time.time())
        before = snap["total"] - change
        e = discord.Embed(title=f"📬 Your week · {date.today():%b %-d}", color=E.GREEN if change >= 0 else E.RED,
                          description=f"Your portfolio: **{money(snap['total'])}**, **{money(change)}** this week "
                                      f"({change / before if before else 0:+.2%})")
        if moves:
            e.add_field(name="Best", value=" · ".join(f"**{short(s)}** {m:+.1%}" for s, m in moves[:3]), inline=True)
            e.add_field(name="Worst", value=" · ".join(f"**{short(s)}** {m:+.1%}" for s, m in moves[::-1][:3]),
                        inline=True)
        if market and market.get("summary"):
            e.add_field(name="The market this week", value=clip(market["summary"], 1024), inline=False)
        e.set_footer(text="Your private weekly memo · /portfolio memo to turn it off · not financial advice")
        return fit_embed(e)

    def register(self, tree) -> None:
        bot = self.bot
        group = app_commands.Group(name="portfolio", description="Your real portfolio, privately")

        async def private(interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)

        @group.command(name="add", description="Add shares or coins you own (only you see your portfolio)")
        @app_commands.describe(symbol="Ticker or name", shares="How many shares or coins",
                               price="What you paid for each (today's price if empty)")
        async def add_cmd(interaction: discord.Interaction, symbol: str,
                          shares: app_commands.Range[float, 0.000001, None],
                          price: app_commands.Range[float, 0.0, None] | None = None):
            await private(interaction)
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return
            u = self.user(interaction.user.id)
            if r.symbol not in u["holdings"] and len(u["holdings"]) >= MAX_HOLDINGS:
                await interaction.followup.send(f"That's {MAX_HOLDINGS} holdings already.", ephemeral=True)
                return
            if not price:
                q = (await self.quotes([r.symbol])).get(r.symbol)
                if q is None:
                    await interaction.followup.send("I couldn't get today's price; add it with `price:`.",
                                                    ephemeral=True)
                    return
                price = q.price
            add(u["holdings"], r.symbol, float(shares), float(price))
            self.save()
            h = u["holdings"][r.symbol]
            await interaction.followup.send(f"✅ {short(r.symbol)}: you now hold {h['shares']:,.6g} at an average "
                                            f"${h['cost'] / h['shares']:,.2f}.", ephemeral=True)

        @group.command(name="remove", description="Remove some or all of a holding")
        @app_commands.describe(symbol="Ticker or name", shares="How many (all if empty)")
        async def remove_cmd(interaction: discord.Interaction, symbol: str,
                             shares: app_commands.Range[float, 0.000001, None] | None = None):
            await private(interaction)
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return
            ok = remove(self.user(interaction.user.id)["holdings"], r.symbol, shares)
            if ok:
                self.save()
            await interaction.followup.send(f"Removed {short(r.symbol)}." if ok else
                                            f"You don't have {short(r.symbol)} in your portfolio.", ephemeral=True)

        @group.command(name="show", description="Your portfolio: value, today's change, gains and weights")
        async def show_cmd(interaction: discord.Interaction):
            await private(interaction)
            u = self.user(interaction.user.id)
            if not u["holdings"]:
                await interaction.followup.send("It's empty: `/portfolio add` to add what you own.", ephemeral=True)
                return
            snap, _, _, _, port_beta = await self.analysis(u["holdings"])
            await interaction.followup.send(embed=show_embed(interaction.user.display_name, snap, port_beta),
                                            ephemeral=True)

        @group.command(name="stress", description="What a crash, or 2008, 2020 or 2022, would do to your portfolio")
        async def stress_cmd(interaction: discord.Interaction):
            await private(interaction)
            u = self.user(interaction.user.id)
            if not u["holdings"]:
                await interaction.followup.send("It's empty: `/portfolio add` to add what you own.", ephemeral=True)
                return
            snap, bars, bench, betas, port_beta = await self.analysis(u["holdings"])
            if bench is None:
                await interaction.followup.send("I couldn't load the S&P 500's history right now; try again soon.",
                                                ephemeral=True)
                return
            await interaction.followup.send(embed=stress_embed(snap, stress(snap, betas, bars, bench), port_beta),
                                            ephemeral=True)

        @group.command(name="memo", description="Turn your private weekly portfolio memo by DM on or off")
        async def memo_cmd(interaction: discord.Interaction, on: bool):
            u = self.user(interaction.user.id)
            u["memo"] = on
            self.save()
            await interaction.response.send_message(
                "📬 You'll get a private memo by DM after each week's last close." if on else "Weekly memo off.",
                ephemeral=True)

        suggest = getattr(bot, "symbol_suggestions", None)
        if suggest is not None:
            add_cmd.autocomplete("symbol")(suggest)
            remove_cmd.autocomplete("symbol")(suggest)
        tree.add_command(group)
