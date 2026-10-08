"""🤖 MarketBot as a league player. An admin funds it with any amount of pretend money (`/botplayer start amount:10`)
and it trades by itself, ranked next to everyone else.

Once a day between 3:40 and 3:58 PM ET it scans a fixed list of big stocks, sector ETFs and major coins with the
breakout model (the part of the model that tests well: its odds that a price clears its 20-day high within 10
days, against the usual rate) and holds the names where those odds are well above usual and the odds of breaking
down are low: up to 4 stocks or ETFs and 1 coin (or 5 stocks, or 3 coins), about equal dollars each. It sells when
the odds fade, after 10 trading days unless it's still a top pick, or when the price falls through a wide safety
stop on two checks a few minutes apart. Decisions and fills use the same live quotes, every await comes before any
change to its portfolio, and a per-day marker is saved with the trades, so a restart can't make it trade twice.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands

from .. import embeds as E
from ..limits import clip, fit_embed
from ..universe import CRYPTO, DEFAULT_CRYPTO, DEFAULT_STOCKS, SECTORS, STOCKS, coin_base, market_of, short
from . import league as L

log = logging.getLogger(__name__)

BOT_ID = "marketbot"  # its key among the players; Discord IDs are numbers, so it can't clash
BOT_NAME = "🤖 MarketBot"
SLOTS = {"both": {STOCKS: 4, CRYPTO: 1}, "stocks": {STOCKS: 5}, "crypto": {CRYPTO: 3}}
MODES = {"both": "stocks and crypto", "stocks": "stocks only", "crypto": "crypto only"}
P_UP = 0.60  # breakout odds needed to buy
TILT_IN, TILT_OUT = 0.25, 0.0  # (odds up - usual) - (odds down - usual): to buy, and below which it sells
HOLD_DAYS = 10  # the model's breakout horizon
MIN_AUC = 0.70  # the model must have tested at least this well to trade on it
WINDOW = ((15, 40), (15, 58))  # New York time, every day for coins, trading days for stocks
STOP_ATR = 3.0
STOP_RANGE = {STOCKS: (0.08, 0.25), CRYPTO: (0.12, 0.35)}  # the stop's distance, from 3 daily ranges, kept in here
STOP_CONFIRM = 240  # seconds between the two checks below the stop
COOLDOWN_DAYS = 5  # no buying back a name for this long after a stop
FRESH = {STOCKS: 900, CRYPTO: 1800}  # oldest usable quote, seconds
FRESH_SHARE = 0.5  # at least this share of the list must have fresh quotes, else it sits the day out (retrying)
UNIVERSE_MAX = {STOCKS: 50, CRYPTO: 25}
STABLES = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "STETH", "WBTC"}
KEEP_TRADES = 200
KEEP_SEASONS = 20


# ----- the rules (pure) -----

def tilt(h) -> float:
    return (h.breakout_up - h.base_up) - (h.breakout_down - h.base_down)


def fresh(q, market: str, now_ts: float) -> bool:
    """A live quote: recent, and for stocks from the regular session."""
    if not q or not q.price or q.price <= 0 or not q.time or now_ts - q.time > FRESH[market]:
        return False
    return market == CRYPTO or q.market_state in ("", "REGULAR")


def due_after(day: date, market: str) -> date:
    if market == CRYPTO:
        return day + timedelta(days=HOLD_DAYS)
    for _ in range(HOLD_DAYS):
        day = L.next_trading_day(day)
    return day


def model_ready(models: dict, market: str) -> bool:
    model = (models or {}).get(market)
    skill = getattr(model, "skill", None) or {}
    try:
        return all(getattr(skill[k], "auc", 0) >= MIN_AUC for k in ("breakout_up", "breakout_down"))
    except (KeyError, TypeError):
        return False


def why(h) -> str:
    text = (f"breakout odds {h.breakout_up:.0%} (usual {h.base_up:.0%}), breakdown {h.breakout_down:.0%} "
            f"(usual {h.base_down:.0%})")
    setup = next((a for a in h.setups or [] if a.direction > 0), None)
    return text + (f" · {setup.emoji} {setup.name}" if setup else "")


def decide(p: dict, market: str, hits, today: str):
    """(sells as (symbol, reason), renewals, buys as hits) for one market."""
    cap = SLOTS[p["auto"]["markets"]][market]
    by = {h.symbol: h for h in hits if h.breakout_up is not None and h.breakout_down is not None}
    ranked = sorted((h for h in by.values() if h.breakout_up >= P_UP and tilt(h) >= TILT_IN),
                    key=lambda h: (-tilt(h), -h.pressure))
    top = {h.symbol for h in ranked[:cap]}
    held = [s for s in p["positions"] if market_of(s) == market]
    sells, renew = [], []
    for s in held:
        h = by.get(s)
        if h is None:  # no fresh quote or no odds today: hold, never act on stale data
            continue
        if tilt(h) < TILT_OUT:
            sells.append((s, f"breakout odds faded: {h.breakout_up:.0%} (usual {h.base_up:.0%}), breakdown "
                             f"{h.breakout_down:.0%} (usual {h.base_down:.0%})"))
        elif p["positions"][s].get("due", "") <= today:
            if s in top:
                renew.append(s)
            else:
                days = f"{HOLD_DAYS} {'trading ' if market == STOCKS else ''}days"
                sells.append((s, f"{days} up and no longer a top pick"))
    free = cap - len(held) + len(sells)
    cool = p["auto"].get("cooldown") or {}
    buys = [h for h in ranked if h.symbol not in p["positions"] and cool.get(h.symbol, "") <= today][:max(free, 0)]
    return sells, renew, buys


def new_bot(amount: float, markets: str, bench: float | None, season: int, by: dict) -> dict:
    p = L.new_player(BOT_NAME, bench)
    p.update(cash=float(amount), funded=float(amount),
             auto={"v": 1, "season": season, "markets": markets, "by": by, "runs": {}, "cooldown": {}, "trades": [],
                   "tally": {"closed": 0, "wins": 0, "sum_ret": 0.0}, "last": None})
    return p


def close(p: dict, sym: str, px: float, reason: str, ts: float, stop: bool = False) -> str:
    """Sells all of a holding and records the trade; returns the post line."""
    pos = p["positions"][sym]
    ret = pos["shares"] * px / pos["cost"] - 1 if pos["cost"] else 0.0
    held = max(0.0, (ts - pos.get("at", ts)) / 86400)
    L.fill(p, {"side": "sell", "symbol": sym, "shares": None}, px)
    auto = p["auto"]
    auto["trades"] = (auto["trades"] + [{"symbol": sym, "in": pos.get("at"), "out": ts, "entry": pos.get("entry"),
                                         "exit": px, "ret": ret, "why_in": pos.get("why", ""),
                                         "why_out": reason}])[-KEEP_TRADES:]
    t = auto["tally"]
    t["closed"] += 1
    t["wins"] += ret > 0
    t["sum_ret"] += ret
    return (f"{'🛑' if stop else '📉'} Sold **{short(sym)}** at {L.money(px)} ({ret:+.1%}, held {held:.0f}d): "
            f"{reason}")


def season_record(p: dict, prices: dict, bench_now: float | None, end: float) -> dict:
    final = L.value(p, prices)
    t = p["auto"]["tally"]
    return {"season": p["auto"]["season"], "markets": p["auto"]["markets"], "funded": p["funded"],
            "start": p["start"], "end": end, "final": final, "ret": final / p["funded"] - 1,
            "bench_ret": (bench_now / p["bench"] - 1) if bench_now and p.get("bench") else None,
            "closed": t["closed"], "wins": t["wins"], "avg": t["sum_ret"] / t["closed"] if t["closed"] else None,
            "unpriced": sum(1 for s in p["positions"] if s not in prices), "by": p["auto"].get("by")}


def season_line(s: dict) -> str:
    start, end = (datetime.fromtimestamp(s[k], L.NEW_YORK) for k in ("start", "end"))
    bench = f" vs S&P {s['bench_ret']:+.1%}" if s.get("bench_ret") is not None else ""
    return (f"S{s['season']} {L.money(s['funded'])} → {L.money(s['final'])} ({s['ret']:+.1%}){bench}, "
            f"{start:%b %-d}–{end:%b %-d} · {s['closed']} trades")


def next_decision(now: datetime, mode: str) -> datetime:
    start = now.replace(hour=WINDOW[0][0], minute=WINDOW[0][1], second=0, microsecond=0)
    day = now.date() if (now.hour, now.minute) < WINDOW[0] else now.date() + timedelta(days=1)
    if mode == "stocks":
        while not L.is_trading_day(day):
            day += timedelta(days=1)
    return start.replace(year=day.year, month=day.month, day=day.day)


def trades_embed(p: dict, lines: list[str], prices: dict) -> discord.Embed:
    worth = L.value(p, prices)
    ret = worth / p["funded"] - 1
    e = discord.Embed(title=f"{BOT_NAME} traded", color=E.GREEN if ret >= 0 else E.RED,
                      description="\n".join(lines[:10]) + (f"\n…and {len(lines) - 10} more" if len(lines) > 10 else ""))
    bench = prices.get(L.BENCH)
    since = datetime.fromtimestamp(p["start"], L.NEW_YORK)
    e.set_footer(text=f"Worth {L.money(worth)} ({ret:+.2%}) since {since:%b %-d}"
                      + (f" · S&P {bench / p['bench'] - 1:+.2%}" if bench and p.get("bench") else "")
                      + " · pretend money · not financial advice")
    return fit_embed(e)


def bot_embed(p: dict | None, prices: dict, seasons: list[dict], now: datetime) -> discord.Embed:
    if p is None:
        e = discord.Embed(title=f"💼 {BOT_NAME}'s paper portfolio", color=E.GREY,
                          description="No bot player yet. An admin can start one with `/botplayer start amount:10`.")
    else:
        e = L.portfolio_embed(p, prices, prices.get(L.BENCH))
        auto = p["auto"]
        holds = []
        for s, pos in p["positions"].items():
            day = max(1, (now.timestamp() - pos.get("at", now.timestamp())) // 86400 + 1)
            due = date.fromisoformat(pos["due"]) if pos.get("due") else None
            holds.append(f"**{short(s)}** day {day:.0f}" + (f" · out by {due:%b %-d} unless still a top pick"
                                                             if due else "")
                         + (f" · stop {L.money(pos['stop'])}" if pos.get("stop") else "") + f" · {pos.get('why', '')}")
        if holds:
            e.add_field(name="🧠 Why it holds each", value=clip("\n".join(holds), 1024), inline=False)
        t = auto["tally"]
        e.add_field(name="📒 Record", value=(f"{t['closed']} trades closed · {t['wins']} won "
                                             f"({t['wins'] / t['closed']:.0%}) · avg {t['sum_ret'] / t['closed']:+.1%} "
                                             "per trade") if t["closed"] else "No closed trades yet", inline=False)
        last = auto.get("last")
        nxt = next_decision(now, auto["markets"])
        e.add_field(name="🕒 Decisions", value=(f"Last <t:{int(last['at'])}:R> ({last['market']}): {last['note']}"
                                               if last else "None yet") + f" · next <t:{int(nxt.timestamp())}:R>",
                    inline=False)
        e.description = (e.description or "") + (f"\nFunded with {L.money(p['funded'])} ({MODES[auto['markets']]}), "
                                                 f"season {auto['season']}")
    if seasons:
        e.add_field(name="🗂️ Past seasons", value="\n".join(season_line(s) for s in seasons[-3:][::-1]),
                    inline=False)
    e.set_footer(text=f"Buys breakout odds ≥{P_UP:.0%} with tilt ≥{TILT_IN} · sells when the tilt turns negative, "
                      f"after {HOLD_DAYS} days unless still a top pick, or on a safety stop · backtests flattered it "
                      "(they used today's winners); this live record is the real test · pretend money · "
                      "not financial advice")
    return fit_embed(e)


def intro(p: dict, who: str) -> str:
    slots = SLOTS[p["auto"]["markets"]]
    each = p["funded"] / sum(slots.values())
    picks = " and ".join(f"{n} {'stocks/ETFs' if m == STOCKS else 'coin' if n == 1 else 'coins'}"
                         for m, n in slots.items())
    return (f"🤖 **MarketBot** joined the league with **{L.money(p['funded'])}** of pretend money "
            f"({MODES[p['auto']['markets']]}), started by {who}. Once a day around 3:45 PM ET it buys the names where "
            f"the breakout model's odds of clearing the 20-day high are well above usual and the odds of breaking "
            f"down are low: up to {picks}, about {L.money(each)} each. It sells when those odds fade, after "
            f"{HOLD_DAYS} trading days unless it's still a top pick, or if the price falls through a safety stop. "
            "Its trades are posted here; `/paper bot` shows what it holds and why. The model predicts breakouts, not "
            "profits.")


class Auto:
    """The bot player's turns, run from the league's job."""

    def __init__(self, league):
        self.league = league
        self.prices: dict[str, float] = {}

    @property
    def engine(self):
        return self.league.bot.engine

    def player(self) -> dict | None:
        return self.league.players.get(BOT_ID)

    def universe(self, p: dict, market: str) -> list[str]:
        held = [s for s in p["positions"] if market_of(s) == market]
        if market == STOCKS:
            fixed = DEFAULT_STOCKS + list(SECTORS) + ["SPY", "QQQ", "IWM", "DIA"]
        else:
            fixed = list(DEFAULT_CRYPTO)
        watched = [s for _, cfg in self.league.bot.channels.of_kind(market) for s in cfg.symbols()]
        out = [s for s in dict.fromkeys(fixed + watched) if s not in held and L.tradable(s) and
               market_of(s) == market and coin_base(s) not in STABLES]
        return held + out[:max(0, UNIVERSE_MAX[market] - len(held))]

    async def step(self, now: datetime) -> None:
        p = self.player()
        if p is None:
            return
        lines = await self.stops(p, now)
        if WINDOW[0] <= (now.hour, now.minute) < WINDOW[1]:
            iso = now.date().isoformat()
            for market in SLOTS[p["auto"]["markets"]]:
                if p["auto"]["runs"].get(market) == iso or self.player() is not p:
                    continue
                if market == STOCKS and not L.market_open(now):
                    continue
                lines += await self.turn(p, market, now)
        if lines and self.player() is p:
            await self.league.post(trades_embed(p, lines, self.prices))

    async def turn(self, p: dict, market: str, now: datetime) -> list[str]:
        ts, iso = now.timestamp(), now.date().isoformat()
        universe = self.universe(p, market)
        quotes = await self.engine.data.quotes(list(dict.fromkeys(universe + list(p["positions"]) + [L.BENCH])))
        live = {s: quotes[s] for s in universe if fresh(quotes.get(s), market, ts)}
        if len(live) < FRESH_SHARE * len(universe):
            log.info("MarketBot sits out %s for now: %d of %d fresh quotes", market, len(live), len(universe))
            return []
        ready = model_ready(getattr(self.engine, "models", {}), market)
        hits = await self.engine.scan(list(live), live, lookback=3) if ready else []
        if self.player() is not p:  # stopped or restarted while this turn waited
            return []
        # from here on nothing waits, so the turn is applied whole or not at all
        self.prices.update({s: q.price for s, q in quotes.items() if q and q.price})
        lines, bought, sold = [], [], []
        if ready:
            sells, renew, buys = decide(p, market, hits, iso)
            for s, reason in sells:
                lines.append(close(p, s, live[s].price, reason, ts))
                sold.append(short(s))
            for s in renew:
                p["positions"][s]["due"] = due_after(now.date(), market).isoformat()
            slot = max(L.value(p, self.prices) / sum(SLOTS[p["auto"]["markets"]].values()), L.MIN_ORDER)
            for h in buys:
                px = live[h.symbol].price
                dollars = min(slot, p["cash"])
                if L.fill(p, {"side": "buy", "symbol": h.symbol, "dollars": dollars}, px) is None:
                    break
                lo, hi = STOP_RANGE[market]
                dist = min(max(STOP_ATR * (h.atr or 0) / px, lo), hi)
                p["positions"][h.symbol].update(entry=px, at=ts, day=iso, due=due_after(now.date(), market).isoformat(),
                                                stop=px * (1 - dist), stop_hit=None, p_up=h.breakout_up,
                                                base_up=h.base_up, p_down=h.breakout_down, base_down=h.base_down,
                                                why=why(h))
                lines.append(f"📈 Bought {L.money(dollars)} of **{short(h.symbol)}** at {L.money(px)}: {why(h)}")
                bought.append(short(h.symbol))
            held = sum(1 for s in p["positions"] if market_of(s) == market)
            note = "; ".join(x for x in (f"bought {', '.join(bought)}" if bought else "",
                                         f"sold {', '.join(sold)}" if sold else "") if x) or \
                f"no change ({held} holding{'' if held == 1 else 's'})"
        else:
            note = "model not ready: holding"
        p["auto"]["runs"][market] = iso
        p["auto"]["last"] = {"at": ts, "market": market, "value": L.value(p, self.prices), "note": note}
        self.league.save()
        return lines

    async def stops(self, p: dict, now: datetime) -> list[str]:
        held = [s for s in p["positions"] if p["positions"][s].get("stop") and L.settled(s, now)]
        if not held:
            return []
        quotes = await self.engine.data.quotes(held)
        if self.player() is not p:
            return []
        ts, lines, changed = now.timestamp(), [], False
        for s in held:
            q, pos = quotes.get(s), p["positions"].get(s)
            if pos is None or not fresh(q, market_of(s), ts):
                continue
            self.prices[s] = q.price
            if q.price > pos["stop"]:
                if pos.get("stop_hit"):
                    pos["stop_hit"], changed = None, True
                continue
            if not pos.get("stop_hit"):
                pos["stop_hit"], changed = ts, True
            elif ts - pos["stop_hit"] >= STOP_CONFIRM:
                lines.append(close(p, s, q.price, f"fell through its safety stop {L.money(pos['stop'])}", ts, True))
                p["auto"]["cooldown"][s] = (now.date() + timedelta(days=COOLDOWN_DAYS)).isoformat()
                changed = True
        if changed:
            self.league.save()
        return lines

    # ----- commands -----

    def register(self, tree, paper) -> None:
        league = self.league
        group = app_commands.Group(name="botplayer", description="The bot player: give it pretend money and it "
                                   "trades by itself", default_permissions=discord.Permissions(manage_channels=True),
                                   guild_only=True)

        @group.command(name="start", description="Give MarketBot pretend money (even $10) to trade by itself")
        @app_commands.describe(amount="How much pretend money, e.g. 10 or 100000", markets="What it may trade")
        @app_commands.choices(markets=[app_commands.Choice(name="Stocks & crypto", value="both"),
                                       app_commands.Choice(name="Stocks only", value="stocks"),
                                       app_commands.Choice(name="Crypto only", value="crypto")])
        async def start(interaction: discord.Interaction, amount: app_commands.Range[float, 5.0, 10_000_000.0],
                        markets: app_commands.Choice[str] | None = None):
            await interaction.response.defer(thinking=True)
            p = self.player()
            if p is not None:
                worth = L.value(p, await league.prices(list(p["positions"])))
                await interaction.followup.send(
                    f"🤖 MarketBot is already playing with {L.money(p['funded'])} since <t:{int(p['start'])}:R> "
                    f"(now {L.money(worth)}, {worth / p['funded'] - 1:+.2%}). `/botplayer stop` ends its season "
                    "first; the result stays on record.")
                return
            season = max((s["season"] for s in league.bot_seasons), default=0) + 1
            by = {"uid": str(interaction.user.id), "name": interaction.user.display_name,
                  "guild": interaction.guild_id}
            p = new_bot(float(amount), markets.value if markets else "both", await league.price(L.BENCH), season, by)
            if self.player() is not None:  # someone else started one meanwhile
                await interaction.followup.send("🤖 MarketBot was just started by someone else.")
                return
            league.players[BOT_ID] = p
            league.save()
            text = intro(p, interaction.user.display_name)
            await interaction.followup.send(text)
            await league.post(discord.Embed(title=f"{BOT_NAME} joined the league", description=text, color=E.GOLD),
                              skip=interaction.channel_id)

        @group.command(name="stop", description="End MarketBot's season; its result stays on record")
        async def stop(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            p = self.player()
            if p is None:
                await interaction.followup.send("No bot player is running.")
                return
            by = p["auto"].get("by") or {}
            if interaction.guild_id != by.get("guild") and str(interaction.user.id) != by.get("uid"):
                await interaction.followup.send("Only admins of the server that started MarketBot can stop it.")
                return
            prices = await league.prices(list(p["positions"]) + [L.BENCH])
            if self.player() is not p:
                await interaction.followup.send("It was just stopped.")
                return
            end = datetime.now(L.NEW_YORK).timestamp()
            record = season_record(p, prices, prices.get(L.BENCH), end)
            league.bot_seasons = (league.bot_seasons + [record])[-KEEP_SEASONS:]
            del league.players[BOT_ID]
            league.save()
            days = max(1, round((end - p["start"]) / 86400))
            bench = f" · S&P {record['bench_ret']:+.2%}" if record["bench_ret"] is not None else ""
            text = (f"🤖 MarketBot's season {record['season']} is over: {L.money(record['funded'])} → "
                    f"{L.money(record['final'])} ({record['ret']:+.2%}) in {days} day{'' if days == 1 else 's'}"
                    f"{bench} · {record['closed']} trades closed, {record['wins']} won")
            await interaction.followup.send(text)
            await league.post(discord.Embed(title=f"{BOT_NAME}'s season is over", description=text, color=E.GOLD),
                              skip=interaction.channel_id)

        @paper.command(name="bot", description="MarketBot's own paper portfolio: what it holds, why, and its record")
        async def bot_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            p = self.player()
            prices = await league.prices(list(p["positions"]) + [L.BENCH]) if p else {}
            await interaction.followup.send(embed=bot_embed(p, prices, league.bot_seasons,
                                                            datetime.now(L.NEW_YORK)))

        tree.add_command(group)
