"""Alerts in plain words: `/when NVDA drops below 170`, `/when bitcoin is up 5% today`, `/when TSLA RSI under 30`,
`/when SPY crosses above its 200-day average`, `/when AMD makes a new 52-week high`, `/when GME volume is 3x normal`.

The common phrasings are read by a parser; anything else goes to the free AI reader, which turns it into the same
conditions. Every condition must hold together, and an alert fires when they become true (not while they stay
true), so "above 200" pings when the price crosses 200. A ping goes where the alert was made (or by DM).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import discord
import numpy as np
from discord import app_commands

from .. import embeds as E
from ..briefs import Post
from ..indicators import rsi, sma
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from ..universe import ALIASES, STOCKS, market_of, short
from . import Feature

log = logging.getLogger(__name__)

METRICS = ("price", "change", "move", "rsi", "ma", "high52", "low52", "volume")
PER_USER = 10
STALE_DAYS = 90  # an alert that never fires is dropped after this long

SYSTEM = (
    "You turn a trader's alert request into conditions. Reply with JSON only. `symbol` is the ticker or name exactly "
    "as the user wrote it. Each condition is {metric, op, value} with op 'above' or 'below':\n"
    "- price: the price in dollars (value = the level)\n"
    "- change: today's change in percent (up 5% today -> change above 5; down 3% -> change below -3)\n"
    "- move: today's absolute change in percent either way (moves 4% -> move above 4)\n"
    "- rsi: the 14-day RSI (oversold -> rsi below 30; overbought -> rsi above 70)\n"
    "- ma: price vs its N-day moving average (value = N, e.g. 200; above = trades above it)\n"
    "- high52 / low52: a new 52-week high or low (op 'above' for high52, 'below' for low52, value 0)\n"
    "- volume: today's volume as a multiple of normal (3x volume -> volume above 3)\n"
    "Several conditions all have to be true together. If the request isn't an alert about one stock, ETF, index or "
    "coin, return an empty conditions list.")
SCHEMA = {"type": "object", "properties": {
    "symbol": {"type": "string"},
    "conditions": {"type": "array", "items": {"type": "object", "properties": {
        "metric": {"type": "string", "enum": list(METRICS)}, "op": {"type": "string", "enum": ["above", "below"]},
        "value": {"type": "number"}}, "required": ["metric", "op", "value"]}}},
    "required": ["symbol", "conditions"]}


@dataclass
class Cond:
    metric: str
    op: str  # above | below
    value: float

    def text(self) -> str:
        v = self.value
        return {"price": f"price {self.op} ${v:,.2f}",
                "change": f"up {v:g}%+ today" if self.op == "above" else f"down {abs(v):g}%+ today",
                "move": f"moves {abs(v):g}%+ today either way",
                "rsi": f"RSI {self.op} {v:g}",
                "ma": f"{self.op} its {v:g}-day average",
                "high52": "new 52-week high", "low52": "new 52-week low",
                "volume": f"volume {v:g}x normal"}[self.metric]


def valid(c: Cond) -> bool:
    v = c.value
    if c.metric not in METRICS or c.op not in ("above", "below") or not np.isfinite(v):
        return False
    return {"price": v > 0, "change": -90 <= v <= 500 and v != 0, "move": 0 < abs(v) <= 500,
            "rsi": 1 <= v <= 99, "ma": 2 <= v <= 400 and v == int(v), "high52": True, "low52": True,
            "volume": 1 < v <= 100}[c.metric]


# ----- the parser for common phrasings -----

UP = r"(?<![A-Za-z])(?:up|rises?|rising|jumps?|gains?|rall(?:y|ies)|climbs?|pumps?|surges?|spikes?|soars?)"
DOWN = r"(?<![A-Za-z])(?:down|drops?|dropping|falls?|falling|dumps?|sinks?|loses?|tanks?|plunges?|slides?|crashes?)"
ABOVE = r"(?<![A-Za-z])(?:above|over|>|>=|breaks? above|crosses? above|goes above|gets above|tops?|exceeds?)"
BELOW = r"(?<![A-Za-z])(?:below|under|<|<=|breaks? below|crosses? below|goes below|dips? below|drops? below|falls? below|"\
        r"drops? under|falls? under)"
NUM = r"\$?(\d[\d,]*(?:\.\d+)?)(?![\d.,]*\d)\s*(k\b|K\b)?"
RULES = [
    (rf"\brsi\b(?:\s+is)?\s+{ABOVE}\s+(\d+(?:\.\d+)?)", lambda m: Cond("rsi", "above", float(m[1]))),
    (rf"\brsi\b(?:\s+is)?\s+{BELOW}\s+(\d+(?:\.\d+)?)", lambda m: Cond("rsi", "below", float(m[1]))),
    (r"\boversold\b", lambda m: Cond("rsi", "below", 30.0)),
    (r"\boverbought\b", lambda m: Cond("rsi", "above", 70.0)),
    (rf"{ABOVE}\s+(?:the\s+|its\s+)?(\d+)[- ]?(?:day|d|dma)\b", lambda m: Cond("ma", "above", float(m[1]))),
    (rf"{BELOW}\s+(?:the\s+|its\s+)?(\d+)[- ]?(?:day|d|dma)\b", lambda m: Cond("ma", "below", float(m[1]))),
    (r"\b(?:new\s+)?52[- ]?(?:week|wk)\s+high|\bnew\s+high\b", lambda m: Cond("high52", "above", 0.0)),
    (r"\b(?:new\s+)?52[- ]?(?:week|wk)\s+low|\bnew\s+low\b", lambda m: Cond("low52", "below", 0.0)),
    (r"\bvolume\b[^\d]{0,20}(\d+(?:\.\d+)?)\s*(?:x|times)|(\d+(?:\.\d+)?)\s*(?:x|times)\s+(?:the\s+)?"
     r"(?:normal\s+|average\s+|usual\s+)?volume", lambda m: Cond("volume", "above", float(m[1] or m[2]))),
    (rf"{UP}\s+(?:by\s+|over\s+|more than\s+)?(\d+(?:\.\d+)?)\s*%", lambda m: Cond("change", "above", float(m[1]))),
    (rf"{DOWN}\s+(?:by\s+|over\s+|more than\s+)?(\d+(?:\.\d+)?)\s*%",
     lambda m: Cond("change", "below", -float(m[1]))),
    (r"\bmoves?\s+(?:by\s+|over\s+|more than\s+)?(\d+(?:\.\d+)?)\s*%", lambda m: Cond("move", "above", float(m[1]))),
    (rf"{ABOVE}\s+{NUM}(?!\s*(?:%|-?\s*day|d\b|dma|x\b))", lambda m: Cond("price", "above", amount(m[1], m[2]))),
    (rf"{BELOW}\s+{NUM}(?!\s*(?:%|-?\s*day|d\b|dma|x\b))", lambda m: Cond("price", "below", amount(m[1], m[2]))),
    (rf"(?<![A-Za-z])(?:hits?|reach(?:es)?|touch(?:es)?|gets? to|at)\s+{NUM}(?!\s*(?:%|-?\s*day|x\b))",
     lambda m: Cond("price", "touch", amount(m[1], m[2]))),
]
NOT_TICKERS = {"RSI", "MA", "DMA", "EMA", "SMA", "ATH", "USD", "X", "I", "A", "AM", "PM", "ET", "UTC", "ME", "MY",
               "IT", "IS", "IF", "UP", "ON", "OR", "AT", "BY", "TO", "THE", "WHEN", "NEW", "DAY", "WK"}


def amount(digits: str, k: str | None) -> float:
    v = float(digits.replace(",", ""))
    return v * 1000 if k else v


def parse(text: str) -> tuple[list[Cond], str]:
    """Conditions found by the parser, and the text left over (where the symbol is)."""
    conds, rest = [], text
    for pattern, make in RULES:
        while True:
            m = re.search(pattern, rest, re.I)
            if not m:
                break
            c = make(m)
            if c.metric not in {x.metric for x in conds}:
                conds.append(c)
            rest = rest[:m.start()] + " " + rest[m.end():]
    return conds, rest


def symbol_words(rest: str) -> list[str]:
    """Likely symbols in what's left, best first: $TICKER, then TICKER in capitals, then known names ("nvidia")."""
    words = re.findall(r"\$?[A-Za-z][A-Za-z0-9.&-]*", rest)
    dollar = [w[1:] for w in words if w.startswith("$")]
    caps = [w for w in words if w.isupper() and 1 <= len(w) <= 6 and w not in NOT_TICKERS]
    low = rest.lower()
    names = sorted((a for a in ALIASES if len(a) > 2 and re.search(rf"(?<![\w&]){re.escape(a)}(?![\w&])", low)),
                   key=lambda a: -len(a))
    return list(dict.fromkeys(dollar + caps + names))


# ----- checking -----

def holds(c: Cond, q, bars) -> bool | None:
    """Whether the condition holds now; None when it can't be told (no data yet)."""
    v = _holds(c, q, bars)
    return None if v is None else bool(v)  # numpy's booleans aren't `True`


def _holds(c: Cond, q, bars):
    if c.metric == "price":
        return q.price >= c.value if c.op == "above" else q.price <= c.value
    if c.metric in ("change", "move"):
        if q.change_pct is None:
            return None
        if c.metric == "move":
            return abs(q.change_pct) >= abs(c.value)
        return q.change_pct >= c.value if c.op == "above" else q.change_pct <= c.value
    if c.metric == "volume":
        if not q.volume or not q.avg_volume:
            return None
        return q.volume >= c.value * q.avg_volume
    if bars is None or len(bars) < 30:
        return None
    close = bars.close
    if c.metric == "rsi":
        v = rsi(close)[-1]
        return None if not np.isfinite(v) else (v >= c.value if c.op == "above" else v <= c.value)
    if c.metric == "ma":
        n = int(c.value)
        if len(close) < n + 1:
            return None
        avg = sma(close, n)[-1]
        return close[-1] >= avg if c.op == "above" else close[-1] <= avg
    year = close[-253:-1]  # the year before today's bar
    if c.metric == "high52":
        return close[-1] > np.max(year)
    return close[-1] < np.min(year)


def needs_bars(conds) -> bool:
    return any(c.metric in ("rsi", "ma", "high52", "low52") for c in conds)


def fired_embed(alert: dict, q) -> discord.Embed:
    e = discord.Embed(title=f"🔔 {short(alert['symbol'])}: {clip(alert['text'], 200)}", color=E.GOLD,
                      description=f"**{short(alert['symbol'])}** at **${q.price:,.2f}**" +
                                  (f" ({q.change_pct:+.2f}% today)" if q.change_pct is not None else "") +
                                  "\n" + " · ".join(f"✅ {Cond(**c).text()}" for c in alert["conds"]))
    e.set_footer(text="Your /when alert · it stays on and pings again the next time this happens (/whens to remove)")
    return e


class WhenDesk(Feature):
    name = "when"
    help_group = "🔔 Alerts & tracking"

    def __init__(self, bot):
        super().__init__(bot)
        self.path = bot.data_dir / "when.json"
        self.alerts: list[dict] = read_json(self.path, {}).get("alerts") or []
        self.next_id = max((a["id"] for a in self.alerts), default=0) + 1

    def jobs(self):
        return [("when", 120, self.job)]

    def help(self):
        return [("when", "an alert in plain words: `NVDA drops below 170`, `BTC up 5% today`, `TSLA RSI under 30`"),
                ("whens", "your plain-words alerts (and removing one)")]

    def status(self):
        return [f"**/when alerts** {len(self.alerts)} active"]

    def save(self) -> None:
        write_json(self.path, {"alerts": self.alerts})

    async def understand(self, text: str):
        """(symbol, conditions) or (None, reason)."""
        conds, rest = parse(text)
        engine = self.bot.engine
        sym = None
        for word in symbol_words(rest):
            listing = engine.directory.lookup(word)
            if listing:
                sym = listing.symbol
                break
        if not conds or sym is None:
            ai = getattr(self.bot, "ai", None)
            out = await ai.complete(SYSTEM, text, SCHEMA, "alert", wait=8) if ai is not None and ai.enabled else None
            if out:
                try:
                    found = [Cond(str(c["metric"]), str(c["op"]), float(c["value"])) for c in out.get("conditions")
                             or []]
                except (KeyError, TypeError, ValueError):
                    found = []
                conds = conds or found
                if sym is None and out.get("symbol"):
                    try:
                        sym = (await engine.resolve(str(out["symbol"])[:40])).symbol
                    except Exception:
                        sym = None
        if sym is None:
            return None, "I couldn't tell which stock or coin you mean: write its ticker in capitals, e.g. `NVDA`."
        if not conds:
            return None, ("I couldn't read a condition there. Try: `below 170`, `up 5% today`, `RSI under 30`, "
                          "`above its 200-day`, `new 52-week high` or `volume 3x`.")
        return sym, conds

    async def facts(self, symbols: list[str], with_bars: set[str]):
        quotes = await self.bot.engine.data.quotes(symbols)
        bars = {}
        for s in with_bars:
            if s in quotes:
                try:
                    bars[s] = await self.bot.engine.history(s, quotes[s])
                except Exception as exc:
                    log.info("/when history for %s unavailable: %s", s, exc)
        return quotes, bars

    async def job(self) -> None:
        if not self.alerts:
            return
        now = time.time()
        self.alerts = [a for a in self.alerts if now - a.get("at", now) < STALE_DAYS * 86400 or a.get("fired")]
        symbols = list({a["symbol"] for a in self.alerts})
        with_bars = {a["symbol"] for a in self.alerts if needs_bars([Cond(**c) for c in a["conds"]])}
        quotes, bars = await self.facts(symbols, with_bars)
        changed = False
        for a in self.alerts:
            q = quotes.get(a["symbol"])
            if not q:
                continue
            conds = [Cond(**c) for c in a["conds"]]
            if market_of(a["symbol"]) == STOCKS and q.market_state not in ("REGULAR", "POST", "POSTPOST", "") and \
                    any(c.metric in ("change", "move", "volume") for c in conds):
                continue  # before the open these are still yesterday's
            states = [holds(c, q, bars.get(a["symbol"])) for c in conds]
            if any(s is None for s in states):
                continue
            now_true = all(states)
            if now_true and not a.get("on"):
                await self.ping(a, q)
                a["fired"] = a.get("fired", 0) + 1
                a["last_fired"] = now
            if bool(a.get("on")) != now_true:
                a["on"] = now_true
                changed = True
        if changed:
            self.save()

    async def ping(self, a: dict, q) -> None:
        embed = fired_embed(a, q)
        try:
            if a.get("dm"):
                user = self.bot.get_user(a["user"]) or await self.bot.fetch_user(a["user"])
                await user.send(embed=embed)
            else:
                await self.bot.send(a["channel"], Post([embed], content=f"<@{a['user']}>"))
        except Exception:
            log.warning("Couldn't deliver /when alert %s", a.get("id"), exc_info=True)

    def register(self, tree) -> None:
        @tree.command(name="when", description="An alert in plain words, e.g. NVDA drops below 170, BTC up 5% today")
        @app_commands.describe(text="What to watch for, e.g. 'TSLA RSI under 30' or 'SPY crosses above its 200-day'")
        async def when_cmd(interaction: discord.Interaction, text: app_commands.Range[str, 3, 200]):
            await interaction.response.defer(thinking=True, ephemeral=interaction.guild is not None)
            uid = interaction.user.id
            if sum(a["user"] == uid for a in self.alerts) >= PER_USER:
                await interaction.followup.send(f"You have {PER_USER} alerts already; remove one with `/whens`.")
                return
            sym, conds = await self.understand(text)
            if sym is None:
                await interaction.followup.send(conds)
                return
            quotes, bars = await self.facts([sym], {sym} if needs_bars(conds) else set())
            q = quotes.get(sym)
            if q is None:
                await interaction.followup.send(f"I couldn't get a price for {short(sym)} right now; try again soon.")
                return
            for c in conds:
                if c.op == "touch":  # "hits 200": whichever side the price has to travel to get there
                    c.op = "above" if c.value >= q.price else "below"
            conds = [c for c in conds if valid(c)]
            if not conds:
                await interaction.followup.send("Those numbers don't look right; try e.g. `NVDA below 170`.")
                return
            states = [holds(c, q, bars.get(sym)) for c in conds]
            on = all(s is True for s in states)
            a = {"id": self.next_id, "user": uid, "channel": interaction.channel_id, "dm": interaction.guild is None,
                 "symbol": sym, "text": text.strip(), "conds": [c.__dict__ for c in conds], "on": on,
                 "at": time.time()}
            self.next_id += 1
            self.alerts.append(a)
            self.save()
            what = " and ".join(c.text() for c in conds)
            note = (" That's true right now, so I'll ping you the next time it happens." if on else "")
            where = "here" if not a["dm"] else "by DM"
            await interaction.followup.send(f"🔔 Watching **{short(sym)}**: {what}. I'll ping you {where} "
                                            f"(#{a['id']}).{note}")

        @tree.command(name="whens", description="Your plain-words alerts; remove one by its number")
        @app_commands.describe(remove="The alert's number to remove")
        async def whens_cmd(interaction: discord.Interaction, remove: int | None = None):
            uid = interaction.user.id
            if remove is not None:
                mine = [a for a in self.alerts if a["id"] == remove and a["user"] == uid]
                if not mine:
                    await interaction.response.send_message(f"You have no alert #{remove}.", ephemeral=True)
                    return
                self.alerts.remove(mine[0])
                self.save()
                await interaction.response.send_message(f"Removed #{remove}.", ephemeral=True)
                return
            mine = [a for a in self.alerts if a["user"] == uid]
            e = discord.Embed(title="🔔 Your /when alerts", color=E.BLUE)
            e.description = "\n".join(
                f"`#{a['id']}` **{short(a['symbol'])}**: " + " and ".join(Cond(**c).text() for c in a["conds"])
                + (f" · fired {a['fired']}×" if a.get("fired") else "") for a in mine) or \
                "None yet. Try `/when NVDA drops below 170` or `/when bitcoin is up 5% today`."
            await interaction.response.send_message(embed=fit_embed(e), ephemeral=True)
