"""The league: paper trading and prediction calls, with a leaderboard.

- /paper buy, sell, portfolio and reset: everyone starts with $100,000 of pretend money and trades stocks, ETFs and
  coins at live prices (no shorting, no leverage). Stock orders placed while the market is closed fill at the next
  open, so nobody trades on news against a stale closing price.
- /call: say a stock or coin goes up or down over a day, a week or a month; the bot grades it when time's up and
  posts the result in the 🏆 League channel.
- /league: the standings (portfolio return vs the S&P 500 since joining, and the best callers), posted there too
  after each week's last close.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import discord
from discord import app_commands

from .. import embeds as E
from ..briefs import Post
from ..hours import NEW_YORK, is_trading_day, market_open, next_trading_day
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from ..universe import CRYPTO, market_of, short
from . import Feature

log = logging.getLogger(__name__)

KIND = "league"
START_CASH = 100_000.0
BENCH = "^GSPC"
HORIZONS = {"1d": ("a day", 86400), "1w": ("a week", 7 * 86400), "1m": ("a month", 30 * 86400)}
MAX_OPEN_CALLS = 10
MIN_CALLS = 3  # graded calls before a caller shows on the board
MIN_ORDER = 1.0


def money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def tradable(symbol: str) -> bool:
    return not symbol.startswith("^") and not symbol.endswith(("=F", "=X"))


def fills_now(symbol: str, now: datetime | None = None) -> bool:
    """Coins trade around the clock; stocks and ETFs only while the US market is open."""
    return market_of(symbol) == CRYPTO or market_open(now)


def settled(symbol: str, now: datetime | None = None) -> bool:
    """When orders and calls placed while the market was closed start: five minutes after the open, once the quotes
    show the opening trades rather than the last close."""
    if market_of(symbol) == CRYPTO:
        return True
    t = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    return market_open(t) and (t.hour, t.minute) >= (9, 35)


def new_player(name: str, bench: float | None) -> dict:
    return {"name": name, "cash": START_CASH, "start": time.time(), "bench": bench, "positions": {}, "orders": [],
            "history": []}


def fill(player: dict, order: dict, price: float) -> str | None:
    """Applies a buy (in dollars) or sell (in shares; None for all) at `price`. Returns what happened, or None when
    there was nothing to do."""
    sym = order["symbol"]
    pos = player["positions"].get(sym)
    if order["side"] == "buy":
        dollars = min(order["dollars"], player["cash"])
        if dollars < MIN_ORDER or price <= 0:
            return None
        shares = dollars / price
        if pos:
            pos["cost"] += dollars
            pos["shares"] += shares
        else:
            player["positions"][sym] = {"shares": shares, "cost": dollars}
        player["cash"] -= dollars
    else:
        if not pos:
            return None
        shares = pos["shares"] if order.get("shares") is None else min(order["shares"], pos["shares"])
        if shares <= 0:
            return None
        part = shares / pos["shares"]
        dollars = shares * price
        pos["cost"] -= pos["cost"] * part
        pos["shares"] -= shares
        if pos["shares"] <= 1e-9:
            del player["positions"][sym]
        player["cash"] += dollars
    player["history"].append({"at": time.time(), "side": order["side"], "symbol": sym, "shares": shares,
                              "price": price})
    player["history"] = player["history"][-100:]
    verb = "Bought" if order["side"] == "buy" else "Sold"
    return f"{verb} {shares:,.4g} {short(sym)} at {money(price)} ({money(dollars)})"


def value(player: dict, prices: dict[str, float]) -> float:
    return player["cash"] + sum(p["shares"] * prices.get(s, p["cost"] / p["shares"] if p["shares"] else 0)
                                for s, p in player["positions"].items())


def standings(players: dict, prices: dict[str, float], bench_now: float | None) -> list[dict]:
    rows = []
    for uid, p in players.items():
        total = value(p, prices)
        ret = total / START_CASH - 1
        bench = (bench_now / p["bench"] - 1) if bench_now and p.get("bench") else None
        rows.append({"uid": uid, "name": p["name"], "value": total, "ret": ret, "bench": bench,
                     "positions": len(p["positions"])})
    return sorted(rows, key=lambda r: -r["ret"])


def call_return(call: dict, price: float) -> float:
    """The move in the called direction: positive when the call was right."""
    move = price / call["price"] - 1
    return move if call["direction"] == "up" else -move


def callers(calls: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for c in calls:
        if c.get("result") is None:
            continue
        r = by.setdefault(c["uid"], {"uid": c["uid"], "name": c["name"], "n": 0, "wins": 0, "edge": 0.0})
        r["n"] += 1
        r["wins"] += c["result"] > 0
        r["edge"] += c["result"]
    rows = [{**r, "rate": r["wins"] / r["n"], "edge": r["edge"] / r["n"]} for r in by.values() if r["n"] >= MIN_CALLS]
    return sorted(rows, key=lambda r: (-r["rate"], -r["edge"]))


def medal(i: int) -> str:
    return ("🥇", "🥈", "🥉")[i] if i < 3 else f"`{i + 1:>2}.`"


def league_embed(rows: list[dict], calls: list[dict], title: str = "🏆 League standings") -> discord.Embed:
    e = discord.Embed(title=title, color=E.GOLD)
    if rows:
        e.add_field(name="💼 Paper portfolios (started with $100K)", value=clip("\n".join(
            f"{medal(i)} **{clip(r['name'], 24)}** {r['ret']:+.2%} ({money(r['value'])})"
            + (f" · S&P {r['bench']:+.2%} since joining" if r["bench"] is not None else "")
            for i, r in enumerate(rows[:15])), 1024), inline=False)
    board = callers(calls)
    if board:
        e.add_field(name=f"🎯 Best callers ({MIN_CALLS}+ graded calls)", value=clip("\n".join(
            f"{medal(i)} **{clip(r['name'], 24)}** {r['wins']}/{r['n']} right ({r['rate']:.0%}) · "
            f"avg {r['edge']:+.2%} per call" for i, r in enumerate(board[:10])), 1024), inline=False)
    if not rows and not board:
        e.description = "Nobody's playing yet. Start with `/paper buy` or make a prediction with `/call`."
    e.set_footer(text="Pretend money at live prices · stock orders fill during market hours · not financial advice")
    return fit_embed(e)


def portfolio_embed(player: dict, prices: dict[str, float], bench_now: float | None) -> discord.Embed:
    total = value(player, prices)
    ret = total / START_CASH - 1
    e = discord.Embed(title=f"💼 {clip(player['name'], 40)}'s paper portfolio", color=E.GREEN if ret >= 0 else E.RED)
    lines = [f"Worth **{money(total)}** ({ret:+.2%}) · cash {money(player['cash'])}"]
    if bench_now and player.get("bench"):
        lines.append(f"S&P 500 since you joined: {bench_now / player['bench'] - 1:+.2%}")
    e.description = "\n".join(lines)
    rows = []
    for sym, p in sorted(player["positions"].items(), key=lambda kv: -kv[1]["shares"] * prices.get(kv[0], 0)):
        px = prices.get(sym)
        if px is None:
            rows.append(f"**{short(sym)}** {p['shares']:,.4g} sh · price unavailable")
            continue
        worth = p["shares"] * px
        gain = worth / p["cost"] - 1 if p["cost"] else 0
        rows.append(f"**{short(sym)}** {p['shares']:,.4g} sh · {money(worth)} ({gain:+.1%})")
    if rows:
        e.add_field(name="Holdings", value=clip("\n".join(rows[:20]), 1024), inline=False)
    if player["orders"]:
        e.add_field(name="⏳ Waiting for the open", value=clip("\n".join(
            f"{o['side']} {short(o['symbol'])} " + (money(o["dollars"]) if o["side"] == "buy" else
                                                   "all" if o.get("shares") is None else f"{o['shares']:,.4g} sh")
            for o in player["orders"]), 1024), inline=False)
    e.set_footer(text="Pretend money at live prices · not financial advice")
    return fit_embed(e)


def graded_embed(c: dict, price: float) -> discord.Embed:
    move = price / c["price"] - 1
    right = c["result"] > 0
    e = discord.Embed(title=f"{'✅' if right else '❌'} {clip(c['name'], 30)}: {short(c['symbol'])} "
                            f"{'up' if c['direction'] == 'up' else 'down'} over {HORIZONS[c['horizon']][0]}",
                      color=E.GREEN if right else E.RED,
                      description=f"{money(c['price'])} → {money(price)} ({move:+.2%}): "
                                  f"**{'right' if right else 'wrong'}**")
    return e


class League(Feature):
    name = "league"
    help_group = "🏆 League"

    def __init__(self, bot):
        super().__init__(bot)
        self.path = bot.data_dir / "league.json"
        data = read_json(self.path, {})
        self.players: dict[str, dict] = data.get("players") or {}
        self.calls: list[dict] = data.get("calls") or []
        self.next_id = max((c["id"] for c in self.calls), default=0) + 1

    def jobs(self):
        return [("league", 300, self.job)]

    def help(self):
        return [("paper buy", "buy with pretend money ($100K to start) at live prices"),
                ("paper sell", "sell some or all of a holding"),
                ("paper portfolio", "your (or someone's) paper portfolio and return"),
                ("call", "predict up or down over a day, week or month; graded automatically"),
                ("league", "the leaderboard: best portfolios and best callers")]

    def status(self):
        open_calls = sum(c.get("result") is None for c in self.calls)
        return [f"**League** {len(self.players)} players · {open_calls} open calls"]

    def save(self) -> None:
        cutoff = time.time() - 365 * 86400
        self.calls = [c for c in self.calls if c.get("result") is None or c["at"] >= cutoff]
        write_json(self.path, {"players": self.players, "calls": self.calls})

    async def prices(self, symbols) -> dict[str, float]:
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return {}
        quotes = await self.bot.engine.data.quotes(symbols)
        return {s: q.price for s, q in quotes.items() if q and q.price}

    async def price(self, symbol: str) -> float | None:
        return (await self.prices([symbol])).get(symbol)

    def channels(self):
        return self.bot.channels.of_kind(KIND)

    async def post(self, embed: discord.Embed) -> None:
        for cid, cfg in self.channels():
            if cfg.alerts:
                await self.bot.send(cid, Post([embed]))

    # ----- the job: fill orders at the open, grade calls, the weekly standings -----

    async def job(self) -> None:
        changed = await self.fill_waiting()
        changed |= await self.grade()
        if changed:
            self.save()
        ny = datetime.now(NEW_YORK)
        d = ny.date()
        if is_trading_day(d) and next_trading_day(d).isocalendar()[1] != d.isocalendar()[1]:
            for cid, cfg in self.channels():
                if cfg.briefs and self.bot._due(cid, "league-week", ny, 16, 30, 180):
                    await self.bot.send(cid, Post([await self.standings_embed("🏆 This week's league standings")]))

    async def fill_waiting(self) -> bool:
        waiting = [(p, o) for p in self.players.values() for o in p["orders"] if settled(o["symbol"])]
        if not waiting:
            return False
        prices = await self.prices(o["symbol"] for _, o in waiting)
        for player, order in waiting:
            px = prices.get(order["symbol"])
            if px is None:
                continue
            player["orders"].remove(order)
            fill(player, order, px)
        return True

    async def grade(self) -> bool:
        now = time.time()
        starting = [c for c in self.calls if c.get("price") is None and settled(c["symbol"])]
        due = [c for c in self.calls if c.get("result") is None and c.get("price") and c["due"] <= now]
        if not starting and not due:
            return False
        prices = await self.prices(c["symbol"] for c in starting + due)
        for c in starting:  # calls on stocks made while the market was closed start at the open
            if c["symbol"] in prices:
                c["price"] = prices[c["symbol"]]
                c["due"] = now + HORIZONS[c["horizon"]][1]
        for c in due:
            px = prices.get(c["symbol"])
            if px is None:
                continue
            c["result"] = call_return(c, px)
            c["end"] = px
            await self.post(graded_embed(c, px))
        return True

    async def standings_embed(self, title: str = "🏆 League standings") -> discord.Embed:
        held = {s for p in self.players.values() for s in p["positions"]}
        prices = await self.prices(list(held) + [BENCH])
        return league_embed(standings(self.players, prices, prices.get(BENCH)), self.calls, title)

    def player(self, user) -> dict | None:
        p = self.players.get(str(user.id))
        if p:
            p["name"] = user.display_name
        return p

    async def join(self, user) -> dict:
        p = self.player(user)
        if p is None:
            p = new_player(user.display_name, await self.price(BENCH))
            self.players[str(user.id)] = p
        return p

    # ----- commands -----

    def register(self, tree) -> None:
        bot = self.bot
        paper = app_commands.Group(name="paper", description="Paper trading with $100K of pretend money")

        async def resolve(interaction, symbol):
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return None
            if not tradable(r.symbol):
                await interaction.followup.send(f"**{short(r.symbol)}** is an index or rate: pick a stock, ETF or "
                                                "coin (SPY tracks the S&P 500, QQQ the Nasdaq 100).")
                return None
            return r

        @paper.command(name="buy", description="Buy a stock, ETF or coin with pretend money")
        @app_commands.describe(symbol="Ticker or name, e.g. NVDA or bitcoin", dollars="How many dollars to spend")
        async def buy(interaction: discord.Interaction, symbol: str,
                      dollars: app_commands.Range[float, MIN_ORDER, 10_000_000.0]):
            await interaction.response.defer(thinking=True)
            r = await resolve(interaction, symbol)
            if r is None:
                return
            p = await self.join(interaction.user)
            order = {"side": "buy", "symbol": r.symbol, "dollars": float(dollars)}
            if dollars > p["cash"] + 0.005:
                await interaction.followup.send(f"You have {money(p['cash'])} in cash.")
                return
            await self.place(interaction, p, order)

        @paper.command(name="sell", description="Sell some or all of a paper holding")
        @app_commands.describe(symbol="What to sell", shares="How many shares or coins (leave empty to sell all)")
        async def sell(interaction: discord.Interaction, symbol: str,
                       shares: app_commands.Range[float, 0.000001, None] | None = None):
            await interaction.response.defer(thinking=True)
            p = self.player(interaction.user)
            r = await resolve(interaction, symbol)
            if r is None:
                return
            if not p or r.symbol not in p["positions"]:
                await interaction.followup.send(f"You don't hold any {short(r.symbol)}.")
                return
            await self.place(interaction, p, {"side": "sell", "symbol": r.symbol,
                                              "shares": float(shares) if shares else None})

        @paper.command(name="portfolio", description="A paper portfolio: holdings, return and orders waiting")
        @app_commands.describe(user="Whose (yours if empty)")
        async def portfolio(interaction: discord.Interaction, user: discord.User | None = None):
            await interaction.response.defer(thinking=True)
            who = user or interaction.user
            p = self.players.get(str(who.id))
            if p is None:
                whose = "You haven't" if who == interaction.user else f"{who.display_name} hasn't"
                await interaction.followup.send(f"{whose} started yet: `/paper buy` to begin with $100,000.")
                return
            prices = await self.prices(list(p["positions"]) + [BENCH])
            await interaction.followup.send(embed=portfolio_embed(p, prices, prices.get(BENCH)))

        @paper.command(name="reset", description="Start over with $100,000 (your calls' record stays)")
        async def reset(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            self.players[str(interaction.user.id)] = new_player(interaction.user.display_name, await self.price(BENCH))
            self.save()
            await interaction.followup.send("Fresh start: $100,000 in cash.", ephemeral=True)

        suggest = getattr(bot, "symbol_suggestions", None)
        if suggest is not None:
            buy.autocomplete("symbol")(suggest)
            sell.autocomplete("symbol")(suggest)
        tree.add_command(paper)

        @tree.command(name="call", description="Predict a stock or coin goes up or down; graded when time's up")
        @app_commands.describe(symbol="Ticker or name", direction="Up or down", horizon="Over how long")
        @app_commands.choices(direction=[app_commands.Choice(name="📈 Up", value="up"),
                                         app_commands.Choice(name="📉 Down", value="down")],
                              horizon=[app_commands.Choice(name="1 day", value="1d"),
                                       app_commands.Choice(name="1 week", value="1w"),
                                       app_commands.Choice(name="1 month", value="1m")])
        async def call_cmd(interaction: discord.Interaction, symbol: str, direction: app_commands.Choice[str],
                           horizon: app_commands.Choice[str]):
            await interaction.response.defer(thinking=True)
            r = await bot.resolve_symbol(interaction, symbol)
            if r is None:
                return
            uid = str(interaction.user.id)
            open_calls = [c for c in self.calls if c["uid"] == uid and c.get("result") is None]
            if len(open_calls) >= MAX_OPEN_CALLS:
                await interaction.followup.send(f"You have {MAX_OPEN_CALLS} calls running; wait for one to finish.")
                return
            now = time.time()
            c = {"id": self.next_id, "uid": uid, "name": interaction.user.display_name, "symbol": r.symbol,
                 "direction": direction.value, "horizon": horizon.value, "at": now, "price": None,
                 "due": now + HORIZONS[horizon.value][1], "result": None}
            if fills_now(r.symbol):
                c["price"] = await self.price(r.symbol)
                if not c["price"]:
                    await interaction.followup.send("I couldn't get a price for that right now; try again shortly.")
                    return
            self.next_id += 1
            self.calls.append(c)
            self.save()
            arrow = "📈 up" if c["direction"] == "up" else "📉 down"
            what = f"**{short(r.symbol)} {arrow}** over {horizon.name.lower()}"
            start = (f"from {money(c['price'])}, graded <t:{int(c['due'])}:R>" if c["price"] else
                     "starting from the next market open")
            await interaction.followup.send(f"🎯 {interaction.user.mention} calls {what}, {start}.")

        if suggest is not None:
            call_cmd.autocomplete("symbol")(suggest)

        @tree.command(name="league", description="Leaderboard: best paper portfolios and best callers")
        async def league_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(embed=await self.standings_embed())

    async def place(self, interaction: discord.Interaction, player: dict, order: dict) -> None:
        sym = order["symbol"]
        if not fills_now(sym):
            player["orders"].append(order)
            self.save()
            await interaction.followup.send(f"⏳ The US market is closed: your {order['side']} of {short(sym)} fills "
                                            "at the next open.")
            return
        px = await self.price(sym)
        if not px:
            await interaction.followup.send("I couldn't get a price for that right now; try again shortly.")
            return
        done = fill(player, order, px)
        self.save()
        await interaction.followup.send(f"✅ {done} · cash left {money(player['cash'])}" if done else
                                        "Nothing to do there.")
