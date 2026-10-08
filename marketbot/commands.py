"""Slash commands."""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from datetime import datetime, timezone

import discord
import numpy as np
from discord import app_commands

from . import briefs, charts, embeds as E, stats
from .briefs import Post, chart_post
from .channels import KIND_NAMES, KINDS, MAX_WATCHLIST, ChannelConfig
from .engine import Resolved, SourcesDown, UnknownSymbol
from .apis import ApiError
from .http import HttpError
from .limits import MESSAGE, clip, fit_embed
from .news import analyse
from .setups import fmt_price
from .universe import (ALIASES, BENCHMARK, CRYPTO, DEFAULT_CRYPTO, DEFAULT_STOCKS, INDICES, SECTORS, STOCKS,
                       display_name, normalize, short, tag, title_of)
from .yahoo import YahooError

log = logging.getLogger("marketbot")

Symbol = app_commands.Range[str, 1, 40]
KIND_CHOICES = [app_commands.Choice(name=KIND_NAMES[k], value=k) for k in KINDS]
MARKET_CHOICES = [app_commands.Choice(name="Stocks", value=STOCKS), app_commands.Choice(name="Crypto", value=CRYPTO)]
PERIODS = {"1D": None, "5D": None, "1M": 22, "3M": 66, "6M": 126, "1Y": 252, "5Y": 1260, "MAX": 10**6}
SETUP_CHANNELS = (("stocks", "📈-stocks", "Live US market board, big-move alerts, breakout setups, pre-market and closing briefs."),
                  ("crypto", "🪙-crypto", "Live crypto board (24/7), fast-move alerts, breakout setups and a daily brief."),
                  ("news", "📰-market-news", "Market-moving headlines with the expected impact on each market."),
                  ("research", "🔬-research", "Daily research digest, weekly outlook, and room for /research deep dives."),
                  ("trends", "🔥-trends", "Top gainers, losers and most traded: today, this week, this month, sectors and crypto."),
                  ("nvidia", "🟩-nvidia", "NVIDIA all day: live price, Massive data, technicals, news with sentiment, briefs."),
                  ("congress", "🏛️-smart-money", "Congress members' trades, insider buying and big funds' moves."),
                  ("calendar", "📅-calendar", "Economic releases and earnings: what usually happens before, what happened after."),
                  ("league", "🏆-league", "Paper trading and predictions: graded calls and the weekly leaderboard."))
INTROS = {
    "stocks": "This channel gets a **live stock board** (pinned, updated every minute), alerts for big moves, new "
              "52-week highs and breakout setups on the watchlist, a **pre-market brief** at 9:00 ET and a "
              "**closing recap** at 4:10 ET on trading days. Change the list with `/watchlist`.",
    "crypto": "This channel gets a **live crypto board** (pinned, 24/7), alerts for big 24-hour and 1-hour moves, "
              "breakout setups, and a **daily crypto brief**. Change the list with `/watchlist`.",
    "news": "This channel gets market-moving headlines from a dozen sources, each with **what it should move, which "
            "way and roughly how much**, plus a morning headline digest. `/settings news_level:` sets how picky it is.",
    "research": "This channel gets a **research digest** after every US close (the strongest setups, with deep "
                "dives) and a **week-ahead outlook** every Sunday evening. Run `/research` here any time.",
    "trends": "This channel gets a **live trends board** (pinned, every 5 minutes while the market is open): today's "
              "top gainers, losers and most traded US stocks, sectors, the week's leaders and crypto. Plus a **daily "
              "recap** after the close, a **weekly recap** on the week's last trading day, a **monthly recap** on "
              "the month's, and a **crypto recap** after midnight UTC. `/trends` shows any period.",
    "nvidia": "This channel follows **NVIDIA (NVDA)**: a live board (price from Yahoo, plus Massive's last-session "
              "data, technicals, company facts and news sentiment), alerts at ±2%, 3%, 4%, 5%…, Massive's NVIDIA "
              "news as it comes, a **pre-market brief** at 9:05 ET and a **closing recap** at 4:15 ET. Massive is "
              "used for NVIDIA only, at most 5 calls a minute.",
    "congress": "This channel follows the **smart money**: what **members of Congress** buy and sell (official House "
                "and Senate disclosures, a pinned board with the latest filings, the best stock pickers and the most "
                "bought stocks, and a post for every new filing), **insiders buying** their own company's stock "
                "(SEC Form 4, $100K+), and **big funds' quarterly moves** (13F: Buffett, Burry, Ackman, Dalio…). "
                "`/congress`, `/insiders` and `/fund` any time. Disclosures come days to weeks after the trades.",
    "calendar": "This channel gets the **market calendar**: a **week-ahead preview** on Sunday evening, each trading "
                "day's **agenda** at 7:45 ET (economic releases with forecasts and what the S&P 500 usually did on "
                "those days; big earnings with the options market's expected move and the last 4 reactions), "
                "**results** as releases come out (actual vs forecast), and **earnings reactions** after the open "
                "and the close. `/calendar` and `/earnings` any time.",
    "league": "This channel is the **league**: everyone gets **$100,000 of pretend money** to trade stocks, ETFs and "
              "coins at live prices (`/paper buy`, `/paper sell`, `/paper portfolio`), and can **predict** a stock or "
              "coin up or down over a day, a week or a month (`/call`). Calls are graded here when time's up, and "
              "the **standings** are posted after each week's last close. `/league` shows them any time.",
}


def _png_file(name: str, data: bytes) -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


async def _within(seconds: float, job):
    """The job's result if it finishes in time (Discord drops suggestions after 3 seconds), else None."""
    task = asyncio.ensure_future(job)
    done, _ = await asyncio.wait([task], timeout=seconds)
    if task in done and not task.cancelled() and task.exception() is None:
        return task.result()
    return None


def sources_down_message(reason: str) -> str:
    return (f"⚠️ The market data sources aren't answering right now ({clip(reason, 160)}), so I can't get that. "
            "I keep retrying by myself: try again in a minute, and `/status` shows each source's state.")


HELP_GUIDE = (
    ("⚙️ Setup", (("setup", "makes all the market channels"), ("channel", "turns this channel into a market channel"),
                  ("settings", "this channel's alerts, news and brief time"),
                  ("watchlist", "adds or removes symbols: `AAPL, tesla, BTC`"))),
    ("💹 Prices & charts", (("price", "live price of anything: `nvidia`, `BTC`, `gold`"),
                           ("chart", "chart with averages, bands, levels and RSI"),
                           ("compare", "two symbols side by side: returns, risk, correlation"))),
    ("🔬 Analysis", (("forecast", "breakout odds, likely ranges and look-alikes"),
                    ("research", "full report on a stock, ETF, index or coin"),
                    ("breakouts", "breakouts and setups about to break"),
                    ("history", "decades, crashes and seasonality since the 1800s"),
                    ("backtest", "would following the model have beaten buy-and-hold?"))),
    ("🌐 Markets", (("movers", "today's biggest gainers and losers"),
                   ("trends", "top movers: today, this week or month, 3 months, YTD, 1 year"),
                   ("feargreed", "Fear & Greed for stocks (CNN) and crypto, with a year's chart"),
                   ("macro", "rates, dollar, VIX, Fear & Greed and S&P valuation"),
                   ("nvidia", "NVIDIA: live price, Massive's data, news and outlook"),
                   ("news", "market-moving news, or news for one symbol"))),
    ("🔔 Alerts & tracking", (("alert", "pings you when a price is reached"), ("alerts", "lists or removes price alerts"),
                             ("record", "how the bot's calls have done"), ("brief", "posts a brief here now"))),
    ("🤖 Bot", (("status", "data sources, jobs, models and the news reader"), ("help", "this guide"))),
)


async def command_ids(bot) -> dict[str, int]:
    """The commands' Discord IDs (for tappable mentions), fetched once; empty if Discord doesn't answer."""
    ids = getattr(bot, "command_ids", None) or {}
    if not ids:
        try:
            ids = {c.name: c.id for c in await bot.tree.fetch_commands()}
        except Exception:
            log.warning("Couldn't fetch the command IDs", exc_info=True)
            return {}
        bot.command_ids = ids
    return ids


def mention(ids: dict[str, int], name: str) -> str:
    """A command as Discord shows it (blue and tappable) when its ID is known, else as /name."""
    top = name.split(" ")[0]  # a subcommand ("paper buy") is mentioned with its group's ID
    return f"</{name}:{ids[top]}>" if top in ids else f"`/{name}`"


def register_commands(bot) -> None:
    tree = bot.tree

    async def reply(interaction: discord.Interaction, msg: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def resolve(interaction: discord.Interaction, text: str) -> Resolved | None:
        try:
            return await bot.engine.resolve(text)
        except UnknownSymbol:
            await reply(interaction, f"I couldn't find **{clip(text, 40)}**. Try a ticker like `AAPL`, `BTC`, `^GSPC` "
                                     "or a name like `nvidia`.")
        except SourcesDown as exc:
            await reply(interaction, sources_down_message(str(exc)))
        return None

    async def symbol_suggestions(interaction: discord.Interaction, current: str):
        q = current.strip()
        out: list[tuple[str, str]] = []
        if not q:
            cfg = bot.channels.get(interaction.channel_id)
            picks = (cfg.symbols()[:12] if cfg and cfg.market else []) + ["^GSPC", "^IXIC", "BTC-USD", "ETH-USD"]
            out = [(f"{short(s)} · {display_name(s)}", s) for s in dict.fromkeys(picks)]
        else:
            if q.lower() in ALIASES:
                s = ALIASES[q.lower()]
                out.append((f"{short(s)} · {display_name(s)}", s))
            found = await _within(2.5, bot.engine.suggest(q)) or []
            out += found
            guess = normalize(q)
            if not out and guess:
                out.append((guess, guess))
        seen, choices = set(), []
        for label, value in out:
            if value in seen:
                continue
            seen.add(value)
            choices.append(app_commands.Choice(name=label[:100], value=value[:100]))
        return choices[:25]

    bot.resolve_symbol = resolve  # for the add-ons' commands
    bot.symbol_suggestions = symbol_suggestions
    def channel_market(interaction: discord.Interaction) -> str | None:
        cfg = bot.channels.get(interaction.channel_id)
        return cfg.market if cfg else None

    # ----- channel setup -----

    @tree.command(name="setup", description="Create the market channels: stocks, crypto, news, research, trends, NVIDIA")
    @app_commands.describe(category="Name of the category to put them in")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def setup(interaction: discord.Interaction, category: app_commands.Range[str, 1, 60] = "📊 Markets"):
        guild = interaction.guild
        me = guild.me if guild else None
        if not me or not me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission to create channels. Add it to my role, or make channels "
                "yourself and run `/channel` in each.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cat = discord.utils.get(guild.categories, name=category) or await guild.create_category(category)
        made = []
        for kind, name, topic in SETUP_CHANNELS:
            channel = discord.utils.get(cat.text_channels, name=name)
            if channel is None:
                channel = await guild.create_text_channel(name, category=cat, topic=topic)
            made.append(channel)
            existing = bot.channels.get(channel.id)
            if existing and existing.kind == kind:
                continue  # already set up (running /setup again adds new channels without touching these)
            bot.channels.set(channel.id, kind, guild.id)
            await bot.send(channel.id, Post([discord.Embed(title=f"{KIND_NAMES[kind]} channel", description=INTROS[kind],
                                                           color=E.BLUE)]))
        for job in ("live", "crypto_data", "mood", "trends", "nvidia"):
            bot._last.pop(job, None)
        bot._trends_board_at = 0.0  # a new trends channel gets its board within a minute
        await interaction.followup.send("Done: " + " ".join(c.mention for c in made) +
                                        "\nBoards appear within a minute. Pin permission (Manage Messages) keeps the "
                                        "boards pinned.", ephemeral=True)

    @tree.command(name="channel", description="Make this channel a stocks, crypto, news, research, trends or NVIDIA channel")
    @app_commands.describe(kind="What this channel is for", enabled="Off: stop all posts here")
    @app_commands.choices(kind=KIND_CHOICES)
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def channel_cmd(interaction: discord.Interaction, kind: app_commands.Choice[str], enabled: bool = True):
        if not enabled:
            removed = bot.channels.remove(interaction.channel_id)
            await interaction.response.send_message("Stopped all market posts here." if removed else
                                                    "This channel wasn't set up for anything.", ephemeral=True)
            return
        bot.channels.set(interaction.channel_id, kind.value, interaction.guild_id or 0)
        for job in ("live", "trends", "nvidia"):
            bot._last.pop(job, None)
        bot._trends_board_at = 0.0
        await interaction.response.send_message(embed=discord.Embed(
            title=f"{KIND_NAMES[kind.value]} channel", description=INTROS[kind.value], color=E.BLUE))

    @tree.command(name="settings", description="This channel's alert, news and brief settings")
    @app_commands.describe(alerts="Big-move, 52-week-high and breakout alerts (stocks/crypto channels)",
                           news_level="News channels: how important a story must be to post",
                           briefs="Scheduled briefs and digests", brief_hour="Crypto channels: hour of the daily brief",
                           timezone="Crypto channels: time zone for the brief hour, e.g. Europe/London")
    @app_commands.choices(news_level=[app_commands.Choice(name="Major only (75+)", value="major"),
                                      app_commands.Choice(name="Important (55+)", value="important"),
                                      app_commands.Choice(name="Most stories (35+)", value="all")])
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def settings(interaction: discord.Interaction, alerts: bool | None = None,
                       news_level: app_commands.Choice[str] | None = None, briefs: bool | None = None,
                       brief_hour: app_commands.Range[int, 0, 23] | None = None,
                       timezone: app_commands.Range[str, 1, 60] | None = None):
        cfg = bot.channels.get(interaction.channel_id)
        if cfg is None:
            await interaction.response.send_message("Set this channel up first with `/channel` or `/setup`.",
                                                    ephemeral=True)
            return
        changes = {}
        if alerts is not None:
            changes["alerts"] = alerts
        if news_level is not None:
            changes["news_level"] = news_level.value
        if briefs is not None:
            changes["briefs"] = briefs
        if brief_hour is not None:
            changes["brief_hour"] = brief_hour
        if timezone is not None:
            if bot._zone(timezone).key != timezone:
                await interaction.response.send_message(f"Unknown time zone `{timezone}`.", ephemeral=True)
                return
            changes["timezone"] = timezone
        if changes:
            cfg = bot.channels.update(interaction.channel_id, **changes)
        await interaction.response.send_message(
            f"{KIND_NAMES[cfg.kind]} channel · alerts **{'on' if cfg.alerts else 'off'}** · briefs "
            f"**{'on' if cfg.briefs else 'off'}** · news level **{cfg.news_level}** · crypto brief at "
            f"**{cfg.brief_hour}:00 {cfg.timezone}**", ephemeral=True)

    @tree.command(name="watchlist", description="Show or change this channel's watchlist")
    @app_commands.describe(action="What to do", symbols="Tickers or names, separated by commas (e.g. AAPL, tesla, BTC)")
    @app_commands.choices(action=[app_commands.Choice(name=n, value=n) for n in ("show", "add", "remove", "reset")])
    @app_commands.guild_only()
    async def watchlist(interaction: discord.Interaction, action: app_commands.Choice[str],
                        symbols: app_commands.Range[str, 1, 300] | None = None):
        cfg = bot.channels.get(interaction.channel_id)
        if cfg is None or cfg.market is None:
            await interaction.response.send_message("Watchlists belong to stocks and crypto channels: run this in "
                                                    "one (or `/channel` to make one).", ephemeral=True)
            return
        if action.value != "show" and not interaction.permissions.manage_channels:
            await interaction.response.send_message("Changing the watchlist needs Manage Channels.", ephemeral=True)
            return
        current = cfg.symbols()
        if action.value == "reset":
            bot.channels.update(interaction.channel_id, watchlist=())
            current = ChannelConfig(cfg.kind).symbols()
        elif action.value in ("add", "remove"):
            if not symbols:
                await interaction.response.send_message("List the symbols too, e.g. `symbols: AAPL, TSLA`.",
                                                        ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            found, missing = [], []
            for text in [t for t in symbols.replace(";", ",").split(",") if t.strip()][:15]:
                try:
                    r = await bot.engine.resolve(text)
                    found.append(r)
                except (UnknownSymbol, SourcesDown):
                    missing.append(text.strip())
            wrong = [r for r in found if r.market != cfg.market]
            found = [r for r in found if r.market == cfg.market]
            if action.value == "add":
                current = list(dict.fromkeys(current + [r.symbol for r in found]))[:MAX_WATCHLIST]
            else:
                drop = {r.symbol for r in found}
                current = [s for s in current if s not in drop]
            bot.channels.update(interaction.channel_id, watchlist=tuple(current))
            bot._last.pop("live", None)
            notes = []
            if missing:
                notes.append("Not found: " + ", ".join(missing))
            if wrong:
                notes.append(f"Not {cfg.market}: " + ", ".join(r.symbol for r in wrong))
            await interaction.followup.send(f"Watchlist ({len(current)}): " + ", ".join(short(s) for s in current)
                                            + ("\n" + "\n".join(notes) if notes else ""), ephemeral=True)
            return
        await interaction.response.send_message(clip(f"Watchlist ({len(current)}): " +
                                                     ", ".join(short(s) for s in current), MESSAGE), ephemeral=True)

    # ----- prices and charts -----

    @tree.command(name="price", description="Live price of a stock, index, ETF, coin or commodity")
    @app_commands.describe(symbol="Ticker or name, e.g. AAPL, BTC, gold, s&p")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    async def price(interaction: discord.Interaction, symbol: Symbol):
        await interaction.response.defer(thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        q = await bot.engine.quote(r.symbol)
        if not q:
            await interaction.followup.send(f"No live price for {r.symbol} right now.")
            return
        extra = []
        try:
            bars = await bot.engine.history(r.symbol, q)
            perf = stats.performance(bars)
            extra.append(" · ".join(f"{k} {E.pct(perf.returns[k], 1)}" for k in ("1W", "1M", "YTD", "1Y")
                                    if perf.returns.get(k) is not None))
        except Exception:
            pass
        embed = E.quote_embed(q, extra)
        try:
            intraday = await bot.engine.data.intraday(r.symbol, "1d", "5m")
            if len(intraday) > 5:
                png = await bot.engine.run(charts.intraday_chart, intraday, f"{title_of(r.symbol, r.name)} · today", q.prev_close)
                embed.set_image(url="attachment://intraday.png")
                await interaction.followup.send(embed=embed, file=_png_file("intraday.png", png))
                return
        except Exception:
            log.warning("Intraday chart for %s failed", r.symbol, exc_info=True)
        await interaction.followup.send(embed=embed)

    @tree.command(name="chart", description="Price chart with averages, Bollinger Bands, levels and RSI")
    @app_commands.describe(symbol="Ticker or name", period="How far back")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    @app_commands.choices(period=[app_commands.Choice(name=p, value=p) for p in PERIODS])
    async def chart(interaction: discord.Interaction, symbol: Symbol, period: app_commands.Choice[str] | None = None):
        await interaction.response.defer(thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        p = period.value if period else "6M"
        if PERIODS[p] is None:
            bars = await bot.engine.data.intraday(r.symbol, "1d" if p == "1D" else "5d", "5m" if p == "1D" else "15m")
            q = await bot.engine.quote(r.symbol)
            png = await bot.engine.run(charts.intraday_chart, bars, f"{title_of(r.symbol, r.name)} · {p}",
                                       q.prev_close if q and p == "1D" else (float(bars.close[0]) if len(bars) else None))
        else:
            q = await bot.engine.quote(r.symbol)
            bars = await bot.engine.history(r.symbol, q)
            o = None
            if PERIODS[p] <= 252:
                try:
                    o = await bot.engine.outlook(r.symbol, r.market, quote=q, name=r.name)
                except UnknownSymbol:
                    pass  # too new for a forecast: the chart goes out without the cone
            png = await bot.engine.run(lambda: charts.price_chart(
                bars, title_of(r.symbol, r.name), o.cone if o else None, o.resistance if o else None,
                o.support if o else None, None, min(PERIODS[p], len(bars))))
        embed = discord.Embed(title=f"📊 {title_of(r.symbol, r.name)} · {p}", color=E.BLUE)
        embed.set_image(url="attachment://chart.png")
        embed.set_footer(text="/forecast for odds and ranges · /research for the full report")
        await interaction.followup.send(embed=embed, file=_png_file("chart.png", png))

    # ----- forecasts and research -----

    @tree.command(name="forecast", description="Where it could go: breakout odds, ranges, look-alikes and setups")
    @app_commands.describe(symbol="Ticker or name, e.g. NVDA, BTC, s&p")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    async def forecast(interaction: discord.Interaction, symbol: Symbol):
        await interaction.response.defer(thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        try:
            o = await bot.engine.outlook(r.symbol, r.market, name=r.name)
        except UnknownSymbol as exc:
            await interaction.followup.send(str(exc))
            return
        post = await bot.engine.run(chart_post, o, E.outlook_embed(o), o.hi20)
        await interaction.followup.send(embeds=post.embeds, files=[_png_file(n, d) for n, d in post.files])

    @tree.command(name="research", description="Full automated research report on a stock, ETF, index or coin")
    @app_commands.describe(symbol="Ticker or name, e.g. AAPL, PLTR, ETH")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    async def research(interaction: discord.Interaction, symbol: Symbol):
        await interaction.response.defer(thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        try:
            o = await bot.engine.outlook(r.symbol, r.market, name=r.name)
        except UnknownSymbol as exc:
            await interaction.followup.send(str(exc))
            return
        post = await bot.engine.run(chart_post, o, E.outlook_embed(o), o.hi20)
        await interaction.followup.send(embeds=post.embeds, files=[_png_file(n, d) for n, d in post.files])
        is_company = r.market == STOCKS and not r.symbol.startswith("^") and "=" not in r.symbol
        fund_task = bot.engine.fundamentals(r.symbol) if is_company else asyncio.sleep(0, {})
        opt_task = bot.engine.options(r.symbol, o.bars) if r.market == STOCKS and "=" not in r.symbol \
            and not r.symbol.startswith("^") else asyncio.sleep(0, None)
        news_task = bot.news.for_symbol(r.symbol, r.market, 10)
        fundamentals, options, headlines = await asyncio.gather(fund_task, opt_task, news_task, return_exceptions=True)
        fundamentals = fundamentals if isinstance(fundamentals, dict) else {}
        options = options if not isinstance(options, BaseException) else None
        headlines = headlines if isinstance(headlines, list) else []
        extra = {"news": sorted((analyse(h, bot.vol_ratio, bot.impacts.calibration) for h in headlines),
                                key=lambda a: -a.headline.published)}
        month = datetime.now(timezone.utc).month
        seasons = {s.month: s for s in stats.seasonality(o.bars)} if len(o.bars) > 500 else {}
        if month in seasons and seasons[month].n >= 5:
            extra["season"] = seasons[month]
        bench = BENCHMARK[r.market]
        if bench != r.symbol:
            try:
                extra["beta"] = stats.beta_corr(o.bars, await bot.engine.cache.daily(bench))
                extra["bench_name"] = display_name(bench)
            except Exception:
                pass
        if r.symbol == "BTC-USD":
            extra["halving"] = stats.halving_cycle(o.bars)
        await interaction.followup.send(embed=E.snapshot_embed(o, fundamentals, options, extra))

    @tree.command(name="breakouts", description="Scan for breakouts and coiled setups that could break soon")
    @app_commands.describe(market="Which market (leave out for this channel's)")
    @app_commands.choices(market=MARKET_CHOICES)
    async def breakouts(interaction: discord.Interaction, market: app_commands.Choice[str] | None = None):
        await interaction.response.defer(thinking=True)
        mk = market.value if market else channel_market(interaction) or STOCKS
        cfg = bot.channels.get(interaction.channel_id)
        own = cfg.symbols() if cfg and cfg.market == mk else []
        if mk == STOCKS:
            symbols = own + DEFAULT_STOCKS + list(SECTORS) + [a.symbol for a in INDICES if a.symbol != "^VIX"]
            try:
                symbols += [q["symbol"] for q in await bot.engine.data.screener("most_actives", 25) if q.get("symbol")]
            except Exception:
                pass
        else:
            symbols = own + DEFAULT_CRYPTO
            try:
                coins = await bot.engine.sources.top_coins(100)
                symbols += [f"{c.symbol}-USD" for c in coins[:40] if c.symbol not in ("USDT", "USDC", "DAI", "USDE",
                                                                                        "FDUSD", "STETH", "WBTC")]
            except Exception:
                pass
        symbols = list(dict.fromkeys(symbols))[:80]
        quotes = await bot.engine.data.quotes(symbols)
        if not quotes:
            await interaction.followup.send(sources_down_message(bot.engine.data.outage() or "no prices came back"))
            return
        hits = await bot.engine.scan([s for s in symbols if s in quotes], quotes, lookback=3)
        hits = [h for h in hits if h.pressure > 0]
        embed = E.scan_embed(hits, mk)
        embed.description = (f"Scanned {len(quotes)} {mk} for fresh breakouts (last 3 sessions), coils, flags and "
                             f"squeezes, ranked by breakout pressure.\n\n" + (embed.description or ""))
        if hits:
            top = hits[0]
            o = await bot.engine.outlook(top.symbol, top.market, quote=quotes.get(top.symbol))
            png = await bot.engine.run(charts.price_chart, o.bars, f"{title_of(o.symbol, o.name)} · top setup", o.cone,
                                       o.resistance, o.support, o.hi20)
            embed.set_image(url="attachment://radar.png")
            await interaction.followup.send(embed=fit_embed(embed), file=_png_file("radar.png", png))
        else:
            await interaction.followup.send(embed=fit_embed(embed))

    @tree.command(name="news", description="Latest market-moving news with expected impact (or news for one symbol)")
    @app_commands.describe(symbol="A ticker or name for its news (leave out for the market)", market="Which market")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    @app_commands.choices(market=MARKET_CHOICES)
    async def news(interaction: discord.Interaction, symbol: Symbol | None = None,
                   market: app_commands.Choice[str] | None = None):
        await interaction.response.defer(thinking=True)
        if symbol:
            r = await resolve(interaction, symbol)
            if not r:
                return
            items = [analyse(h, bot.vol_ratio, bot.impacts.calibration) for h in await bot.news.for_symbol(r.symbol, r.market)]
            title = f"📰 {short(r.symbol)} news"
        else:
            mk = market.value if market else channel_market(interaction)
            markets = {CRYPTO} if mk == CRYPTO else {STOCKS, "macro"} if mk == STOCKS else {STOCKS, CRYPTO, "macro"}
            items = [a for a in bot.recent_news if time.time() - a.headline.published < 24 * 3600]
            if len(items) < 5:
                items = [analyse(h, bot.vol_ratio, bot.impacts.calibration) for h in await bot.news.latest()]
            items = sorted((a for a in items if a.market in markets and not a.opinion), key=lambda a: -a.importance)
            title = "📰 Market-moving news (last 24h)"
        if not items:
            await interaction.followup.send("No news found.")
            return
        top = [a for a in items if a.impacts][:2]
        embeds = [E.news_digest(items[:12], title)] + [E.news_embed(a) for a in top]
        while sum(len(e) for e in embeds) > 5800 and len(embeds) > 1:
            embeds.pop()
        await interaction.followup.send(embeds=embeds)

    @tree.command(name="history", description="The long view: decades, crashes, seasonality and cycles since the 1800s")
    @app_commands.describe(symbol="Ticker or name (S&P 500 goes back to 1871)")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    async def history(interaction: discord.Interaction, symbol: Symbol = "^GSPC"):
        await interaction.response.defer(thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        bars = await bot.engine.cache.daily(r.symbol)
        if len(bars) < 300:
            await interaction.followup.send(f"{r.symbol} doesn't have a long enough history yet.")
            return

        def build():
            perf = stats.performance(bars)
            years = stats.calendar_years(bars)
            pres = stats.presidential_cycle(years) if r.market == STOCKS and r.symbol.startswith("^") else None
            note = f"Daily data since {perf.first:%Y} (Yahoo Finance)"
            m, closes = stats.month_ends(bars)
            t = 1970 + m.astype(int) / 12
            p = closes
            return perf, years, pres, note, t, p
        perf, years, pres, note, t, p = await bot.engine.run(build)
        marks = []
        if r.symbol == "^GSPC":
            lr = await bot.engine.long_run()
            if lr is not None:
                t, p = stats.long_run_monthly(lr, bars)
                long_years = stats.long_run_years(t, p)
                pres = stats.presidential_cycle(long_years)
                note = "Daily since 1927 (Yahoo) · monthly since 1871 (Robert Shiller) for the cycle and chart"
                marks = [(1929.7, "1929 crash"), (1932.5, "1932 low"), (1987.8, "1987"), (2000.2, "Dot-com"),
                         (2009.2, "2009 low"), (2020.2, "Covid")]
        embed = E.history_embed(r.symbol, r.name, perf, stats.drawdowns(bars, 0.2 if r.market == STOCKS else 0.5),
                                years, stats.seasonality(bars), stats.decades(bars), pres, note)
        png = await bot.engine.run(charts.long_run_chart, t, p, title_of(r.symbol, r.name),
                                   f"Monthly, log scale, since {int(t[0])} · shaded: more than 20% below a high", marks)
        embed.set_image(url="attachment://history.png")
        await interaction.followup.send(embed=embed, file=_png_file("history.png", png))

    @tree.command(name="macro", description="Rates, dollar, VIX, fear & greed, crypto market and S&P valuation")
    async def macro(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        m = await bot.engine.macro()
        bot.macro_cache = m
        cape = None
        lr = await bot.engine.long_run()
        if lr is not None and "^GSPC" in m.quotes:
            now = datetime.now(timezone.utc)
            cape = stats.cape_view(lr, m.quotes["^GSPC"].price, now.year + (now.timetuple().tm_yday - 1) / 365.25)
        await interaction.followup.send(embed=E.macro_embed(m, cape))

    @tree.command(name="feargreed", description="Fear & Greed for US stocks (CNN) and crypto: now, a week, month and year ago")
    async def feargreed(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        m = await bot.engine.macro()
        bot.macro_cache = m
        embed = E.fear_greed_embed(m.stock_fg, m.crypto_fg, m.fng_history)
        stocks = m.stock_fg.history if m.stock_fg else []
        crypto = m.crypto_fg.history if m.crypto_fg else []
        if len(stocks) > 1 or len(crypto) > 1:
            try:
                png = await bot.engine.run(charts.fear_greed_chart, stocks, crypto, "Fear & Greed · the past year",
                                           "0 extreme fear · 100 extreme greed · shaded: the fear and greed zones")
            except Exception:
                log.warning("Fear & Greed chart failed", exc_info=True)
            else:
                embed.set_image(url="attachment://feargreed.png")
                await interaction.followup.send(embed=embed, file=_png_file("feargreed.png", png))
                return
        await interaction.followup.send(embed=embed)

    @tree.command(name="trends", description="Biggest gainers and losers: today, this week or month, 3 months, YTD, a year")
    @app_commands.describe(period="Over what time", market="Stocks, sectors & ETFs, or crypto")
    @app_commands.choices(period=[app_commands.Choice(name=n, value=v) for v, n in (
        ("1D", "Today"), ("WTD", "This week (since last Friday's close)"), ("MTD", "This month"),
        ("1W", "Past 5 sessions"), ("1M", "Past 21 sessions"), ("3M", "3 months"), ("YTD", "Year to date"),
        ("1Y", "1 year"))],
        market=[app_commands.Choice(name=n, value=v) for v, n in (
            ("stocks", "Stocks"), ("sectors", "Sectors & ETFs"), ("crypto", "Crypto"))])
    async def trends(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None,
                     market: app_commands.Choice[str] | None = None):
        await interaction.response.defer(thinking=True)
        p = period.value if period else "1D"
        mk = market.value if market else ("crypto" if channel_market(interaction) == CRYPTO else "stocks")
        snap = await bot.trends.refresh(max_age=180)
        if not snap.changes and not snap.day_gainers and not snap.coins:
            await interaction.followup.send(sources_down_message(bot.engine.data.outage() or "no data came back"))
            return
        embed = E.trends_embed(snap, p, mk, bot.trends.index_members("sp500"), bot.trends.index_members("ndx100"))
        await interaction.followup.send(embed=embed)

    @tree.command(name="nvidia", description="NVIDIA right now: live price, Massive's data, technicals, news and outlook")
    async def nvidia(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        q = await bot.engine.quote("NVDA")
        board = E.nvidia_board(q, bot.spotlight, (bot.massive_used(), 5))
        try:
            o = await bot.engine.outlook("NVDA", STOCKS, quote=q, name="NVIDIA Corporation")
        except (UnknownSymbol, YahooError, HttpError) as exc:
            log.warning("NVIDIA outlook failed: %s", exc)
            await interaction.followup.send(embed=board)
            return
        post = await bot.engine.run(chart_post, o, E.outlook_embed(o), o.hi20)
        await interaction.followup.send(embeds=[board] + post.embeds, files=[_png_file(n, d) for n, d in post.files])

    @tree.command(name="movers", description="Today's biggest gainers and losers")
    @app_commands.choices(market=MARKET_CHOICES)
    async def movers(interaction: discord.Interaction, market: app_commands.Choice[str] | None = None):
        await interaction.response.defer(thinking=True)
        mk = market.value if market else channel_market(interaction) or STOCKS
        if mk == STOCKS:
            g = await bot.engine.data.screener("day_gainers", 10)
            l = await bot.engine.data.screener("day_losers", 10)
            conv = lambda rows: [(q["symbol"], q.get("shortName", ""), q.get("regularMarketPrice") or 0,
                                  q.get("regularMarketChangePercent") or 0) for q in rows if q.get("symbol")]
            embed = E.movers_embed("🏁 US stock movers", conv(g), conv(l), "US stocks over $2B market cap · Yahoo Finance")
        else:
            coins = [c for c in await bot.engine.sources.top_coins(100) if c.change_24h is not None]
            coins.sort(key=lambda c: -c.change_24h)
            conv = lambda rows: [(c.symbol, c.name, c.price, c.change_24h) for c in rows]
            embed = E.movers_embed("🏁 Crypto movers (top 100, 24h)", conv(coins[:10]), conv(coins[::-1][:10]),
                                   "CoinGecko")
        await interaction.followup.send(embed=embed)

    @tree.command(name="compare", description="Compare two symbols: returns, risk and how they move together")
    @app_commands.autocomplete(first=symbol_suggestions, second=symbol_suggestions)
    async def compare(interaction: discord.Interaction, first: Symbol, second: Symbol):
        await interaction.response.defer(thinking=True)
        a, b = await resolve(interaction, first), await resolve(interaction, second)
        if not a or not b:
            return
        ba, bb = await asyncio.gather(bot.engine.cache.daily(a.symbol), bot.engine.cache.daily(b.symbol))
        pa, pb = stats.performance(ba), stats.performance(bb)
        rows = []
        for k in ("1M", "3M", "YTD", "1Y", "3Y", "5Y", "10Y"):
            if pa.returns.get(k) is not None and pb.returns.get(k) is not None:
                win = "◀" if pa.returns[k] > pb.returns[k] else "▶"
                rows.append(f"`{k:>3}` {E.pct(pa.returns[k], 1):>8} {win} {E.pct(pb.returns[k], 1)}")
        embed = discord.Embed(title=f"⚖️ {short(a.symbol)} vs {short(b.symbol)}", color=E.BLUE,
                              description=f"**{a.name}** ◀ ▶ **{b.name}**\n" + "\n".join(rows))
        embed.add_field(name="Risk", value=f"{short(a.symbol)}: volatility {pa.vol:.0%}, worst fall {pa.max_dd.depth:.0%}\n"
                                           f"{short(b.symbol)}: volatility {pb.vol:.0%}, worst fall {pb.max_dd.depth:.0%}",
                        inline=False)
        bc = stats.beta_corr(ba, bb)
        if bc:
            embed.add_field(name="Moving together (1 year)", value=f"Correlation **{bc[1]:.2f}** · beta of "
                                                                   f"{short(a.symbol)} to {short(b.symbol)} {bc[0]:.2f}",
                            inline=False)
        common, ia, ib = np.intersect1d(stats.days(ba), stats.days(bb), return_indices=True)
        files = []
        if len(common) > 30:
            ia, ib = ia[-252:], ib[-252:]
            png = await bot.engine.run(lambda: charts.equity_chart(
                ba.t[ia], ba.close[ia] / ba.close[ia][0], bb.close[ib] / bb.close[ib][0],
                f"{short(a.symbol)} vs {short(b.symbol)} · last year", "Growth of 1 (log scale)",
                (a.name, b.name), (short(a.symbol), short(b.symbol))))
            embed.set_image(url="attachment://compare.png")
            files.append(_png_file("compare.png", png))
        await interaction.followup.send(embed=fit_embed(embed), files=files)

    @tree.command(name="backtest", description="Would following the model have beaten buy-and-hold? (walk-forward test)")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    async def backtest(interaction: discord.Interaction, symbol: Symbol):
        await interaction.response.defer(thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        bt = await bot.engine.backtest(r.symbol, r.market)
        if bt is None:
            await interaction.followup.send(f"{r.symbol} doesn't have enough history to backtest.")
            return
        embed = E.backtest_embed(r.symbol, r.name, bt)
        png = await bot.engine.run(charts.equity_chart, bt.t, bt.equity, bt.hold,
                                   f"{tag(r.symbol)} · model timing vs buy and hold",
                                   f"Walk-forward since {bt.start_year}: each year predicted by a model trained on earlier years only")
        embed.set_image(url="attachment://backtest.png")
        await interaction.followup.send(embed=embed, file=_png_file("backtest.png", png))

    # ----- alerts -----

    @tree.command(name="alert", description="Ping me here when a price is reached")
    @app_commands.describe(symbol="Ticker or name", price="The price to watch for")
    @app_commands.autocomplete(symbol=symbol_suggestions)
    @app_commands.guild_only()
    async def alert(interaction: discord.Interaction, symbol: Symbol, price: app_commands.Range[float, 0.0, 1e12]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        r = await resolve(interaction, symbol)
        if not r:
            return
        q = await bot.engine.quote(r.symbol)
        if not q:
            await interaction.followup.send("No live price for that right now.", ephemeral=True)
            return
        mine = [a for _, a in bot.state.items("price_alerts") if a["channel"] == interaction.channel_id]
        if len(mine) >= 50:
            await interaction.followup.send("This channel already has 50 alerts; remove some with `/alerts`.",
                                            ephemeral=True)
            return
        above = price > q.price
        key = uuid.uuid4().hex[:8]
        bot.state.set("price_alerts", key, {"channel": interaction.channel_id, "user": interaction.user.id,
                                            "symbol": r.symbol, "target": price, "above": above, "at": time.time()})
        bot._last.pop("live", None)
        await interaction.followup.send(f"🔔 I'll ping you here when **{short(r.symbol)}** goes "
                                        f"{'above' if above else 'below'} **{fmt_price(price)}** "
                                        f"(now {fmt_price(q.price)}).", ephemeral=True)

    async def alert_suggestions(interaction: discord.Interaction, current: str):
        rows = [(k, a) for k, a in bot.state.items("price_alerts") if a["channel"] == interaction.channel_id]
        return [app_commands.Choice(name=f"{short(a['symbol'])} {'above' if a['above'] else 'below'} {fmt_price(a['target'])}",
                                    value=k) for k, a in rows if current.lower() in a["symbol"].lower()][:25]

    @tree.command(name="alerts", description="List this channel's price alerts, or remove one")
    @app_commands.describe(remove="An alert to remove")
    @app_commands.autocomplete(remove=alert_suggestions)
    @app_commands.guild_only()
    async def alerts(interaction: discord.Interaction, remove: str | None = None):
        if remove:
            a = bot.state.get("price_alerts", remove)
            if not a or a["channel"] != interaction.channel_id:
                await interaction.response.send_message("No such alert here.", ephemeral=True)
                return
            if a["user"] != interaction.user.id and not interaction.permissions.manage_messages:
                await interaction.response.send_message("Only its owner (or a moderator) can remove that alert.",
                                                        ephemeral=True)
                return
            bot.state.delete("price_alerts", remove)
            await interaction.response.send_message("Removed.", ephemeral=True)
            return
        rows = [a for _, a in bot.state.items("price_alerts") if a["channel"] == interaction.channel_id]
        if not rows:
            await interaction.response.send_message("No price alerts here. Add one with `/alert`.", ephemeral=True)
            return
        lines = [f"🔔 **{short(a['symbol'])}** {'above' if a['above'] else 'below'} {fmt_price(a['target'])} · <@{a['user']}>"
                 for a in rows]
        await interaction.response.send_message(clip("\n".join(lines), MESSAGE), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ----- bookkeeping -----

    @tree.command(name="record", description="How the bot's forecasts, breakout calls and news calls have done")
    async def record(interaction: discord.Interaction):
        await interaction.response.send_message(embed=E.record_embed(bot.predictions.summary(), bot.impacts.summary(),
                                                                     bot.engine.models))

    @tree.command(name="brief", description="Post a brief here now")
    @app_commands.choices(kind=[app_commands.Choice(name=n, value=v) for n, v in (
        ("Pre-market brief", "premarket"), ("Closing recap", "close"), ("Crypto daily", "crypto"),
        ("Research digest", "research"), ("Week ahead", "weekly"), ("Morning headlines", "morning"),
        ("Trends: today", "trends-1D"), ("Trends: this week", "trends-1W"), ("Trends: this month", "trends-1M"),
        ("Trends: crypto", "trends-crypto"), ("NVIDIA brief", "nvidia"))])
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def brief(interaction: discord.Interaction, kind: app_commands.Choice[str]):
        await interaction.response.defer(thinking=True)
        cfg = bot.channels.get(interaction.channel_id)
        stocks = cfg.symbols() if cfg and cfg.market == STOCKS else bot._watchlist(STOCKS)
        coins = cfg.symbols() if cfg and cfg.market == CRYPTO else bot._watchlist(CRYPTO)
        build = {"premarket": lambda: briefs.premarket(bot, stocks), "close": lambda: briefs.close_recap(bot, stocks),
                 "crypto": lambda: briefs.crypto_daily(bot, coins),
                 "research": lambda: briefs.research_digest(bot, stocks, coins), "weekly": lambda: briefs.weekly(bot),
                 "morning": lambda: briefs.morning_news(bot),
                 "trends-1D": lambda: briefs.trends_recap(bot, "1D"), "trends-1W": lambda: briefs.trends_recap(bot, "1W"),
                 "trends-1M": lambda: briefs.trends_recap(bot, "1M"), "trends-crypto": lambda: briefs.crypto_trends(bot),
                 "nvidia": lambda: briefs.nvidia_brief(bot, "premarket")}[kind.value]
        posts = await build()
        if not posts:
            await interaction.followup.send("Nothing to post yet (the news desk fills up after a few minutes).")
            return
        first, rest = posts[0], posts[1:]
        await interaction.followup.send(embeds=first.embeds, files=[_png_file(n, d) for n, d in first.files])
        for post in rest:
            await bot.send(interaction.channel_id, post)

    @tree.command(name="status", description="Data sources, jobs, models and the news reader")
    async def status(interaction: discord.Interaction):
        e = discord.Embed(title="🩺 Market bot status", color=E.BLUE,
                          description=f"Version {bot.version} · up since {E.ts(bot.started)} · "
                                      f"{len(bot.channels.all())} channels")
        lines = []
        for name, h in sorted(bot.health.items()):
            ok = f"ok {E.ts(h.last_ok)}" if h.last_ok else "not yet"
            err = f" · ⚠️ {h.last_error} {E.ts(h.error_at)}" if h.last_error and (h.error_at or 0) > (h.last_ok or 0) else ""
            lines.append(f"`{name}` {ok}{err}")
        e.add_field(name="Jobs", value="\n".join(lines) or "Starting…", inline=False)
        data = bot.engine.data
        src = [f"Connection: {data.http.transport}"]
        for name, h in sorted(data.health.items()):
            src.append(f"**{name}** {h.line()}")
        cg = getattr(bot.engine, "sources", None)
        if cg is not None and hasattr(cg, "coingecko_line"):
            src.append(f"**CoinGecko** {cg.coingecko_line()}")
        d = bot.engine.directory
        src.append(f"Symbol list: {d.count(STOCKS):,} US stocks & ETFs, {d.count(CRYPTO):,} coins"
                   + (f" · updated {E.ts(d.updated)}" if d.updated else ""))
        e.add_field(name="Data sources", value=clip("\n".join(src), 1024), inline=False)
        extra = []
        for feature in getattr(bot, "features", []):
            try:
                extra += feature.status()
            except Exception:
                log.warning("Status of %s failed", feature.name, exc_info=True)
        if extra:
            e.add_field(name="Add-on data", value=clip("\n".join(extra), 1024), inline=False)
        spot = bot.spotlight
        if bot.massive:
            lines = [f"{spot.plan()} · {bot.massive_used()}/5 calls in the last minute · {bot.massive.limiter.total:,} "
                     "calls since start"]
            if bot.massive.last_error:
                lines.append(f"⚠️ {bot.massive.last_error}")
            blocked = sorted(spot.not_in_plan)
            if blocked:
                lines.append("Not in the plan: " + ", ".join(blocked))
            lines += [f"⚠️ {k}: {v}" for k, v in list(spot.errors.items())[:4]]
            if not bot.channels.of_kind("nvidia"):
                lines.append("Idle until an NVIDIA channel exists (`/setup` or `/channel`)")
            e.add_field(name="Massive (NVIDIA)", value=clip("\n".join(lines), 1024), inline=False)
        else:
            e.add_field(name="Massive (NVIDIA)", value="No key found: set MASSIVE_API_KEY", inline=False)
        models = []
        for market, m in bot.engine.models.items():
            models.append(f"{market}: trained {E.ts(m.trained_at)} on {m.rows:,} days ({len(m.symbols)} histories since {m.first_year})")
        if bot.engine.training:
            models.append("⏳ training now…")
        if bot.engine.training_error:
            models.append(f"⚠️ {bot.engine.training_error}")
        e.add_field(name="Models", value="\n".join(models) or "Not trained yet", inline=False)
        readers = [f"{r.name}{' (free plan)' if r.free else ''} · {r.model} · {r.calls_today} call{'' if r.calls_today == 1 else 's'} today"
                   + (f" · ⏸️ {r.resting}" if r.resting else "")
                   + (f" · ⚠️ {r.last_error}" if r.last_error else "") for r in bot.ai.statuses()]
        readers += [f"⚠️ {p}" for p in bot.ai.problems]
        e.add_field(name="News reader", value="\n".join(readers) or (
            "Keyword model. For an AI read of the news, set GROQ_API_KEY or GEMINI_API_KEY (free plans) or "
            "ANTHROPIC_API_KEY (Claude)"), inline=False)
        if bot.news.failures:
            e.add_field(name="Feeds failing", value="\n".join(clip(u, 80) for u in list(bot.news.failures)[:6]),
                        inline=False)
        await interaction.response.send_message(embed=fit_embed(e), ephemeral=True)

    @tree.command(name="update", description="Check GitHub for a new version of the market bot now")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def update(interaction: discord.Interaction):
        from .bot import on_railway, trigger_update
        if on_railway():
            await interaction.response.send_message(
                f"Running {bot.version} on Railway, which redeploys the bot by itself whenever the code on GitHub "
                "changes. Nothing to do here.", ephemeral=True)
            return
        ok, detail = await trigger_update()
        msg = (f"🔄 Checking GitHub now (running {bot.version}); I'll restart in about a minute if there's a new version."
               if ok else f"I can't start an update from here (`{detail}`). The server checks every 5 minutes anyway.")
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="help", description="Every command, tap one to use it (post: true shares it in the channel)")
    @app_commands.describe(post="Post the guide for everyone here (to pin it) instead of just for you")
    async def help_cmd(interaction: discord.Interaction, post: bool = False):
        ids = await command_ids(bot)
        e = discord.Embed(title="📊 MarketBot · commands", color=E.BLUE, description=(
            "Live stocks and crypto, alerts, breakout radar, forecasts built on a century of prices, and a news desk "
            "that estimates each story's market impact. Tap a command to use it. Any symbol works by ticker or "
            "name: `nvidia`, `brk.b`, `BTC`, `gold`, `s&p`."))
        groups: dict[str, list[tuple[str, str]]] = {}
        for feature in getattr(bot, "features", []):
            if feature.help_group:
                groups.setdefault(feature.help_group, []).extend(feature.help())
        for title, rows in list(HELP_GUIDE) + [(t, tuple(r)) for t, r in groups.items()]:
            if rows:
                e.add_field(name=title, value=clip("\n".join(f"{mention(ids, name)} {what}" for name, what in rows),
                                                   1024), inline=False)
        e.add_field(name="About the predictions", value=(
            "Breakout odds and price ranges are the strong suit: tested on years the model never saw, breakout calls "
            f"score an AUC around 0.8. Plain up/down direction is close to a coin flip for every method, so those "
            f"numbers stay near history's base rates. Everything is graded in {mention(ids, 'record')}."), inline=False)
        e.set_footer(text=E.DISCLAIMER)
        await interaction.response.send_message(embed=fit_embed(e), ephemeral=not post)

    @tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        name = interaction.command.qualified_name if interaction.command else "?"
        cause = getattr(error, "original", error)
        if isinstance(cause, (YahooError, HttpError, SourcesDown, ApiError)):
            log.warning("/%s: data unavailable: %s", name, cause)
            msg = (f"⚠️ I couldn't get the data for that right now ({clip(str(cause), 160)}). I keep retrying by "
                   "myself: try again in a minute, and `/status` shows each source's state.")
        else:
            log.error("/%s failed", name, exc_info=error)
            msg = "Something went wrong running that command. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    for feature in getattr(bot, "features", []):
        try:
            feature.register(tree)
        except Exception:
            log.exception("The commands of %s failed to register", feature.name)
