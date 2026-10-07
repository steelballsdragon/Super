"""The scheduled posts: pre-market brief, closing recap, crypto daily, research digest, weekly outlook and the
morning headlines."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import discord

from . import charts, embeds as E, stats
from .engine import Outlook
from .hours import NEW_YORK
from .limits import fit_embed
from .news import Analysis, impact_text
from .setups import fmt_price
from .universe import BOARD_LABELS, CRYPTO, FUTURES, INDICES, MACRO, SECTORS, STOCKS, short, title_of

log = logging.getLogger(__name__)


@dataclass
class Post:
    embeds: list[discord.Embed]
    files: list[tuple[str, bytes]] = field(default_factory=list)
    content: str | None = None


def chart_post(o: Outlook, embed: discord.Embed, trigger: float | None = None) -> Post:
    """An outlook embed with its chart attached and shown inside the embed."""
    name = f"{short(o.symbol).replace('^', '')}_chart.png".lower()
    png = charts.price_chart(o.bars, title_of(o.symbol, o.name), o.cone, o.resistance, o.support, trigger)
    embed.set_image(url=f"attachment://{name}")
    return Post([embed], [(name, png)])


def top_news(bot, hours: float, markets: set[str], count: int) -> list[Analysis]:
    cutoff = time.time() - hours * 3600
    items = [a for a in bot.recent_news if a.headline.published >= cutoff and a.market in markets
             and not a.opinion and a.importance >= 40]
    items.sort(key=lambda a: -a.importance)
    return items[:count]


def compact_outlook(o: Outlook) -> str:
    parts = [f"**{o.label}** technicals ({o.score:+d})", f"higher in a week {o.up[5]:.0%}, a month {o.up[20]:.0%}"]
    if o.breakout_up is not None:
        if o.breakout_up - o.base.get("breakout_up", 0) >= o.breakout_down - o.base.get("breakout_down", 0):
            parts.append(f"{o.breakout_up:.0%} odds of clearing {fmt_price(o.hi20)} in 10 sessions")
        else:
            parts.append(f"{o.breakout_down:.0%} odds of losing {fmt_price(o.lo20)} in 10 sessions")
    if o.cone and 5 in o.cone.horizons:
        d = o.cone.horizons[5]
        parts.append(f"1-week range {fmt_price(d['p5'])}–{fmt_price(d['p95'])}")
    return " · ".join(parts)


async def _outlooks(bot, symbols: list[tuple[str, str]], record: bool = True) -> list[Outlook]:
    found = await asyncio.gather(*(bot.engine.outlook(s, m, quote=bot.quotes.get(s)) for s, m in symbols),
                                 return_exceptions=True)
    out = []
    for o in found:
        if isinstance(o, Outlook):
            out.append(o)
            if record:
                bot.predictions.add_forecast(o.symbol, o.market, o.price, o.up,
                                             {5: o.base.get("up_5d", 0.5), 20: o.base.get("up_20d", 0.5)}, o.label)
        else:
            log.warning("Brief outlook failed: %r", o)
    return out


async def premarket(bot, watchlist: list[str]) -> list[Post]:
    now = datetime.now(NEW_YORK)
    quotes = await bot.engine.data.quotes([a.symbol for a in FUTURES + MACRO] + ["BTC-USD", "^VIX"] + watchlist)
    bot.quotes.update(quotes)
    e = discord.Embed(title=f"🌅 Pre-market brief · {now:%A, %B %-d}", color=E.BLUE)
    e.add_field(name="Futures", value=E.ansi_rows([(BOARD_LABELS.get(a.symbol, a.name)[:10], getattr(quotes.get(a.symbol), "price", None),
                                                    getattr(quotes.get(a.symbol), "change_pct", None), "") for a in FUTURES], 10),
                inline=False)
    e.add_field(name="Overnight in other markets", value=E.ansi_rows(
        [(BOARD_LABELS.get(a.symbol, a.name)[:10], getattr(quotes.get(a.symbol), "price", None),
          getattr(quotes.get(a.symbol), "change_pct", None), "") for a in MACRO]
        + [("Bitcoin", getattr(quotes.get("BTC-USD"), "price", None), getattr(quotes.get("BTC-USD"), "change_pct", None), "")], 10),
        inline=False)
    fg = bot.macro_cache.stock_fg if bot.macro_cache else None
    if fg is not None:
        then = [f"{name} {v:.0f}" for name, v in (("prev close", fg.close), ("week ago", fg.week)) if v is not None]
        e.add_field(name="Fear & Greed", value=E.fear_greed_text(fg) + f" ({fg.source}"
                    + (f" · {' · '.join(then)})" if then else ")"), inline=False)
    movers = [(s, q) for s, q in ((s, quotes.get(s)) for s in watchlist) if q and q.ext_change_pct is not None]
    movers.sort(key=lambda x: -abs(x[1].ext_change_pct))
    if movers:
        e.add_field(name="Watchlist pre-market movers", value=" · ".join(
            f"{E.arrow(q.ext_change_pct)} **{short(s)}** {q.ext_change_pct:+.1f}%" for s, q in movers[:8]), inline=False)
    earnings = []
    for s in watchlist:
        q = quotes.get(s)
        t = (q.extra.get("earningsTimestamp") or q.extra.get("earningsTimestampStart")) if q else None
        if t and 0 <= t - time.time() < 2 * 86400:
            earnings.append(f"**{short(s)}** {E.ts(t, 'R')}")
    if earnings:
        e.add_field(name="Earnings coming up", value=" · ".join(earnings), inline=False)
    news = top_news(bot, 16, {"stocks", "macro"}, 5)
    if news:
        e.add_field(name="Overnight news that matters", value="\n".join(
            f"{a.emoji} {E.headline_link(a, 100)}"
            + (f"\n-# {' · '.join(impact_text(i).replace('**', '') for i in a.impacts[:2])}" if a.impacts else "")
            for a in news), inline=False)
    outs = await _outlooks(bot, [("^GSPC", STOCKS), ("^IXIC", STOCKS)])
    for o in outs:
        e.add_field(name=f"{o.name} outlook", value=compact_outlook(o), inline=False)
    e.set_footer(text=E.DISCLAIMER)
    posts = [Post([fit_embed(e)])]
    if outs:
        posts.append(chart_post(outs[0], E.outlook_embed(outs[0], "📊"), outs[0].hi20))
    return posts


async def close_recap(bot, watchlist: list[str]) -> list[Post]:
    now = datetime.now(NEW_YORK)
    syms = [a.symbol for a in INDICES] + list(SECTORS) + watchlist + ["^TNX", "DX-Y.NYB", "GC=F", "CL=F"]
    quotes = await bot.engine.data.quotes(syms)
    bot.quotes.update(quotes)
    sp = quotes.get("^GSPC")
    e = discord.Embed(title=f"🔔 Closing recap · {now:%A, %B %-d}",
                      color=E.GREEN if sp and (sp.change_pct or 0) >= 0 else E.RED)
    e.add_field(name="Indices", value=E.ansi_rows([(BOARD_LABELS.get(a.symbol, a.name)[:10], getattr(quotes.get(a.symbol), "price", None),
                                                    getattr(quotes.get(a.symbol), "change_pct", None), "") for a in INDICES], 10),
                inline=False)
    sectors = sorted(((n, quotes[s].change_pct) for s, n in SECTORS.items() if s in quotes and quotes[s].change_pct is not None),
                     key=lambda x: -x[1])
    if sectors:
        e.add_field(name="Sectors (best to worst)", value=" · ".join(f"{n} {c:+.1f}%" for n, c in sectors), inline=False)
    wl = sorted(((s, quotes[s]) for s in watchlist if s in quotes and quotes[s].change_pct is not None),
                key=lambda x: -x[1].change_pct)
    if wl:
        best = " · ".join(f"🟢 **{short(s)}** {q.change_pct:+.1f}%" for s, q in wl[:4] if q.change_pct > 0)
        worst = " · ".join(f"🔴 **{short(s)}** {q.change_pct:+.1f}%" for s, q in wl[::-1][:4] if q.change_pct < 0)
        up = sum(1 for _, q in wl if q.change_pct > 0)
        e.add_field(name=f"Watchlist ({up}/{len(wl)} up)", value="\n".join(x for x in (best, worst) if x) or "Flat",
                    inline=False)
    try:
        gainers = await bot.engine.data.screener("day_gainers", 6)
        losers = await bot.engine.data.screener("day_losers", 6)

        def row(q):
            return f"**{q.get('symbol')}** {q.get('regularMarketChangePercent', 0):+.1f}%"
        e.add_field(name="Biggest US movers", value="🟢 " + " · ".join(row(q) for q in gainers[:5])
                    + "\n🔴 " + " · ".join(row(q) for q in losers[:5]), inline=False)
    except Exception:
        log.warning("Screener unavailable for the recap", exc_info=True)
    fired = [h for h in bot.last_scan.get(STOCKS, []) if h.setups]
    if fired:
        e.add_field(name="Setups on the watchlist today", value=" · ".join(
            f"{h.setups[0].emoji} **{short(h.symbol)}** {h.setups[0].name}" for h in fired[:6]), inline=False)
    if bot.macro_cache and bot.macro_cache.mood is not None:
        m = bot.macro_cache.mood
        e.add_field(name="Market mood", value=f"{m:.0f}/100 · {E.mood_label(m)}", inline=True)
    vix = quotes.get("^VIX")
    if vix:
        e.add_field(name="VIX", value=f"{vix.price:.2f} ({E.pct(vix.change_pct, 1, already_pct=True)})", inline=True)
    outs = await _outlooks(bot, [("^GSPC", STOCKS)])
    for o in outs:
        e.add_field(name="S&P 500 from here", value=compact_outlook(o), inline=False)
    e.set_footer(text=E.DISCLAIMER)
    return [Post([fit_embed(e)])]


async def crypto_daily(bot, watchlist: list[str]) -> list[Post]:
    quotes = await bot.engine.data.quotes(watchlist + ["BTC-USD", "ETH-USD"])
    bot.quotes.update(quotes)
    e = discord.Embed(title=f"🪙 Crypto daily · {datetime.now(NEW_YORK):%A, %B %-d}", color=E.BLUE)
    try:
        cg = await bot.engine.sources.crypto_global()
        e.add_field(name="Market", value=f"Total ${E.big(cg.total_cap)} ({cg.cap_change_24h:+.1f}% 24h) · BTC dominance "
                                         f"{cg.btc_dominance:.1f}% · stablecoins {cg.stable_dominance:.1f}%", inline=False)
    except Exception:
        pass
    m = bot.macro_cache
    if m and m.crypto_fng is not None:
        txt = f"**{m.crypto_fng:.0f} {E.fear_greed_label(m.crypto_fng)}**"
        if m.fng_history:
            txt += (f" · after similar readings BTC was higher 30 days later {m.fng_history['up']:.0%} of the time "
                    f"(median {E.pct(m.fng_history['median'], 1)})")
        e.add_field(name="Fear & Greed", value=txt, inline=False)
    try:
        coins = await bot.engine.sources.top_coins(100)
        movers = sorted((c for c in coins if c.change_24h is not None and c.symbol not in ("USDT", "USDC", "DAI", "USDE")),
                        key=lambda c: -c.change_24h)
        e.add_field(name="Top-100 movers (24h)", value="🟢 " + " · ".join(f"**{c.symbol}** {c.change_24h:+.1f}%" for c in movers[:5])
                    + "\n🔴 " + " · ".join(f"**{c.symbol}** {c.change_24h:+.1f}%" for c in movers[::-1][:5]), inline=False)
    except Exception:
        log.warning("CoinGecko unavailable for the crypto brief", exc_info=True)
    news = top_news(bot, 24, {"crypto"}, 4)
    if news:
        e.add_field(name="News that matters", value="\n".join(
            f"{a.emoji} {E.headline_link(a, 100)}" for a in news), inline=False)
    outs = await _outlooks(bot, [("BTC-USD", CRYPTO), ("ETH-USD", CRYPTO)])
    for o in outs:
        e.add_field(name=f"{o.name} outlook", value=compact_outlook(o), inline=False)
    try:
        btc = await bot.engine.cache.daily("BTC-USD")
        hv = stats.halving_cycle(btc)
        e.add_field(name="Halving cycle", value=f"Day {hv['days_since']} after the {hv['last']:%b %Y} halving · "
                                                f"next ≈ {hv['next_est']:%b %Y}", inline=False)
    except Exception:
        pass
    e.set_footer(text=E.DISCLAIMER)
    posts = [Post([fit_embed(e)])]
    if outs:
        posts.append(chart_post(outs[0], E.outlook_embed(outs[0], "📊"), outs[0].hi20))
    return posts


async def research_digest(bot, stock_list: list[str], crypto_list: list[str]) -> list[Post]:
    quotes = await bot.engine.data.quotes(stock_list + crypto_list + list(SECTORS))
    bot.quotes.update(quotes)
    hits = await bot.engine.scan(stock_list + list(SECTORS) + crypto_list, quotes, lookback=3)
    e = E.scan_embed(hits, "all", f"🔬 Research digest · {datetime.now(NEW_YORK):%A, %B %-d}")
    e.description = ("The strongest setups across the watchlists and sector ETFs, ranked by breakout pressure. "
                     "Deep dives on the top two follow.\n\n" + (e.description or ""))
    posts = [Post([fit_embed(e)])]
    top = [(h.symbol, h.market) for h in hits[:2] if h.pressure > 0]
    for o in await _outlooks(bot, top):
        posts.append(chart_post(o, E.outlook_embed(o), o.hi20))
    return posts


async def weekly(bot) -> list[Post]:
    posts = []
    intro = discord.Embed(title=f"🗓️ Week ahead · {datetime.now(NEW_YORK):%B %-d, %Y}", color=E.PURPLE)
    try:
        macro = await bot.engine.macro()
        bot.macro_cache = macro
        lr = await bot.engine.long_run()
        cape = None
        if lr and "^GSPC" in macro.quotes:
            now = datetime.now(NEW_YORK)
            cape = stats.cape_view(lr, macro.quotes["^GSPC"].price, now.year + (now.timetuple().tm_yday - 1) / 365.25)
        posts.append(Post([E.macro_embed(macro, cape)]))
    except Exception:
        log.warning("Macro unavailable for the weekly", exc_info=True)
    month = datetime.now(NEW_YORK).month
    try:
        sp = await bot.engine.cache.daily("^GSPC")
        seasons = {s.month: s for s in stats.seasonality(sp)}
        s = seasons.get(month)
        if s:
            intro.add_field(name=f"{stats.MONTHS[month - 1]} since 1928",
                            value=f"S&P 500 average {E.pct(s.avg, 1)}, higher in {s.up:.0%} of {s.n} years", inline=False)
        yr = datetime.now(NEW_YORK).year
        pres = stats.presidential_cycle(stats.calendar_years(sp))
        k = stats.presidential_year(yr)
        if k in pres:
            intro.add_field(name=f"{stats.PRESIDENTIAL_NAMES[k]} ({yr})",
                            value=f"Average S&P 500 year {E.pct(pres[k]['avg'], 1)}, higher {pres[k]['up']:.0%} of the time",
                            inline=False)
    except Exception:
        pass
    news = top_news(bot, 72, {"stocks", "macro", "crypto"}, 6)
    if news:
        intro.add_field(name="The week's biggest stories", value="\n".join(
            f"{a.emoji} {E.headline_link(a, 100)}" for a in news), inline=False)
    intro.set_footer(text=E.DISCLAIMER)
    posts.insert(0, Post([fit_embed(intro)]))
    for o in await _outlooks(bot, [("^GSPC", STOCKS), ("^IXIC", STOCKS), ("BTC-USD", CRYPTO), ("ETH-USD", CRYPTO)]):
        posts.append(chart_post(o, E.outlook_embed(o), o.hi20))
    return posts


async def morning_news(bot) -> list[Post]:
    items = top_news(bot, 16, {"stocks", "macro", "crypto"}, 10)
    if not items:
        return []
    return [Post([E.news_digest(items, f"🗞️ Morning headlines · {datetime.now(NEW_YORK):%A, %B %-d}")])]


# ----- the trends channel -----

def _members(bot) -> tuple[list[str], list[str]]:
    return bot.trends.index_members("sp500"), bot.trends.index_members("ndx100")


async def trends_recap(bot, period: str) -> list[Post]:
    """The day's (1D), week's (1W) or month's (1M) movers: stocks, sectors and crypto. The monthly recap adds the
    year-to-date leaders."""
    snap = await bot.trends.refresh(max_age=120)
    sp500, ndx = _members(bot)
    when = datetime.now(NEW_YORK)
    title = {"1D": f"📅 Daily trends · {when:%a %b %d}", "1W": f"🗓️ Weekly trends · week of {when:%b %d}",
             "1M": f"📆 Monthly trends · {when:%B %Y}"}.get(period, "Trends")
    calendar = {"1W": "WTD", "1M": "MTD"}.get(period, period)  # the recaps cover the calendar week and month
    embeds = [E.trends_embed(snap, calendar, "stocks", sp500, ndx),
              E.trends_embed(snap, calendar, "sectors", sp500, ndx)]
    embeds[0].title = f"{title} · {embeds[0].title}"
    if period in ("1W", "1M"):
        embeds.append(E.trends_embed(snap, period, "crypto", sp500, ndx))
    if period == "1M":
        embeds.append(E.trends_embed(snap, "YTD", "stocks", sp500, ndx, count=5))
    return [Post(_fit_total(embeds))]


async def crypto_trends(bot) -> list[Post]:
    snap = await bot.trends.refresh(max_age=120)
    sp500, ndx = _members(bot)
    e = E.trends_embed(snap, "1D", "crypto", sp500, ndx)
    e.title = f"🪙 Crypto daily trends · {datetime.now(NEW_YORK):%a %b %d} · last 24 hours"
    return [Post([e])]


def _fit_total(embeds: list[discord.Embed], limit: int = 5800) -> list[discord.Embed]:
    """Discord allows 6,000 characters across a message's embeds: drop trailing ones that don't fit."""
    out, total = [], 0
    for e in embeds:
        if total + len(e) > limit and out:
            break
        out.append(e)
        total += len(e)
    return out


# ----- the NVIDIA channel -----

async def nvidia_brief(bot, kind: str) -> list[Post]:
    """Before the open: the outlook and the latest from Massive. After the close: how the day went."""
    q = (await bot.engine.data.quotes(["NVDA"])).get("NVDA") or bot.quotes.get("NVDA")
    board = E.nvidia_board(q, bot.spotlight, (bot.massive_used(), 5))
    board.title = ("🌅 NVIDIA before the open" if kind == "premarket" else "🔔 NVIDIA at the close")
    posts = [Post([board])]
    try:
        o = await bot.engine.outlook("NVDA", STOCKS, quote=q, name="NVIDIA Corporation")
    except Exception:
        log.warning("NVIDIA outlook failed", exc_info=True)
        return posts
    bot.predictions.add_forecast(o.symbol, o.market, o.price, o.up,
                                 {5: o.base.get("up_5d", 0.5), 20: o.base.get("up_20d", 0.5)}, o.label)
    posts.append(await bot.engine.run(chart_post, o, E.outlook_embed(o), o.hi20))
    return posts
