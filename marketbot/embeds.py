"""Discord embeds for every post and command. Every builder returns embeds trimmed to Discord's limits."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import discord
import numpy as np

from . import stats
from .engine import Macro, OptionsView, Outlook, ScanHit, mood_label
from .features import FEATURE_LABELS
from .limits import clip, fit_embed
from .model import Backtest, MarketModel
from .news import Analysis, impact_text
from .setups import DEFS, SetupStats, fmt_price
from .sources import CryptoGlobal, fear_greed_label
from .universe import BOARD_LABELS, CRYPTO, SECTORS, STOCKS, display_name, short, tag, title_of
from .yahoo import Quote

GREEN, RED, BLUE, GOLD, GREY, PURPLE = 0x0CA30C, 0xD03B3B, 0x3987E5, 0xC98500, 0x5C5C58, 0x9085E9
DISCLAIMER = "Probabilities from history, not certainties · not financial advice"
ANSI_GREEN, ANSI_RED, ANSI_GREY, ANSI_RESET = "\u001b[0;32m", "\u001b[0;31m", "\u001b[0;30m", "\u001b[0m"


def ts(t: float, style: str = "R") -> str:
    return f"<t:{int(t)}:{style}>"


def pct(v: float | None, digits: int = 2, already_pct: bool = False) -> str:
    if v is None or not np.isfinite(v):
        return "—"
    v = v if already_pct else v * 100
    return f"{v:+.{digits}f}%"


def arrow(v: float | None) -> str:
    if v is None or not np.isfinite(v) or v == 0:
        return "⚪"
    return "🟢" if v > 0 else "🔴"


def big(v: float | None) -> str:
    if v is None:
        return "—"
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.0f}"


def money(v: float | None, symbol: str = "") -> str:
    if v is None:
        return "—"
    if symbol.startswith("^") and symbol not in ("^TNX", "^IRX", "^TYX", "^FVX"):
        return fmt_price(v)  # index points, not dollars
    if symbol in ("^TNX", "^IRX", "^TYX", "^FVX"):
        return f"{v:.3f}%"
    return "$" + fmt_price(v)


def state_label(q: Quote | None) -> str:
    s = (q.market_state if q else "") or ""
    return {"REGULAR": "🟢 Market open", "PRE": "🌅 Pre-market", "PREPRE": "🌙 Overnight", "POST": "🌙 After hours",
            "POSTPOST": "🌙 After hours", "CLOSED": "⚫ Market closed"}.get(s, "")


SOURCE_NAMES = {"Yahoo": "Yahoo Finance", "Nasdaq": "Nasdaq", "Coinbase": "Coinbase", "CoinGecko": "CoinGecko"}


def sources_note(quotes: dict[str, Quote]) -> str:
    """Where the prices came from, e.g. "Yahoo Finance" or "Nasdaq, Coinbase (backups: Yahoo isn't answering)"."""
    used = list(dict.fromkeys(q.source for q in quotes.values() if q))
    if not used or used == ["Yahoo"]:
        return "Yahoo Finance"
    names = [SOURCE_NAMES.get(u, u) for u in used]
    return ", ".join(names) + ("" if "Yahoo" in used else " (backups: Yahoo isn't answering)")


def ansi_rows(rows: list[tuple[str, float | None, float | None, str]], width: int = 7) -> str:
    """A monospace table with green/red rows (colour shows on desktop; arrows carry it everywhere)."""
    lines = []
    for label, price, change, extra in rows:
        if price is None:
            continue
        color = ANSI_GREEN if (change or 0) > 0 else ANSI_RED if (change or 0) < 0 else ANSI_GREY
        sign = "▲" if (change or 0) > 0 else "▼" if (change or 0) < 0 else "•"
        ch = f"{change:+.2f}%" if change is not None else "   —  "
        lines.append(f"{color}{sign} {label[:width]:<{width}}{fmt_price(price):>11} {ch:>8}{extra}{ANSI_RESET}")
    return "```ansi\n" + "\n".join(lines) + "\n```" if lines else "—"


# ----- live boards -----

def stocks_board(quotes: dict[str, Quote], indices, futures, macro, watchlist: list[str], mood: float | None,
                 updated: float) -> discord.Embed:
    lead = quotes.get("^GSPC")
    state = state_label(lead)
    chg = lead.change_pct if lead else None
    e = discord.Embed(title="📈 US Stock Market · Live", color=GREEN if (chg or 0) >= 0 else RED,
                      description=f"{state} · updated {ts(updated)}" + (
                          f"\nMarket mood: **{mood:.0f}/100 {mood_label(mood)}**" if mood is not None else ""))
    def rows_for(assets):
        return [(BOARD_LABELS.get(a.symbol, a.name)[:10], getattr(quotes.get(a.symbol), "price", None),
                 getattr(quotes.get(a.symbol), "change_pct", None), "") for a in assets]

    e.add_field(name="Indices", value=ansi_rows(rows_for(indices), 10), inline=False)
    if lead and lead.market_state != "REGULAR":
        e.add_field(name="Futures", value=ansi_rows(rows_for(futures), 10), inline=False)
    e.add_field(name="Rates, dollar & commodities", value=ansi_rows(rows_for(macro), 10), inline=False)
    rows = []
    for sym in watchlist:
        q = quotes.get(sym)
        if not q:
            continue
        extra = ""
        if q.ext_price and q.ext_change_pct is not None and q.market_state != "REGULAR":
            extra = f" {'pre' if q.market_state.startswith('PRE') else 'ah'} {q.ext_change_pct:+.1f}%"
        rows.append((short(sym), q.price, q.change_pct, extra))
    for i, chunk in enumerate(_chunks(rows, 16)):
        e.add_field(name="Watchlist" if i == 0 else "Watchlist (cont.)", value=ansi_rows(chunk), inline=False)
    e.set_footer(text=f"Prices: {sources_note(quotes)} (may be delayed) · /watchlist to change · /forecast for any symbol")
    return fit_embed(e)


def crypto_board(quotes: dict[str, Quote], watchlist: list[str], cg: CryptoGlobal | None, fng: float | None,
                 coins: dict[str, object], updated: float) -> discord.Embed:
    btc = quotes.get("BTC-USD")
    chg = btc.change_pct if btc else None
    desc = [f"🟢 Trading 24/7 · updated {ts(updated)}"]
    if cg:
        desc.append(f"Total market cap **${big(cg.total_cap)}** ({cg.cap_change_24h:+.2f}% 24h) · "
                    f"BTC dominance **{cg.btc_dominance:.1f}%** · ETH {cg.eth_dominance:.1f}% · "
                    f"stablecoins {cg.stable_dominance:.1f}%")
    if fng is not None:
        desc.append(f"Fear & Greed: **{fng:.0f} {fear_greed_label(fng)}**")
    e = discord.Embed(title="🪙 Crypto Market · Live", color=GREEN if (chg or 0) >= 0 else RED,
                      description="\n".join(desc))
    rows = []
    for sym in watchlist:
        q = quotes.get(sym)
        if not q:
            continue
        coin = coins.get(short(sym))
        extra = ""
        if coin is not None and getattr(coin, "change_1h", None) is not None:
            extra = f" 1h {coin.change_1h:+.1f}%"
            if coin.change_7d is not None:
                extra += f" 7d {coin.change_7d:+.0f}%"
        rows.append((short(sym), q.price, q.change_pct, extra))
    for i, chunk in enumerate(_chunks(rows, 14)):
        e.add_field(name="24h change" if i == 0 else "24h change (cont.)", value=ansi_rows(chunk, 6), inline=False)
    e.set_footer(text=f"Prices: {sources_note(quotes)}, market data from CoinGecko · /watchlist to change")
    return fit_embed(e)


def _chunks(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)] or [[]]


# ----- alerts -----

def move_alert(q: Quote, threshold: float, window: str, change: float, market: str) -> discord.Embed:
    up = change > 0
    name = display_name(q.symbol, q.name)
    e = discord.Embed(
        title=f"{'🚀' if up else '📉'} {tag(q.symbol)} {'up' if up else 'down'} {abs(change):.1f}% {window}",
        description=f"**{name}** at **{money(q.price, q.symbol)}** ({pct(q.change_pct, already_pct=True)} "
                    f"{'in 24 hours' if q.extra.get('change_window') == '24h' else 'today'})"
                    f"\nCrossed the ±{threshold:g}% alert line.",
        color=GREEN if up else RED)
    if q.high52 and q.price >= q.high52 * 0.995:
        e.description += "\n🏔️ At its 52-week high."
    elif q.low52 and q.price <= q.low52 * 1.005:
        e.description += "\n🕳️ At its 52-week low."
    e.set_footer(text=f"/forecast {short(q.symbol)} for what usually follows")
    return fit_embed(e)


def price_alert(q: Quote, target: float, above: bool, who: int | None) -> tuple[discord.Embed, str | None]:
    e = discord.Embed(title=f"🔔 {tag(q.symbol)} {'above' if above else 'below'} {fmt_price(target)}",
                      description=f"**{display_name(q.symbol, q.name)}** is at **{money(q.price, q.symbol)}** "
                                  f"({pct(q.change_pct, already_pct=True)} today).",
                      color=GREEN if above else RED)
    return fit_embed(e), (f"<@{who}>" if who else None)


def setup_stats_line(s: SetupStats | None, label: str) -> str:
    if not s:
        return ""
    return (f"{label}: higher 20 days later **{s.up_rate:.0%}** of {s.events} times "
            f"(normal {s.base_up:.0%}), median {pct(s.median, 1)}")


def breakout_alert(hit: ScanHit, setup, history_label: str) -> discord.Embed:
    d = DEFS[setup.key]
    up = d.direction >= 0
    color = GREEN if d.direction > 0 else RED if d.direction < 0 else GOLD
    desc = f"**{hit.name}** at **{money(hit.price, hit.symbol)}**"
    if hit.change_pct is not None:
        desc += f" ({pct(hit.change_pct, already_pct=True)} today)"
    if setup.detail:
        desc += f"\n{setup.detail[:1].upper() + setup.detail[1:]}."
    e = discord.Embed(title=f"{d.emoji} {tag(hit.symbol)} · {setup.name}", description=desc, color=color)
    p = setup.plan
    plan = []
    if p.trigger is not None:
        plan.append(f"Trigger **{fmt_price(p.trigger)}**")
    if p.target is not None:
        plan.append(f"target {fmt_price(p.target)}")
    if p.stop is not None:
        plan.append(f"fails below {fmt_price(p.stop)}" if up else f"fails above {fmt_price(p.stop)}")
    if plan:
        e.add_field(name="Levels", value=" · ".join(plan), inline=False)
    odds = []
    if hit.breakout_up is not None:
        odds.append(f"Close above {fmt_price(hit.hi20)} within 10 sessions: **{hit.breakout_up:.0%}** "
                    f"(normal {hit.base_up:.0%})")
        odds.append(f"Close below {fmt_price(hit.lo20)}: **{hit.breakout_down:.0%}** (normal {hit.base_down:.0%})")
    if odds:
        e.add_field(name="Breakout odds (model)", value="\n".join(odds), inline=False)
    hist = [x for x in (setup_stats_line(setup.stats, history_label),
                        setup_stats_line(setup.bench_stats, "On the benchmark")) if x]
    if hist:
        e.add_field(name="How this setup played out before", value="\n".join(hist), inline=False)
    e.set_footer(text=f"/forecast {short(hit.symbol)} for the full picture · {DISCLAIMER}")
    return fit_embed(e)


# ----- forecast / research -----

def _skill_note(o: Outlook, target: str) -> str:
    s = o.skill.get(target)
    if not s:
        return ""
    return f"AUC {s.auc:.2f} on {s.n:,} unseen days ({s.grade})"


def outlook_embed(o: Outlook, title_prefix: str = "🔮") -> discord.Embed:
    color = GREEN if o.score >= 15 else RED if o.score <= -15 else GOLD
    q = o.quote
    head = f"**{o.label}** technical picture · score {o.score:+d}/100 · confidence {o.confidence}"
    price = f"**{money(o.price, o.symbol)}**"
    if q and q.change_pct is not None:
        price += f" ({pct(q.change_pct, already_pct=True)} today)"
    lines = [head, f"{price} · {o.trend[0]} · volatility {o.vol_regime[0]} "
                   f"({o.vol_regime[1]:.0%} of its history was calmer)"]
    if o.vs_sma200 is not None:
        lines.append(f"{pct(o.vs_sma50, 1)} vs 50-day avg · {pct(o.vs_sma200, 1)} vs 200-day · RSI {o.rsi:.0f}")
    e = discord.Embed(title=f"{title_prefix} {title_of(o.symbol, o.name)}", description="\n".join(lines), color=color)

    if o.breakout_up is not None:
        b_up, b_dn = o.base.get("breakout_up", 0), o.base.get("breakout_down", 0)
        txt = (f"Close **above {fmt_price(o.hi20)}** (20-day high): **{o.breakout_up:.0%}** · normal {b_up:.0%}\n"
               f"Close **below {fmt_price(o.lo20)}** (20-day low): **{o.breakout_down:.0%}** · normal {b_dn:.0%}")
        note = _skill_note(o, "breakout_up")
        if note:
            txt += f"\n-# Breakout model: {note}"
        e.add_field(name="⚡ Breakout odds · next 10 sessions", value=txt, inline=False)
    base = {5: o.base.get("up_5d"), 20: o.base.get("up_20d"), 60: o.base.get("up_60d")}
    up_txt = " · ".join(f"{lbl} **{o.up[h]:.0%}**" for h, lbl in ((5, "1 week"), (20, "1 month"), (60, "3 months")))
    base_txt = " / ".join(f"{base[h]:.0%}" for h in (5, 20, 60) if base[h] is not None)
    skill = o.skill.get("up_20d")
    sub = f"-# Normal rate {base_txt}" if base_txt else ""
    if skill:
        sub += f" · direction is the hard part: model {skill.grade} (AUC {skill.auc:.2f}), so these lean on history"
    e.add_field(name="📈 Chance of being higher", value=up_txt + ("\n" + sub if sub else ""), inline=False)
    if o.cone:
        rows = []
        for h, lbl in ((21, "1 month"), (63, "3 months"), (252, "1 year")):
            d = o.cone.horizons.get(h)
            if d:
                rows.append(f"{lbl}: {fmt_price(d['p5'])} – {fmt_price(d['p95'])} · median {fmt_price(d['p50'])}")
        m = o.cone.horizons.get(21)
        if m:
            rows.append(f"Touch +10% within a month: {m['touch_up10']:.0%} · touch −10%: {m['touch_down10']:.0%}")
        e.add_field(name="🎯 Likely range (Monte Carlo, 90% band)", value="\n".join(rows), inline=False)
    a = o.analogs
    if a.matches and 20 in a.horizons:
        h20, h60 = a.horizons[20], a.horizons.get(60)
        first = min(stats.to_date(m.t).year for m in a.matches)
        txt = (f"{h20['n']} closest setups since {first} (similarity {a.similarity:.0f}/100): higher 20 days later "
               f"**{h20['up']:.0%}**, median **{pct(h20['median'], 1)}**")
        if h60:
            txt += f"; 60 days: {h60['up']:.0%}, {pct(h60['median'], 1)}"
        closest = " · ".join(f"{short(m.symbol)} {stats.to_date(m.t):%b %Y} ({pct(m.forward.get(20), 1)})"
                             for m in a.matches[:4])
        e.add_field(name="🧬 Historical look-alikes", value=f"{txt}\nClosest: {closest}", inline=False)
    if o.setups:
        lines = []
        for s in o.setups[:5]:
            when = "today" if s.days_ago == 0 else f"{s.days_ago}d ago"
            line = f"{s.emoji} **{s.name}** ({when})"
            if s.detail:
                line += f" · {s.detail}"
            st = s.stats or s.bench_stats
            if st:
                line += f"\n-# Before on {'this chart' if s.stats else 'the benchmark'}: up 20d later {st.up_rate:.0%} of {st.events} (normal {st.base_up:.0%})"
            lines.append(line)
        e.add_field(name="🧩 Active setups", value="\n".join(lines), inline=False)
    lv = []
    if o.resistance:
        lv.append("Resistance: " + ", ".join(f"**{fmt_price(l.price)}** ({l.label})" for l in o.resistance[:3]))
    if o.support:
        lv.append("Support: " + ", ".join(f"**{fmt_price(l.price)}** ({l.label})" for l in o.support[:3]))
    if lv:
        e.add_field(name="🧱 Key levels", value="\n".join(lv), inline=False)
    if o.drivers:
        e.add_field(name="🧠 What's moving the model", value=" · ".join(
            f"{'▲' if c > 0 else '▼'} {FEATURE_LABELS.get(f, f)}" for f, c in o.drivers), inline=False)
    if not o.model_ready:
        e.add_field(name="⏳", value="The prediction models are still training (a few minutes after startup); "
                                     "odds are from look-alikes and history for now.", inline=False)
    e.set_footer(text=DISCLAIMER)
    e.timestamp = datetime.now(timezone.utc)
    return fit_embed(e)


def snapshot_embed(o: Outlook, fundamentals: dict, options: OptionsView | None, extra: dict) -> discord.Embed:
    e = discord.Embed(title=f"🔬 {tag(o.symbol)} · research snapshot", color=BLUE)
    p = o.perf
    if p:
        r = p.returns
        perf = " · ".join(f"{k} {pct(r[k], 1)}" for k in ("1D", "1W", "1M", "3M", "YTD", "1Y", "3Y", "5Y", "10Y")
                          if r.get(k) is not None)
        e.add_field(name="Performance", value=perf, inline=False)
        risk = (f"CAGR since {p.first.year}: **{p.cagr:.1%}** · volatility {p.vol:.0%}/yr · Sharpe {p.sharpe:.2f}\n"
                f"Worst fall: **{p.max_dd.depth:.0%}** ({p.max_dd.peak:%b %Y} → {p.max_dd.trough:%b %Y}"
                + (f", recovered {p.max_dd.recovered:%b %Y})" if p.max_dd.recovered else ", not yet recovered)")
                + f" · now {p.current_dd:.1%} from the high")
        if extra.get("beta"):
            b, corr = extra["beta"]
            risk += f"\nBeta vs {extra.get('bench_name', 'benchmark')}: {b:.2f} (correlation {corr:.2f})"
        e.add_field(name="Risk", value=risk, inline=False)
    fd = fundamentals.get("financialData") or {}
    ks = fundamentals.get("defaultKeyStatistics") or {}
    cal = fundamentals.get("calendarEvents") or {}
    q = o.quote
    val = []
    ex = q.extra if q else {}
    if ex.get("marketCap"):
        val.append(f"Market cap **${big(ex['marketCap'])}**")
    if ex.get("trailingPE"):
        val.append(f"P/E {ex['trailingPE']:.1f}")
    if ex.get("forwardPE"):
        val.append(f"forward P/E {ex['forwardPE']:.1f}")
    peg = (ks.get("pegRatio") or {}).get("raw") if isinstance(ks.get("pegRatio"), dict) else None
    if peg:
        val.append(f"PEG {peg:.2f}")
    for key, label in (("revenueGrowth", "revenue growth"), ("profitMargins", "profit margin"),
                       ("returnOnEquity", "ROE")):
        v = (fd.get(key) or {}).get("raw") if isinstance(fd.get(key), dict) else None
        if v is not None:
            val.append(f"{label} {v:.0%}")
    if ex.get("dividendYield"):
        val.append(f"dividend {ex['dividendYield']:.2f}%")
    short_float = (ks.get("shortPercentOfFloat") or {}).get("raw") if isinstance(ks.get("shortPercentOfFloat"), dict) else None
    if short_float:
        val.append(f"short interest {short_float:.1%} of float")
    if val:
        e.add_field(name="Valuation & business", value=" · ".join(val), inline=False)
    target = (fd.get("targetMeanPrice") or {}).get("raw") if isinstance(fd.get("targetMeanPrice"), dict) else None
    analysts = []
    if target:
        analysts.append(f"Mean target **{fmt_price(target)}** ({pct(target / o.price - 1, 1)})")
    if fd.get("recommendationKey"):
        n = (fd.get("numberOfAnalystOpinions") or {}).get("raw") if isinstance(fd.get("numberOfAnalystOpinions"), dict) else None
        analysts.append(f"consensus **{str(fd['recommendationKey']).replace('_', ' ')}**" + (f" ({n} analysts)" if n else ""))
    trend = ((fundamentals.get("recommendationTrend") or {}).get("trend") or [{}])[0]
    if trend:
        analysts.append(f"{trend.get('strongBuy', 0) + trend.get('buy', 0)} buy · {trend.get('hold', 0)} hold · "
                        f"{trend.get('sell', 0) + trend.get('strongSell', 0)} sell")
    earn = ((cal.get("earnings") or {}).get("earningsDate") or [])
    if earn and isinstance(earn[0], dict) and earn[0].get("raw"):
        analysts.append(f"next earnings {ts(earn[0]['raw'], 'D')}")
    hist = (fundamentals.get("earningsHistory") or {}).get("history") or []
    beats = [h for h in hist if isinstance(h.get("surprisePercent"), dict) and h["surprisePercent"].get("raw") is not None]
    if beats:
        wins = sum(1 for h in beats if h["surprisePercent"]["raw"] > 0)
        analysts.append(f"beat EPS estimates {wins} of the last {len(beats)} quarters")
    if analysts:
        e.add_field(name="Analysts & earnings", value=" · ".join(analysts), inline=False)
    if options:
        ov = options
        lines = [f"Expiry {ts(ov.expiry, 'D')} ({ov.days:.0f} days)"]
        if ov.expected_move:
            lines.append(f"Options price a move of **±{fmt_price(ov.expected_move)}** "
                         f"(±{ov.expected_move / ov.spot:.1%}) by then")
        if ov.atm_iv:
            iv = f"Implied volatility **{ov.atm_iv:.0%}**"
            if ov.realized_vol:
                ratio = ov.atm_iv / ov.realized_vol
                iv += f" vs {ov.realized_vol:.0%} realized ({'options pricing extra risk' if ratio > 1.2 else 'options cheap vs recent moves' if ratio < 0.8 else 'in line'})"
            lines.append(iv)
        flow = []
        if ov.put_call_oi:
            flow.append(f"put/call open interest {ov.put_call_oi:.2f}")
        if ov.put_call_volume:
            flow.append(f"put/call volume {ov.put_call_volume:.2f}")
        if flow:
            lines.append(" · ".join(flow) + (" (bearish tilt)" if (ov.put_call_volume or ov.put_call_oi or 0) > 1.0
                                             else " (bullish tilt)" if (ov.put_call_volume or ov.put_call_oi or 1) < 0.6 else ""))
        walls = []
        if ov.call_wall:
            walls.append(f"call wall {fmt_price(ov.call_wall)}")
        if ov.put_wall:
            walls.append(f"put wall {fmt_price(ov.put_wall)}")
        if ov.max_pain:
            walls.append(f"max pain {fmt_price(ov.max_pain)}")
        if walls:
            lines.append(" · ".join(walls) + (" (by today's volume; open interest updates in market hours)"
                                              if not ov.total_call_oi else ""))
        e.add_field(name="🎲 Options positioning", value="\n".join(lines), inline=False)
    if extra.get("season"):
        s = extra["season"]
        e.add_field(name=f"📅 {stats.MONTHS[s.month - 1]} in history",
                    value=f"Average {pct(s.avg, 1)}, higher {s.up:.0%} of {s.n} years", inline=True)
    if extra.get("halving"):
        hv = extra["halving"]
        txt = f"{hv['days_since']} days since the {hv['last']:%b %Y} halving ({pct(hv.get('since_halving_now'), 0)} since)"
        prev = [f"{r['halving'].year}: {pct(r.get('6M'), 0)}" for r in hv["earlier"] if r.get("6M") is not None]
        if prev:
            txt += f"\nNext 6 months from the same point: {' · '.join(prev)}"
        e.add_field(name="⛏️ Halving cycle", value=txt, inline=False)
    news = extra.get("news") or []
    if news:
        e.add_field(name="📰 Latest news", value="\n".join(
            f"{'🟢' if a.polarity > 0 else '🔴' if a.polarity < 0 else '⚪'} {headline_link(a, 90)} "
            f"· *{a.headline.source}*" for a in news[:5]), inline=False)
    e.set_footer(text=DISCLAIMER)
    return fit_embed(e)


def scan_embed(hits: list[ScanHit], market: str, title: str | None = None) -> discord.Embed:
    e = discord.Embed(title=title or f"⚡ Breakout radar · {'stocks' if market == STOCKS else 'crypto' if market == CRYPTO else 'all'}",
                      color=GOLD)
    lines = []
    for h in hits[:12]:
        tags = " ".join(s.emoji for s in h.setups[:3])
        odds = ""
        if h.breakout_up is not None:
            if h.breakout_up - h.base_up >= h.breakout_down - h.base_down:
                odds = f"↗ {h.breakout_up:.0%} to clear {fmt_price(h.hi20)}"
            else:
                odds = f"↘ {h.breakout_down:.0%} to lose {fmt_price(h.lo20)}"
        names = ", ".join(s.name for s in h.setups[:2])
        lines.append(f"**{short(h.symbol)}** {money(h.price, h.symbol)} ({pct(h.change_pct, 1, already_pct=True)}) {tags}\n"
                     f"-# {odds}{' · ' + names if names else ''}")
    e.description = "\n".join(lines) or "Nothing set up right now."
    e.set_footer(text="Odds: chance of a close beyond the 20-day high/low within 10 sessions · /forecast for detail")
    return fit_embed(e)


def news_embed(a: Analysis) -> discord.Embed:
    h = a.headline
    color = GREEN if a.polarity > 0 else RED if a.polarity < 0 else GREY
    e = discord.Embed(title=clip(f"{a.emoji} {h.title}", 256), url=h.link or None, color=color)
    desc = []
    if a.note:
        desc.append(f"💡 {a.note}")
    elif h.summary:
        desc.append(clip(h.summary, 300))
    tag = f"**{a.kind}** · importance {a.importance}/100 · confidence {a.confidence}"
    if a.priced_in:
        tag += " · describes a move that already happened"
    desc.append(tag)
    e.description = "\n".join(desc)
    if a.impacts:
        e.add_field(name="Expected impact (next day)", value="\n".join(impact_text(i) for i in a.impacts[:6]),
                    inline=False)
    src = h.source + (f" (+{len(h.also)} more)" if h.also else "")
    reader = "Claude" if a.source == "ai" else "keyword model"
    e.set_footer(text=f"{src} · read by {reader} · ranges scaled to today's volatility")
    e.timestamp = datetime.fromtimestamp(h.published, timezone.utc)
    return fit_embed(e)


def headline_link(a: Analysis, limit: int = 110) -> str:
    """A headline as a link, unless the link is so long it would crowd other stories out of the post."""
    title = clip(a.headline.title, limit)
    link = a.headline.link
    return f"[{title}]({link})" if link and len(link) <= 160 else f"**{title}**"


def news_digest(items: list[Analysis], title: str) -> discord.Embed:
    e = discord.Embed(title=title, color=GREY)
    lines = []
    for a in items[:12]:
        dot = "🟢" if a.polarity > 0 else "🔴" if a.polarity < 0 else "⚪"
        top = a.impacts[0] if a.impacts else None
        hint = f" · {top.name} {'▲' if top.direction > 0 else '▼' if top.direction < 0 else '⇅'}" if top else ""
        lines.append(f"{dot} {headline_link(a)} · *{a.headline.source}*{hint}")
    e.description = "\n".join(lines) or "No news."
    return fit_embed(e)


def macro_embed(m: Macro, cape) -> discord.Embed:
    q = m.quotes
    e = discord.Embed(title="🌐 Macro dashboard", color=BLUE)
    if m.mood is not None:
        parts = "\n".join(f"-# {k}: {v * 100:.0f}" for k, v in m.mood_parts.items())
        e.add_field(name="Stock market mood", value=f"**{m.mood:.0f}/100 · {mood_label(m.mood)}**\n{parts}", inline=True)
    if m.crypto_fng is not None:
        txt = f"**{m.crypto_fng:.0f} · {fear_greed_label(m.crypto_fng)}**"
        if m.crypto_fng_prev_week is not None:
            txt += f" (week ago {m.crypto_fng_prev_week:.0f})"
        if m.fng_history:
            fh = m.fng_history
            txt += (f"\n-# After similar readings BTC was higher 30 days later {fh['up']:.0%} of {fh['n']} days, "
                    f"median {pct(fh['median'], 1)} (all days: {fh['all_up']:.0%})")
        e.add_field(name="Crypto Fear & Greed", value=txt, inline=True)
    vix = q.get("^VIX")
    if vix:
        txt = f"**{vix.price:.2f}** ({pct(vix.change_pct, 1, already_pct=True)})"
        if m.vix_pct is not None:
            txt += f" · higher than {m.vix_pct:.0%} of days since 1990"
        e.add_field(name="VIX (expected S&P swings)", value=txt, inline=False)
    rates = []
    tnx, irx = q.get("^TNX"), q.get("^IRX")
    if tnx:
        rates.append(f"10-year **{tnx.price:.2f}%** ({(tnx.price - (tnx.prev_close or tnx.price)) * 100:+.0f} bp)")
    if irx:
        rates.append(f"3-month {irx.price:.2f}%")
    if m.curve is not None:
        rates.append(f"curve {m.curve:+.2f} pts" + (" ⚠️ inverted (has preceded most US recessions)" if m.curve < 0 else ""))
    if rates:
        e.add_field(name="Rates", value=" · ".join(rates), inline=False)
    rows = [(n, getattr(q.get(s), "price", None), getattr(q.get(s), "change_pct", None), "")
            for s, n in (("DX-Y.NYB", "Dollar"), ("GC=F", "Gold"), ("CL=F", "Oil"), ("HG=F", "Copper"),
                         ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ether"), ("^MOVE", "MOVE"))]
    e.add_field(name="Cross-asset", value=ansi_rows(rows, 8), inline=False)
    if m.crypto_global:
        cg = m.crypto_global
        e.add_field(name="Crypto market", value=f"${big(cg.total_cap)} ({cg.cap_change_24h:+.1f}% 24h) · BTC dominance "
                                                f"{cg.btc_dominance:.1f}%", inline=False)
    if cape:
        e.add_field(name="S&P 500 valuation (Shiller CAPE, since 1881)",
                    value=f"CAPE ≈ **{cape.cape_now:.1f}** (median {cape.median:.1f}) · pricier than "
                          f"{cape.percentile:.0%} of months\nHistory's 10-year outlook at this valuation: "
                          f"**{cape.expected_10y_real:+.1%}/yr** after inflation (range {cape.band[0]:+.1%} to {cape.band[1]:+.1%})"
                          + (f"\n-# Similar valuations: {', '.join(str(y) for y in cape.similar_years[:8])}" if cape.similar_years else ""),
                    inline=False)
    e.set_footer(text="Mood: momentum, price strength, VIX and junk-bond demand vs the last 2 years (0 fear · 100 greed)")
    return fit_embed(e)


def history_embed(symbol: str, name: str, perf: stats.Performance, dds: list[stats.Drawdown],
                  years: list[tuple[int, float]], seasons: list[stats.Season], decs, pres: dict | None,
                  source_note: str) -> discord.Embed:
    e = discord.Embed(title=f"📜 {title_of(symbol, name)} · the long view", color=PURPLE,
                      description=f"Since **{perf.first:%B %Y}** ({perf.years:.0f} years): **{perf.cagr:.1%}/yr**, "
                                  f"×{1 + perf.returns['Max']:,.0f} in total · up days {perf.up_days:.0%}\n"
                                  f"Best day {pct(perf.best_day[0], 1)} ({perf.best_day[1]:%b %d %Y}) · worst "
                                  f"{pct(perf.worst_day[0], 1)} ({perf.worst_day[1]:%b %d %Y})")
    if decs:
        e.add_field(name="By decade", value="\n".join(f"`{d}` {pct(tot, 0):>7} ({ann:+.1%}/yr)" for d, tot, ann in decs[-12:]),
                    inline=True)
    if dds:
        e.add_field(name="Biggest falls", value="\n".join(
            f"`{d.depth:.0%}` {d.peak:%b %Y}→{d.trough:%b %Y}"
            + (f", back by {d.recovered:%Y}" if d.recovered else ", not recovered") for d in dds[:7]), inline=True)
    if years:
        full = years[:-1] if len(years) > 1 else years
        r = np.array([y[1] for y in full])
        best = sorted(full, key=lambda y: -y[1])[:3]
        worst = sorted(full, key=lambda y: y[1])[:3]
        e.add_field(name="Calendar years", value=(
            f"Up in **{(r > 0).mean():.0%}** of {len(r)} years · median {pct(float(np.median(r)), 1)}\n"
            f"Best: {', '.join(f'{y} {pct(v, 0)}' for y, v in best)}\nWorst: {', '.join(f'{y} {pct(v, 0)}' for y, v in worst)}\n"
            f"This year so far: **{pct(years[-1][1], 1)}**"), inline=False)
    if seasons:
        cells = [f"`{stats.MONTHS[s.month - 1]}` {pct(s.avg, 1)} ({s.up:.0%}↑)" for s in seasons]
        e.add_field(name="Average month", value="\n".join(" · ".join(cells[i:i + 3]) for i in range(0, 12, 3)),
                    inline=False)
    if pres:
        now = stats.presidential_year(datetime.now(timezone.utc).year)
        e.add_field(name="US presidential cycle", value="\n".join(
            f"{'➡️ ' if k == now else ''}{stats.PRESIDENTIAL_NAMES[k]}: avg {pct(v['avg'], 1)}, up {v['up']:.0%} "
            f"({v['n']} years)" for k, v in pres.items()), inline=False)
    e.set_footer(text=source_note)
    return fit_embed(e)


def backtest_embed(symbol: str, name: str, bt: Backtest) -> discord.Embed:
    beat = bt.strategy_cagr > bt.hold_cagr
    e = discord.Embed(title=f"🧪 {tag(symbol)} · model timing backtest since {bt.start_year}", color=GREEN if beat else GOLD,
                      description=f"Rule: hold **{name}** while the model's chance of being higher in 20 days beats "
                                  "the usual rate, otherwise cash. Each year was predicted by a model trained only on "
                                  "earlier years, so this is what it would have done live.")
    e.add_field(name="Model timing", value=f"**{bt.strategy_cagr:.1%}/yr** · worst fall {bt.strategy_dd:.0%}\n"
                                           f"invested {bt.exposure:.0%} of days · {bt.trades} entries", inline=True)
    e.add_field(name="Buy and hold", value=f"**{bt.hold_cagr:.1%}/yr** · worst fall {bt.hold_dd:.0%}", inline=True)
    if bt.skill:
        s = bt.skill
        e.add_field(name="Prediction quality", value=f"Right on direction {s.accuracy:.0%} of days (always-up would be "
                                                     f"{s.base_rate:.0%}) · AUC {s.auc:.2f} ({s.grade})\n"
                                                     f"Invested 20-day spells that ended higher: {bt.hit_rate:.0%}",
                    inline=False)
    verdict = ("The model's timing beat simply holding." if beat else
               "Holding beat the model's timing: in-and-out rarely wins after the fact.")
    if bt.strategy_dd > bt.hold_dd and not beat:
        verdict += " It did cut the worst drawdown."
    e.add_field(name="Verdict", value=verdict + " No costs or taxes included.", inline=False)
    e.set_footer(text=DISCLAIMER)
    return fit_embed(e)


def record_embed(summary: dict, news: dict, models: dict[str, MarketModel]) -> discord.Embed:
    e = discord.Embed(title="📒 Track record", color=BLUE,
                      description="Every forecast and breakout call the bot posts by itself is graded when its time is "
                                  "up. A forecast counts as right when it said above 50% and the price rose (or below "
                                  "50% and it fell).")
    for h, f in sorted(summary.get("forecasts", {}).items()):
        better = "better" if f["brier"] < f["base_brier"] else "worse"
        e.add_field(name=f"{'1-week' if h == 5 else '1-month'} forecasts",
                    value=f"{f['n']} graded · right **{f['hit']:.0%}** · prices rose {f['up_rate']:.0%} of the time\n"
                          f"-# Brier {f['brier']:.3f} vs {f['base_brier']:.3f} for always saying the usual rate ({better})",
                    inline=False)
    b = summary.get("breakouts")
    if b:
        txt = f"{b['n']} graded · broke through **{b['hit']:.0%}** of the time"
        if b.get("avg_p") is not None:
            txt += f" (the model expected {b['avg_p']:.0%})"
        e.add_field(name="Breakout calls", value=txt, inline=False)
    if summary.get("open_forecasts") or summary.get("open_breakouts"):
        e.add_field(name="Still open", value=f"{summary.get('open_forecasts', 0)} forecasts · "
                                             f"{summary.get('open_breakouts', 0)} breakout calls", inline=False)
    if news.get("n"):
        e.add_field(name="News impact calls", value=f"{news['n']} graded after a day · direction right "
                                                    f"**{news['hits'] / news['n']:.0%}** (each event type's sizes are "
                                                    "re-scaled to what markets actually did)", inline=False)
    for market, m in models.items():
        lines = []
        for target, label in (("breakout_up", "Breakouts up"), ("breakout_down", "Breakdowns"),
                              ("up_20d", "Direction, 1 month")):
            s = m.skill.get(target)
            if s:
                lines.append(f"{label}: AUC **{s.auc:.2f}** ({s.grade}) · Brier skill {s.skill:+.2f}")
        if lines:
            e.add_field(name=f"{market.title()} model on unseen years ({m.rows:,} training days since {m.first_year})",
                        value="\n".join(lines), inline=False)
    if len(e.fields) == 0:
        e.add_field(name="Nothing graded yet", value="Calls are graded 1–4 weeks after they're made.", inline=False)
    e.set_footer(text="AUC: 0.5 = coin flip, 1.0 = perfect · Brier: lower is better")
    return fit_embed(e)


def movers_embed(title: str, gainers: list[tuple[str, str, float, float]], losers: list[tuple[str, str, float, float]],
                 note: str = "") -> discord.Embed:
    e = discord.Embed(title=title, color=BLUE)
    e.add_field(name="Top gainers", value=ansi_rows([(s, p, c, "") for s, _, p, c in gainers[:10]]), inline=False)
    e.add_field(name="Top losers", value=ansi_rows([(s, p, c, "") for s, _, p, c in losers[:10]]), inline=False)
    if note:
        e.set_footer(text=note)
    return fit_embed(e)


def quote_embed(q: Quote, extra_lines: list[str]) -> discord.Embed:
    chg = q.change_pct
    e = discord.Embed(title=f"{arrow(chg)} {title_of(q.symbol, display_name(q.symbol, q.name))}",
                      description=f"**{money(q.price, q.symbol)}** {pct(chg, already_pct=True)}"
                                  + (f" ({'+' if (q.change or 0) >= 0 else ''}{fmt_price(q.change)})" if q.change is not None else "")
                                  + ("\n🟢 Trading 24/7" if q.quote_type == "CRYPTOCURRENCY"
                                     else f"\n{state_label(q)}" if state_label(q) else ""),
                      color=GREEN if (chg or 0) >= 0 else RED)
    if q.ext_price and q.ext_change_pct is not None:
        e.description += f" · extended hours {money(q.ext_price, q.symbol)} ({pct(q.ext_change_pct, already_pct=True)})"
    stats_ = []
    if q.day_low and q.day_high:
        stats_.append(f"Day {fmt_price(q.day_low)} – {fmt_price(q.day_high)}")
    if q.low52 and q.high52:
        pos = (q.price - q.low52) / (q.high52 - q.low52) if q.high52 > q.low52 else 0.5
        stats_.append(f"52 weeks {fmt_price(q.low52)} – {fmt_price(q.high52)} ({pos:.0%} of the way up)")
    if q.volume:
        v = f"Volume {big(q.volume)}"
        if q.avg_volume:
            v += f" ({q.volume / q.avg_volume:.1f}× normal)"
        stats_.append(v)
    if q.extra.get("marketCap"):
        stats_.append(f"Market cap ${big(q.extra['marketCap'])}")
    e.add_field(name="Today", value="\n".join(stats_ + extra_lines) or "—", inline=False)
    e.set_footer(text=f"{SOURCE_NAMES.get(q.source, q.source)} · "
                      f"{datetime.fromtimestamp(q.time or time.time(), timezone.utc):%b %d %H:%M} UTC")
    return fit_embed(e)


# ----- trends -----

def _mover_rows(movers, n: int, width: int = 7, volume: bool = False) -> str:
    return ansi_rows([(short(m.symbol), m.price, m.change, f" {big(m.volume)}" if volume and m.volume else "")
                      for m in movers[:n]], width)


def _sector_rows(snap, period: str) -> str:
    rows = [(name[:12], snap.prices.get(sym), snap.changes[sym][period], "")
            for sym, name in SECTORS.items() if period in snap.changes.get(sym, {})]
    rows.sort(key=lambda r: -r[2])
    return ansi_rows(rows, 12)


def trends_board(snap, sp500: list[str], ndx: list[str], market_state: str = "") -> discord.Embed:
    """The live trends board: today's movers across the US market, sectors, the week's leaders and crypto."""
    up, down = snap.breadth(sp500)
    nup, ndown = snap.breadth(ndx)
    desc = [f"{market_state + ' · ' if market_state else ''}updated {ts(snap.at)}"]
    if up + down:
        desc.append(f"S&P 500 today: **{up}** up · **{down}** down · Nasdaq-100: **{nup}** up · **{ndown}** down")
    e = discord.Embed(title="🔥 Market Trends · Live", color=GREEN if up >= down else RED, description="\n".join(desc))
    when = "last session" if snap.day_source.startswith("Nasdaq") else "today"
    if snap.day_gainers:
        e.add_field(name=f"🚀 Top gainers {when} (US, $2B+)", value=_mover_rows(snap.day_gainers, 8), inline=False)
    if snap.day_losers:
        e.add_field(name=f"💥 Top losers {when}", value=_mover_rows(snap.day_losers, 8), inline=False)
    if snap.most_active:
        e.add_field(name=f"🔊 Most traded {when}", value=_mover_rows(snap.most_active, 6, volume=True), inline=False)
    if any("1D" in snap.changes.get(s, {}) for s in SECTORS):
        e.add_field(name="🏭 Sectors today", value=_sector_rows(snap, "1D"), inline=False)
    week = snap.movers("WTD", list(dict.fromkeys(sp500 + ndx)))
    if week:
        e.add_field(name="📅 This week's leaders (S&P 500 + Nasdaq-100)",
                    value=_mover_rows(week, 5) + _mover_rows(week[::-1], 5), inline=False)
    coins = snap.coin_movers("1D")
    if coins:
        e.add_field(name="🪙 Crypto 24h (top 250)", value=_mover_rows(coins, 5) + _mover_rows(coins[::-1], 5),
                    inline=False)
    if not e.fields:
        e.add_field(name="Waiting for data", value="The data sources haven't answered yet; this fills in shortly.",
                    inline=False)
    e.set_footer(text=f"Today: {snap.day_source or '—'} · periods: {snap.periods_source or '—'} · crypto: CoinGecko · "
                      "/trends for any period")
    return fit_embed(e)


TREND_MARKETS = {"stocks": "📈 Stocks", "sectors": "🏭 Sectors & ETFs", "crypto": "🪙 Crypto"}


def trends_embed(snap, period: str, market: str, sp500: list[str], ndx: list[str], count: int = 10) -> discord.Embed:
    """Gainers and losers for one market over one period (1D, WTD, MTD, 1W, 1M, 3M, YTD, 1Y). Crypto uses
    CoinGecko's rolling 24-hour, 7-day, 30-day and 1-year changes (this week and month map to 7 and 30 days)."""
    from .trends import CRYPTO_ALIASES, CRYPTO_LABELS, CRYPTO_PERIODS, MAJOR_ETFS, PERIODS
    label = PERIODS.get(period, period)
    if market == "crypto":
        period = CRYPTO_ALIASES.get(period, period)
        label = CRYPTO_LABELS.get(period, label)
    e = discord.Embed(title=f"{TREND_MARKETS.get(market, market)} · biggest moves {label}", color=BLUE)
    note = ""
    if market == "crypto":
        movers = snap.coin_movers(period)
        note = "Top 250 coins by market cap, without stablecoins and wrapped coins · CoinGecko"
        if not movers:
            e.description = ("CoinGecko has 24-hour, 7-day, 30-day and 1-year changes for coins: pick one of those."
                             if period not in CRYPTO_PERIODS else
                             "No crypto numbers right now: CoinGecko isn't answering. They come back by themselves.")
            e.set_footer(text=note)
            return fit_embed(e)
    elif market == "sectors":
        rows = [(SECTORS.get(s, s)[:12], snap.prices.get(s), snap.changes[s][period], "")
                for s in list(SECTORS) + MAJOR_ETFS if period in snap.changes.get(s, {})]
        rows.sort(key=lambda r: -r[2])
        if not rows:
            e.description = NO_PERIOD_DATA
        else:
            e.add_field(name="Sector & major ETFs", value=ansi_rows(rows[:20], 12), inline=False)
        e.set_footer(text=f"{snap.periods_source or 'Yahoo Finance'} · {DISCLAIMER_SHORT}")
        return fit_embed(e)
    else:
        pool = list(dict.fromkeys(sp500 + ndx))
        if period == "1D" and snap.day_gainers:
            movers = snap.day_gainers + snap.day_losers[::-1]
            note = f"Whole US market, companies worth $2B+ · {snap.day_source}"
            up, down = snap.breadth(sp500)
            if up + down:
                e.description = f"S&P 500: **{up}** up · **{down}** down"
        else:
            movers = snap.movers(period, pool)
            note = f"S&P 500 + Nasdaq-100 · {snap.periods_source or 'Yahoo Finance'}"
            up, down = snap.breadth(sp500, period)
            if up + down:
                e.description = f"S&P 500 {label}: **{up}** up · **{down}** down"
    gainers = [m for m in movers if m.change > 0][:count]
    losers = sorted((m for m in movers if m.change < 0), key=lambda m: m.change)[:count]
    if not gainers and not losers:
        e.description = NO_PERIOD_DATA
        e.set_footer(text=note)
        return fit_embed(e)
    e.add_field(name="🚀 Gainers", value=_mover_rows(gainers, count), inline=False)
    e.add_field(name="💥 Losers", value=_mover_rows(losers, count), inline=False)
    if market == "stocks" and period == "1D" and snap.most_active:
        e.add_field(name="🔊 Most traded", value=_mover_rows(snap.most_active, 8, volume=True), inline=False)
    e.set_footer(text=note)
    return fit_embed(e)


DISCLAIMER_SHORT = "past moves, not predictions"
NO_PERIOD_DATA = ("No numbers for this period right now. Moves beyond today come from Yahoo Finance, which isn't "
                  "answering; they come back by themselves when it does (`/status` shows the sources).")


# ----- the NVIDIA channel -----

SENTIMENT = {"positive": ("🟢", GREEN), "negative": ("🔴", RED), "neutral": ("⚪", GREY)}


def _bar_line(b: dict) -> str:
    return (f"Open {fmt_price(b.get('o'))} · high {fmt_price(b.get('h'))} · low {fmt_price(b.get('l'))} · "
            f"close **{fmt_price(b.get('c'))}**")


def rsi_label(v: float) -> str:
    return "overbought" if v >= 70 else "oversold" if v <= 30 else "strong" if v >= 60 else "weak" if v <= 40 else "neutral"


def _n(v) -> float | None:
    """A number from saved or sent data, else None."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) else None


def nvidia_board(q: Quote | None, spot, budget: tuple[int, int] = (0, 5)) -> discord.Embed:
    """The NVIDIA board: the live price (Yahoo) with everything Massive has on the stock. Massive's data shows only
    while Massive is usable (a key it accepts), so nothing stale lingers after a key is removed or rejected."""
    chg = q.change_pct if q else None
    e = discord.Embed(title="🟩 NVIDIA (NVDA) · Live", color=GREEN if (chg or 0) >= 0 else RED)
    desc = []
    if q:
        desc.append(f"**{money(q.price)}** {pct(chg, already_pct=True)} today · {state_label(q) or 'US market'}")
        if q.ext_price and q.ext_change_pct is not None and q.market_state != "REGULAR":
            desc.append(f"Extended hours **{money(q.ext_price)}** ({pct(q.ext_change_pct, already_pct=True)})")
        if q.day_low and q.day_high:
            desc.append(f"Day range {fmt_price(q.day_low)} – {fmt_price(q.day_high)}"
                        + (f" · volume {big(q.volume)}" if q.volume else ""))
    else:
        desc.append("Live price unavailable right now.")
    show = spot.enabled
    if show and spot.snapshot_fresh():
        last = spot.snapshot.get("lastTrade")
        price = _n(last.get("p")) if isinstance(last, dict) else None
        if price:
            desc.append(f"Massive (15-min delayed): {money(price)} "
                        f"{pct(_n(spot.snapshot.get('todaysChangePerc')), already_pct=True)}")
    e.description = "\n".join(desc)
    if show and spot.prev:
        b = spot.prev
        avg = spot.avg_volume()
        v = _n(b.get("v"))
        vol = f"Volume {big(v)}" + (f" ({v / avg:.1f}× its 50-day average)" if avg and v else "")
        vwap = f" · VWAP {fmt_price(b['vw'])}" if _n(b.get("vw")) else ""
        when = datetime.fromtimestamp(b["t"] / 1000, timezone.utc).strftime("%a %b %d") if _n(b.get("t")) else ""
        e.add_field(name=f"Last session ({when})", value=f"{_bar_line(b)}{vwap}\n{vol}", inline=False)
    tech = []
    price = q.price if q else _n((spot.prev or {}).get("c"))
    indicators = spot.indicators if show else {}
    for key, label in (("sma50", "50-day average"), ("sma200", "200-day average"), ("ema20", "20-day EMA")):
        v = _n((indicators.get(key) or {}).get("value"))
        if v and price:
            tech.append(f"{label} {fmt_price(v)} ({'above' if price >= v else 'below'}, {pct(price / v - 1, 1)})")
    rsi = _n((indicators.get("rsi14") or {}).get("value"))
    if rsi is not None:
        tech.append(f"RSI {rsi:.0f} ({rsi_label(rsi)})")
    macd = indicators.get("macd") or {}
    if _n(macd.get("value")) is not None and _n(macd.get("signal")) is not None:
        side = "above" if macd["value"] >= macd["signal"] else "below"
        tech.append(f"MACD {macd['value']:.2f} {side} its signal {macd['signal']:.2f} "
                    f"({'bullish' if side == 'above' else 'bearish'})")
    if tech:
        e.add_field(name="Technicals (at the last close)", value="\n".join(tech), inline=False)
    info = []
    rng = spot.range_52w() if show else None
    if rng and price:
        lo, hi = rng
        pos = (price - lo) / (hi - lo) if hi > lo else 0.5
        info.append(f"52 weeks {fmt_price(lo)} – {fmt_price(hi)} ({pos:.0%} of the way up, {pct(price / hi - 1, 1)} "
                    "from the high)")
    d = (spot.details or {}) if show else {}
    cap, staff = _n(d.get("market_cap")), _n(d.get("total_employees"))
    if cap:
        info.append(f"Market cap ${big(cap)}" + (f" · {staff:,.0f} employees" if staff else ""))
    dv = spot.dividends[0] if show and spot.dividends and isinstance(spot.dividends[0], dict) else {}
    if _n(dv.get("cash_amount")) is not None:
        info.append(f"Dividend ${_n(dv['cash_amount']):g} (ex-date {dv.get('ex_dividend_date') or '?'}, paid "
                    f"{dv.get('pay_date') or '?'})")
    sp = spot.splits[0] if show and spot.splits and isinstance(spot.splits[0], dict) else {}
    if _n(sp.get("split_to")) and _n(sp.get("split_from")):
        info.append(f"Last split {_n(sp['split_to']):g}-for-{_n(sp['split_from']):g} on "
                    f"{sp.get('execution_date') or '?'}")
    if show and spot.related:
        info.append("Related: " + ", ".join(spot.related[:8]))
    if info:
        e.add_field(name="The company", value="\n".join(info), inline=False)
    if show:
        pos_, neu, neg = spot.news_mood()
        lines = []
        for n in spot.news[:4]:
            mood, _ = spot.sentiment(n)
            icon = SENTIMENT.get(mood, ("📰", BLUE))[0]
            url = n.get("article_url") or ""
            title = clip(n.get("title", ""), 95)
            lines.append(f"{icon} [{title}]({url})" if url.startswith("http") else f"{icon} {title}")
        if lines:
            e.add_field(name=f"News · last 48h: {pos_} positive, {neu} neutral, {neg} negative",
                        value="\n".join(lines), inline=False)
    used, limit = budget
    massive = getattr(spot, "massive", None)
    live = f"Live price: {SOURCE_NAMES.get(q.source, q.source) if q else 'Yahoo Finance'}"
    if massive is not None and massive.key and massive.key_rejected:
        foot = f"{live} · Massive rejected the key (see /status)"
    elif not show:
        foot = f"{live} · add MASSIVE_API_KEY for Massive's data"
    else:
        foot = f"{live} · Massive ({spot.plan()}): {used}/{limit} calls in the last minute"
    e.set_footer(text=foot)
    return fit_embed(e)


def nvidia_news(item: dict, mood: str, reasoning: str) -> discord.Embed:
    icon, color = SENTIMENT.get(mood, ("📰", BLUE))
    url = item.get("article_url") if isinstance(item.get("article_url"), str) else ""
    description = item.get("description") if isinstance(item.get("description"), str) else ""
    e = discord.Embed(title=clip(f"{icon} {item.get('title', 'NVIDIA news')}", 256),
                      url=url if url.startswith("http") else None, description=clip(description, 600), color=color)
    if mood:
        e.add_field(name=f"For NVDA: {mood}", value=clip(reasoning or "—", 400), inline=False)
    tickers = item.get("tickers") if isinstance(item.get("tickers"), list) else []
    others = [t for t in tickers if isinstance(t, str) and t != "NVDA"][:8]
    if others:
        e.add_field(name="Also mentions", value=", ".join(others), inline=False)
    publisher = item.get("publisher") if isinstance(item.get("publisher"), dict) else {}
    pub = publisher.get("name") if isinstance(publisher.get("name"), str) else "Massive news"
    e.set_footer(text=f"{pub} · via Massive")
    if isinstance(item.get("published_utc"), str) and item["published_utc"]:
        try:
            e.timestamp = datetime.fromisoformat(item["published_utc"].replace("Z", "+00:00"))
        except ValueError:
            pass
    if isinstance(item.get("image_url"), str) and item["image_url"].startswith("http"):
        e.set_thumbnail(url=item["image_url"])
    return fit_embed(e)
