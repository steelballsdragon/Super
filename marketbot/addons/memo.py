"""The weekly memo, posted to the research channel after each week's last close: how stocks, bonds, the dollar,
gold, oil and crypto did, the best and worst sectors, the week's biggest headlines and next week's big events,
summed up by the free AI reader (a plain summary when no reader is free). `/memo` shows the latest one.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

import discord

from .. import embeds as E
from ..briefs import Post
from ..hours import NEW_YORK, is_trading_day, next_trading_day
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from . import Feature

log = logging.getLogger(__name__)

KIND = "research"
ASSETS = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^RUT": "Russell 2000", "^TNX": "10-year yield",
          "DX-Y.NYB": "Dollar", "GC=F": "Gold", "CL=F": "Oil", "BTC-USD": "Bitcoin", "ETH-USD": "Ether",
          "^VIX": "VIX"}
SECTORS = {"XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health care", "XLY": "Consumer disc.",
           "XLP": "Staples", "XLI": "Industrials", "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate",
           "XLC": "Communication"}
LEVELS = {"^TNX", "^VIX"}  # shown as a level change, not a percent
KEEP_NEWS = 40

SYSTEM = ("You write a short weekly market memo for a Discord server of retail investors, from the facts given "
          "(weekly moves in percent, the 10-year yield and VIX in points, sectors, the week's top headlines and next "
          "week's events). Plain words, no hype, no advice, no facts beyond those given. Reply with JSON only.")
SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string", "description": "a headline for the week, under 80 characters"},
    "summary": {"type": "string", "description": "2-3 sentences: what happened and why"},
    "points": {"type": "array", "items": {"type": "string"}, "description": "3-5 short takeaways"},
    "watch": {"type": "array", "items": {"type": "string"}, "description": "2-4 things to watch next week"}},
    "required": ["title", "summary", "points", "watch"]}


def week_move(bars, now: float, level: bool = False) -> float | None:
    """The change since the last close at least 7 days ago: percent, or points for a level."""
    if bars is None or len(bars) < 10:
        return None
    then = now - 7 * 86400
    i = int((bars.t <= then).sum()) - 1
    if i < 0 or not bars.close[i]:
        return None
    last = float(bars.close[-1])
    return last - float(bars.close[i]) if level else (last / float(bars.close[i]) - 1) * 100


def fmt(sym: str, v: float) -> str:
    if sym == "^TNX":
        return f"{v * 100:+.0f} bp"
    if sym in LEVELS:
        return f"{v:+.1f} pts"
    return f"{v:+.1f}%"


def template(facts: dict) -> dict:
    moves = facts["markets"]
    spx = facts.get("spx")
    sectors = facts["sectors"]
    title = ("Stocks rose this week" if spx and spx > 0.3 else "Stocks fell this week" if spx and spx < -0.3 else
             "A flat week for stocks")
    lines = [f"{k} {v}" for k, v in list(moves.items())[:6]]
    summary = "This week: " + ", ".join(lines) + "."
    points = []
    if sectors:
        points.append(f"Best sector: {sectors[0][0]} ({sectors[0][1]}); worst: {sectors[-1][0]} ({sectors[-1][1]})")
    points += [h for h in facts["headlines"][:3]]
    return {"title": title, "summary": summary, "points": points, "watch": facts["next_week"][:4]}


def memo_embed(memo: dict, facts: dict, ai: bool) -> discord.Embed:
    e = discord.Embed(title=f"🗞️ Weekly memo: {clip(memo.get('title') or 'the week', 200)}", color=E.BLUE,
                      description=clip(str(memo.get("summary") or ""), 1500))
    if facts["markets"]:
        e.add_field(name="The week", value=" · ".join(f"**{k}** {v}" for k, v in facts["markets"].items()),
                    inline=False)
    if facts["sectors"]:
        e.add_field(name="Sectors", value=" · ".join(f"{n} {v}" for n, v in facts["sectors"]), inline=False)
    pts = [clip(str(p), 200) for p in memo.get("points") or [] if str(p).strip()][:5]
    if pts:
        e.add_field(name="Takeaways", value="\n".join(f"• {p}" for p in pts), inline=False)
    watch = [clip(str(w), 200) for w in memo.get("watch") or [] if str(w).strip()][:4]
    if watch:
        e.add_field(name="Next week", value="\n".join(f"👀 {w}" for w in watch), inline=False)
    e.set_footer(text=("Written by a free AI reader from the facts above" if ai else "Summary from the facts above")
                 + " · not financial advice")
    return fit_embed(e)


class MemoDesk(Feature):
    name = "memo"
    help_group = "🔬 Analysis"

    def __init__(self, bot):
        super().__init__(bot)
        self.path = bot.data_dir / "memo.json"
        data = read_json(self.path, {})
        self.news: list[dict] = data.get("news") or []
        self.latest: dict | None = data.get("latest")

    def jobs(self):
        return [("memo", 900, self.job)]

    def help(self):
        return [("memo", "the weekly market memo: the week in a few lines and what to watch next")]

    def save(self) -> None:
        write_json(self.path, {"news": self.news, "latest": self.latest})

    def collect_news(self, now: float) -> None:
        """The week's biggest headlines (the bot keeps only three days, the memo needs seven)."""
        have = {n["title"] for n in self.news}
        for a in getattr(self.bot, "recent_news", []) or []:
            if a.opinion or a.importance < 55 or a.headline.title in have:
                continue
            self.news.append({"title": a.headline.title[:200], "importance": a.importance,
                              "at": a.headline.published or now})
            have.add(a.headline.title)
        self.news = sorted((n for n in self.news if now - n["at"] < 7 * 86400),
                           key=lambda n: -n["importance"])[:KEEP_NEWS]

    async def facts(self, now: float) -> dict:
        markets, sectors, spx = {}, [], None
        cache = self.bot.engine.cache
        for sym, name in list(ASSETS.items()) + list(SECTORS.items()):
            try:
                bars = await cache.daily(sym, fresh=1800)
            except Exception as exc:
                log.info("Memo: no history for %s (%s)", sym, exc)
                continue
            v = week_move(bars, now, level=sym in LEVELS)
            if v is None:
                continue
            if sym == "^GSPC":
                spx = round(v, 2)
            if sym in SECTORS:
                sectors.append((name, v))
            else:
                markets[name] = fmt(sym, v)
        sectors.sort(key=lambda s: -s[1])
        nxt = []
        cal = next((f for f in getattr(self.bot, "features", []) if f.name == "calendar"), None)
        if cal is not None:
            start = datetime.fromtimestamp(now, NEW_YORK).date() + timedelta(days=1)
            try:
                items = await cal.schedule(start, 7)
                nxt = [f"{date.fromisoformat(i.day):%a}: {i.title}" + (" earnings" if i.key == "earnings" else "")
                       for i in items if i.importance >= 2 or i.key == "earnings"][:10]
            except Exception as exc:
                log.info("Memo: next week's calendar unavailable: %s", exc)
        return {"markets": markets, "spx": spx, "sectors": [(n, f"{v:+.1f}%") for n, v in sectors],
                "headlines": [n["title"] for n in self.news[:12]], "next_week": nxt}

    async def write(self, now: float | None = None) -> tuple[dict, dict, bool]:
        now = now or time.time()
        facts = await self.facts(now)
        ai = getattr(self.bot, "ai", None)
        memo = None
        if ai is not None and ai.enabled:
            memo = await ai.complete(SYSTEM, json.dumps(facts), SCHEMA, "memo", wait=20)
        used_ai = bool(memo and memo.get("summary"))
        memo = memo if used_ai else template(facts)
        self.latest = {"title": memo.get("title"), "summary": memo.get("summary"), "points": memo.get("points"),
                       "watch": memo.get("watch"), "facts": facts, "ai": used_ai, "at": now}
        self.save()
        return memo, facts, used_ai

    async def job(self) -> None:
        now = time.time()
        self.collect_news(now)
        self.save()
        ny = datetime.now(NEW_YORK)
        d = ny.date()
        if not is_trading_day(d) or next_trading_day(d).isocalendar()[1] == d.isocalendar()[1]:
            return
        channels = [(cid, cfg) for cid, cfg in self.bot.channels.of_kind(KIND) if cfg.briefs]
        due = [cid for cid, _ in channels if self.bot._due(cid, "memo", ny, 16, 40, 240)]
        if not due:
            return
        memo, facts, used_ai = await self.write(now)
        embed = memo_embed(memo, facts, used_ai)
        for cid in due:
            await self.bot.send(cid, Post([embed]))

    def register(self, tree) -> None:
        @tree.command(name="memo", description="The weekly market memo: the week in a few lines and what's next")
        async def memo_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            latest = self.latest
            if not latest or time.time() - latest.get("at", 0) > 3 * 86400:
                memo, facts, used_ai = await self.write()
            else:
                memo, facts, used_ai = latest, latest["facts"], latest.get("ai", False)
            stamp = datetime.fromtimestamp(self.latest["at"], timezone.utc) if self.latest else None
            embed = memo_embed(memo, facts, used_ai)
            if stamp:
                embed.timestamp = stamp
            await interaction.followup.send(embed=embed)
