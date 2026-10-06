"""The Discord client: live boards, alerts, the news desk, scheduled briefs and grading, all on one loop."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import tasks

from . import briefs, embeds as E
from .ai import NewsAI
from .briefs import Post
from .channels import NEWS_LEVELS, ChannelStore
from .engine import Engine, Macro, ScanHit
from .feeds import NewsFetcher
from .hours import NEW_YORK, is_trading_day, market_open
from .news import TARGETS, Analysis, ImpactBook, analyse
from .record import PredictionBook
from .setups import DEFS
from .storage import StateStore
from .universe import CRYPTO, FUTURES, INDICES, MACRO, STOCKS, market_of, short
from .yahoo import Quote

log = logging.getLogger("marketbot")

TICK_SECONDS = 15
DEFAULT_LIVE_SECONDS = 60
MIN_LIVE_SECONDS = 30
NEWS_SECONDS = 180
NEWS_MAX_AGE = 3 * 3600  # unseen stories older than this aren't posted (e.g. after downtime)
NEWS_PER_CYCLE = 6  # per channel; more than this go into one digest post
RECENT_NEWS_HOURS = 72
SCAN_STOCKS_SECONDS = 600
SCAN_CRYPTO_SECONDS = 1800
SETUP_ALERTS_PER_SCAN = 4
SETUP_REPEAT_DAYS = {"breakout": 3, "pre-breakout": 5, "trend": 10, "momentum": 2}
ALERT_KINDS = ("breakout", "pre-breakout", "trend", "momentum")
# Daily move alert lines (percent). Indices move less, so their lines are closer together.
INDEX_STEPS = (1.0, 2.0, 3.0, 4.0, 5.0, 7.0)
STOCK_STEPS = (3.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0)
CRYPTO_STEPS = (5.0, 10.0, 15.0, 20.0, 30.0, 50.0)
VIX_STEPS = (15.0, 25.0, 40.0)
NO_MOVE_ALERTS = {"^TNX", "^IRX", "DX-Y.NYB", "ES=F", "NQ=F", "YM=F"}
FAST_MOVE = {"BTC-USD": 2.0, "ETH-USD": 3.0}  # crypto: % in an hour (others: 4%)
FAST_DEFAULT = 4.0


@dataclass
class JobHealth:
    last_ok: float | None = None
    last_error: str | None = None
    error_at: float | None = None
    runs: int = 0


def code_version() -> str:
    repo = str(Path(__file__).resolve().parent.parent)
    try:
        out = subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", repo, "log", "-1", "--format=%h (%cd)",
                              "--date=short"], capture_output=True, text=True, timeout=5)
        if out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # Railway builds without the .git folder but says which commit it deployed.
    return (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or "unknown"


def session_day(q: Quote | None) -> str:
    """The trading session a quote belongs to (New York date of its last trade)."""
    t = q.time if q and q.time else time.time()
    return datetime.fromtimestamp(t, NEW_YORK).strftime("%Y-%m-%d")


def move_steps(symbol: str, market: str) -> tuple[float, ...]:
    if symbol == "^VIX":
        return VIX_STEPS
    if market == CRYPTO:
        return CRYPTO_STEPS
    if symbol.startswith("^") or symbol.endswith("=F"):
        return INDEX_STEPS
    return STOCK_STEPS


def crossed(change: float, steps: tuple[float, ...]) -> float:
    """The highest alert line a move has crossed (0 for none)."""
    return max((s for s in steps if abs(change) >= s), default=0.0)


class MarketBot(discord.Client):
    def __init__(self, data_dir: str | Path, dev_guild: int | None = None, live_seconds: float = DEFAULT_LIVE_SECONDS,
                 engine: Engine | None = None, ai: NewsAI | None = None):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.channels = ChannelStore(self.data_dir / "channels.json")
        self.state = StateStore(self.data_dir / "state.json")
        self.records = StateStore(self.data_dir / "record.json")
        self.engine = engine or Engine(self.data_dir)
        self.news = NewsFetcher(self.engine.yahoo)
        self.ai = ai or NewsAI()
        self.impacts = ImpactBook(self.records)
        self.predictions = PredictionBook(self.records)
        self.quotes: dict[str, Quote] = {}
        self.quotes_at = 0.0
        self.recent_news: list[Analysis] = []
        self.vol_ratio: dict[str, float] = {}
        self.macro_cache: Macro | None = None
        self.crypto_global = None
        self.coins: dict[str, object] = {}
        self.fng: float | None = None
        self.last_scan: dict[str, list[ScanHit]] = {}
        self.trail: dict[str, deque] = {}
        self.health: dict[str, JobHealth] = {}
        self.dev_guild = dev_guild
        self.live_seconds = live_seconds
        self.version = code_version()
        self.started = time.time()
        self._jobs: dict[str, asyncio.Task] = {}
        self._last: dict[str, float] = {}
        self._first_news = not self.state.items("news_seen")

    # ----- lifecycle -----

    async def setup_hook(self) -> None:
        from .commands import register_commands
        register_commands(self)
        self.stop_on_sigterm()
        self.tick.start()
        try:
            if self.dev_guild:
                guild = discord.Object(id=self.dev_guild)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException:
            log.exception("Couldn't register the slash commands with Discord")

    def stop_on_sigterm(self) -> None:
        """Hosts (Railway, systemd) stop the bot with SIGTERM: close the Discord connection cleanly then, so
        the next copy takes over without the old one lingering."""
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._sigterm)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # not supported here (e.g. Windows, or not the main thread)

    def _sigterm(self) -> None:
        log.info("Asked to stop (SIGTERM); closing")
        self._closing = asyncio.get_running_loop().create_task(self.close())

    async def close(self) -> None:
        self.tick.cancel()
        for task in self._jobs.values():
            task.cancel()
        await self.news.close()
        await self.engine.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def on_guild_channel_delete(self, channel) -> None:
        self.channels.remove(channel.id)

    # ----- the scheduler -----

    def schedule(self) -> list[tuple[str, float, object]]:
        return [
            ("train", 6 * 3600, self.job_train),
            ("live", self.live_seconds, self.job_live),
            ("crypto_data", 300, self.job_crypto_data),
            ("mood", 900, self.job_mood),
            ("news", NEWS_SECONDS, self.job_news),
            ("scan_stocks", SCAN_STOCKS_SECONDS, self.job_scan_stocks),
            ("scan_crypto", SCAN_CRYPTO_SECONDS, self.job_scan_crypto),
            ("briefs", 60, self.job_briefs),
            ("volatility", 3600, self.job_volatility),
            ("grade", 3600, self.job_grade),
            ("prune", 3600, self.job_prune),
        ]

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self) -> None:
        now = time.monotonic()
        for name, every, job in self.schedule():
            running = self._jobs.get(name)
            if running and not running.done():
                continue
            if now - self._last.get(name, -1e12) < every:
                continue
            self._last[name] = now
            self._jobs[name] = asyncio.get_running_loop().create_task(self._run(name, job))

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.wait_until_ready()

    @tick.error
    async def _tick_error(self, error: BaseException) -> None:
        log.error("Scheduler crashed; restarting it", exc_info=error)
        asyncio.get_running_loop().call_later(5, lambda: self.tick.is_running() or self.is_closed() or self.tick.start())

    async def _run(self, name: str, job) -> None:
        health = self.health.setdefault(name, JobHealth())
        health.runs += 1
        try:
            await job()
            health.last_ok = time.time()
        except Exception as exc:
            health.last_error, health.error_at = f"{type(exc).__name__}: {exc}"[:200], time.time()
            log.exception("%s failed; trying again next time", name)

    # ----- posting -----

    async def _channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def send(self, channel_id: int, post: Post):
        """Posts embeds (and attached charts); returns the message, or None if it failed."""
        try:
            channel = await self._channel(channel_id)
            files = [discord.File(io.BytesIO(data), filename=name) for name, data in post.files]
            return await channel.send(content=post.content, embeds=post.embeds, files=files)
        except discord.NotFound:
            log.warning("Channel %s is gone; forgetting it", channel_id)
            self.channels.remove(channel_id)
        except discord.Forbidden:
            log.warning("No permission to post in channel %s", channel_id)
        except discord.HTTPException:
            log.exception("Posting to channel %s failed", channel_id)
        return None

    async def send_all(self, channel_id: int, posts: list[Post]) -> None:
        for post in posts:
            await self.send(channel_id, post)

    # ----- live prices, boards and alerts -----

    def live_symbols(self) -> list[str]:
        syms: list[str] = []
        for _, cfg in self.channels.of_kind(STOCKS):
            syms += [a.symbol for a in INDICES + FUTURES + MACRO] + cfg.symbols()
        for _, cfg in self.channels.of_kind(CRYPTO):
            syms += cfg.symbols()
        syms += [a["symbol"] for _, a in self.state.items("price_alerts")]
        return list(dict.fromkeys(syms))

    async def job_live(self) -> None:
        symbols = self.live_symbols()
        if not symbols:
            return
        quotes = await self.engine.yahoo.quotes(symbols)
        now = time.time()
        self.quotes.update(quotes)
        self.quotes_at = now
        for sym, q in quotes.items():
            if market_of(sym, q.quote_type) == CRYPTO:
                trail = self.trail.setdefault(sym, deque(maxlen=240))
                trail.append((now, q.price))
        await self.refresh_boards()
        await self.check_price_alerts()
        await self.check_moves()
        await self.check_new_highs()

    async def refresh_boards(self) -> None:
        for cid, cfg in self.channels.all():
            if cfg.kind == STOCKS:
                embed = E.stocks_board(self.quotes, INDICES, FUTURES, MACRO, cfg.symbols(),
                                       self.macro_cache.mood if self.macro_cache else None, self.quotes_at)
            elif cfg.kind == CRYPTO:
                embed = E.crypto_board(self.quotes, cfg.symbols(), self.crypto_global, self.fng, self.coins,
                                       self.quotes_at)
            else:
                continue
            await self.show_board(cid, embed)

    async def show_board(self, channel_id: int, embed: discord.Embed) -> None:
        cfg = self.channels.get(channel_id)
        if cfg is None:
            return
        try:
            channel = await self._channel(channel_id)
        except discord.NotFound:
            self.channels.remove(channel_id)
            return
        except discord.HTTPException:
            log.warning("Couldn't open channel %s for its board", channel_id)
            return
        if cfg.board_message_id:
            try:
                await channel.get_partial_message(cfg.board_message_id).edit(embed=embed)
                return
            except discord.NotFound:
                pass  # the board was deleted: post a new one
            except discord.HTTPException:
                log.warning("Couldn't update the board in %s", channel_id, exc_info=True)
                return
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("Couldn't post a board in %s", channel_id, exc_info=True)
            return
        self.channels.update(channel_id, board_message_id=message.id)
        try:
            await message.pin()
        except discord.HTTPException:
            pass  # no Manage Messages permission: the board just isn't pinned

    async def check_price_alerts(self) -> None:
        for key, a in self.state.items("price_alerts"):
            q = self.quotes.get(a["symbol"])
            if not q:
                continue
            hit = q.price >= a["target"] if a["above"] else q.price <= a["target"]
            if not hit:
                continue
            embed, mention = E.price_alert(q, a["target"], a["above"], a.get("user"))
            await self.send(a["channel"], Post([embed], content=mention))
            self.state.delete("price_alerts", key)

    async def check_moves(self) -> None:
        for cid, cfg in self.channels.all():
            if cfg.kind not in (STOCKS, CRYPTO) or not cfg.alerts:
                continue
            symbols = cfg.symbols() + ([a.symbol for a in INDICES] if cfg.kind == STOCKS else [])
            for sym in dict.fromkeys(symbols):
                q = self.quotes.get(sym)
                if not q or q.change_pct is None or sym in NO_MOVE_ALERTS:
                    continue
                if cfg.kind == STOCKS and q.market_state not in ("REGULAR", "POST", "POSTPOST", ""):
                    continue  # before the open the day's change is still yesterday's
                await self._daily_move(cid, cfg.kind, sym, q)
                if cfg.kind == CRYPTO:
                    await self._fast_move(cid, sym, q)

    async def _daily_move(self, cid: int, market: str, sym: str, q: Quote) -> None:
        steps = move_steps(sym, market)
        line = crossed(q.change_pct, steps)
        if sym == "^VIX" and q.change_pct < 0:
            return  # only fear spikes
        # Stocks: the trading session. Crypto: Yahoo's day starts at midnight UTC.
        day = session_day(q) if market == STOCKS else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{cid}|{sym}|{day}"
        before = self.state.get("moves", key, 0.0)
        signed = line if q.change_pct > 0 else -line
        if line and (abs(signed) > abs(before) or (signed > 0) != (before > 0)):
            self.state.set("moves", key, signed)
            window = "today" if market == STOCKS else "since midnight UTC"
            await self.send(cid, Post([E.move_alert(q, line, window, q.change_pct, market)]))

    async def _fast_move(self, cid: int, sym: str, q: Quote) -> None:
        trail = self.trail.get(sym)
        if not trail or len(trail) < 3:
            return
        now = trail[-1][0]
        old = [p for t, p in trail if 50 * 60 <= now - t <= 75 * 60]
        if not old:
            return
        change = (q.price / old[0] - 1) * 100
        limit = FAST_MOVE.get(sym, FAST_DEFAULT)
        key = f"{cid}|{sym}"
        if abs(change) >= limit and now - self.state.get("fast_moves", key, 0) > 2 * 3600:
            self.state.set("fast_moves", key, now)
            await self.send(cid, Post([E.move_alert(q, limit, "in the last hour", change, CRYPTO)]))

    async def check_new_highs(self) -> None:
        for cid, cfg in self.channels.of_kind(STOCKS):
            if not cfg.alerts:
                continue
            highs, lows = [], []
            for sym in cfg.symbols() + ["^GSPC", "^IXIC", "^DJI"]:
                q = self.quotes.get(sym)
                if not q or not q.day_high or q.market_state not in ("REGULAR", "POST", "POSTPOST"):
                    continue
                key = f"{cid}|{sym}|{session_day(q)}"
                if q.high52 and q.day_high >= q.high52 * 0.9995 and not self.state.get("highs", key):
                    self.state.set("highs", key, 1)
                    highs.append(sym)
                elif q.low52 and q.day_low and q.day_low <= q.low52 * 1.0005 and not self.state.get("highs", key):
                    self.state.set("highs", key, -1)
                    lows.append(sym)
            if highs or lows:
                lines = []
                if highs:
                    lines.append("🏔️ **New 52-week highs:** " + ", ".join(
                        f"{short(s)} {E.money(self.quotes[s].price, s)}" for s in highs))
                if lows:
                    lines.append("🕳️ **New 52-week lows:** " + ", ".join(
                        f"{short(s)} {E.money(self.quotes[s].price, s)}" for s in lows))
                embed = discord.Embed(description="\n".join(lines), color=E.GREEN if highs else E.RED)
                await self.send(cid, Post([embed]))

    # ----- context data -----

    async def job_crypto_data(self) -> None:
        if not self.channels.of_kind(CRYPTO):
            return
        try:
            self.crypto_global = await self.engine.sources.crypto_global()
            self.coins = {c.symbol: c for c in await self.engine.sources.top_coins(100)}
        except Exception:
            log.warning("CoinGecko unavailable", exc_info=True)
        try:
            _, values = await self.engine.sources.fear_greed()
            self.fng = float(values[-1])
        except Exception:
            log.warning("Fear & Greed unavailable", exc_info=True)

    async def job_mood(self) -> None:
        if self.channels.all():
            self.macro_cache = await self.engine.macro()

    async def job_volatility(self) -> None:
        """Each news-impact market's volatility vs normal, to scale impact estimates."""
        from . import indicators as ind
        for symbol, _, _ in TARGETS.values():
            try:
                bars = await self.engine.cache.daily(symbol, fresh=6 * 3600)
                v20 = ind.realized_vol(bars.close, 20)[-1]
                v250 = ind.realized_vol(bars.close, 250)[-1]
                if v250 > 0:
                    self.vol_ratio[symbol] = float(v20 / v250)
            except Exception:
                log.debug("No volatility for %s", symbol, exc_info=True)

    async def job_train(self) -> None:
        self.engine.start_training()

    # ----- news desk -----

    async def job_news(self) -> None:
        news_channels = self.channels.of_kind("news")
        fetched = await self.news.latest()
        now = time.time()
        watched = {s for _, cfg in self.channels.all() for s in cfg.symbols()}
        fresh = []
        with self.state.batch():
            for h in fetched:
                if self.state.get("news_seen", h.id):
                    continue
                self.state.set("news_seen", h.id, now)
                fresh.append(analyse(h, self.vol_ratio, self.impacts.calibration, watched))
        if self.ai.enabled:
            await self.ai.review([a for a in fresh if a.importance >= 40 and now - a.headline.published < NEWS_MAX_AGE])
        self.recent_news = [a for a in self.recent_news + fresh
                            if now - a.headline.published < RECENT_NEWS_HOURS * 3600][-600:]
        if self._first_news:
            # First run on a new install: don't flood the channel with the day's backlog.
            self._first_news = False
            fresh = sorted(fresh, key=lambda a: -a.importance)[:3]
        postable = sorted((a for a in fresh if now - a.headline.published < NEWS_MAX_AGE and not a.opinion),
                          key=lambda a: -a.importance)
        if not postable:
            return
        recorded = False
        for cid, cfg in news_channels:
            level = NEWS_LEVELS.get(cfg.news_level, 55)
            mine = [a for a in postable if a.importance >= level and a.market in cfg.news_markets]
            for a in mine[:NEWS_PER_CYCLE]:
                await self.send(cid, Post([E.news_embed(a)]))
            if len(mine) > NEWS_PER_CYCLE:
                await self.send(cid, Post([E.news_digest(mine[NEWS_PER_CYCLE:], "📰 More headlines")]))
            recorded = recorded or bool(mine)
        # The biggest stories also go to the market they move.
        for a in postable:
            if a.importance < 80 or not a.impacts:
                continue
            kind = CRYPTO if a.market == CRYPTO else STOCKS
            for cid, cfg in self.channels.of_kind(kind):
                if cfg.alerts:
                    await self.send(cid, Post([E.news_embed(a)], content="📰 **Market-moving news**"))
        # Remember the calls on posted stories, to grade them tomorrow.
        posted = [a for a in postable if a.importance >= 55 and a.impacts]
        if posted:
            symbols = list({i.symbol for a in posted for i in a.impacts})
            prices = {s: q.price for s, q in (await self.engine.yahoo.quotes(symbols)).items()}
            with self.records.batch():
                for a in posted:
                    self.impacts.record(a, prices)

    # ----- breakout radar -----

    async def job_scan_stocks(self) -> None:
        channels = self.channels.of_kind(STOCKS)
        ny = datetime.now(NEW_YORK)
        after_close = is_trading_day(ny.date()) and 16 <= ny.hour < 17 and ny.minute < 30
        if not channels or not (market_open() or after_close):
            return
        await self._scan(STOCKS, channels)

    async def job_scan_crypto(self) -> None:
        channels = self.channels.of_kind(CRYPTO)
        if channels:
            await self._scan(CRYPTO, channels)

    async def _scan(self, market: str, channels) -> None:
        symbols = list(dict.fromkeys(s for _, cfg in channels for s in cfg.symbols()))
        if market == STOCKS:
            symbols += ["^GSPC", "^IXIC", "^RUT"]
        quotes = {s: q for s, q in self.quotes.items() if s in symbols}
        if len(quotes) < len(symbols) // 2:
            quotes = await self.engine.yahoo.quotes(symbols)
        hits = await self.engine.scan(symbols, quotes, lookback=1)
        self.last_scan[market] = hits
        now = time.time()
        for cid, cfg in channels:
            if not cfg.alerts:
                continue
            mine = set(cfg.symbols()) | ({"^GSPC", "^IXIC", "^RUT"} if market == STOCKS else set())
            fresh: list[tuple[ScanHit, object]] = []
            for h in hits:
                if h.symbol not in mine:
                    continue
                keys = {s.key for s in h.setups}
                for s in h.setups:
                    if s.key == "high_52w" and "ath" in keys:
                        continue  # a record high is a 52-week high too: one post is enough
                    if s.days_ago != 0 or s.kind not in ALERT_KINDS:
                        continue
                    key = f"{cid}|{h.symbol}|{s.key}"
                    last = self.state.get("setup_alerts", key, 0)
                    if now - last < SETUP_REPEAT_DAYS.get(s.kind, 3) * 86400:
                        continue
                    self.state.set("setup_alerts", key, now)
                    fresh.append((h, s))
            label = "On this chart"
            for h, s in fresh[:SETUP_ALERTS_PER_SCAN]:
                await self.send(cid, Post([E.breakout_alert(h, s, label)]))
                self._record_call(h, s)
            if len(fresh) > SETUP_ALERTS_PER_SCAN:
                rest = list({id(h): h for h, _ in fresh[SETUP_ALERTS_PER_SCAN:]}.values())
                await self.send(cid, Post([E.scan_embed(rest, market, "⚡ More setups just now")]))

    def _record_call(self, h: ScanHit, s) -> None:
        d = DEFS[s.key]
        if not d.direction:
            return
        if s.kind == "pre-breakout" and s.plan.trigger:
            level = s.plan.trigger
        elif s.kind == "breakout" and s.plan.target:
            level = s.plan.target
        else:
            return
        prob = h.breakout_up if d.direction > 0 else h.breakout_down
        base = h.base_up if d.direction > 0 else h.base_down
        self.predictions.add_breakout(h.symbol, h.market, d.direction, level, h.price,
                                      prob if s.kind == "pre-breakout" else None, base, s.key)

    # ----- scheduled briefs -----

    def _zone(self, name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return NEW_YORK

    def _due(self, cid: int, kind: str, local: datetime, hour: int, minute: int, window_min: int = 120) -> bool:
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not start <= local < start + timedelta(minutes=window_min):
            return False
        key = f"{cid}|{kind}|{local:%Y-%m-%d}"
        if self.state.get("briefs", key):
            return False
        self.state.set("briefs", key, time.time())
        return True

    async def job_briefs(self) -> None:
        ny = datetime.now(NEW_YORK)
        trading = is_trading_day(ny.date())
        for cid, cfg in self.channels.all():
            if not cfg.briefs:
                continue
            try:
                if cfg.kind == STOCKS and trading:
                    if self._due(cid, "premarket", ny, 9, 0, 75):
                        await self.send_all(cid, await briefs.premarket(self, cfg.symbols()))
                    elif self._due(cid, "close", ny, 16, 10, 110):
                        await self.send_all(cid, await briefs.close_recap(self, cfg.symbols()))
                elif cfg.kind == CRYPTO:
                    local = datetime.now(self._zone(cfg.timezone))
                    if self._due(cid, "crypto", local, cfg.brief_hour, 0):
                        await self.send_all(cid, await briefs.crypto_daily(self, cfg.symbols()))
                elif cfg.kind == "research":
                    if ny.weekday() == 6 and self._due(cid, "weekly", ny, 18, 0, 240):
                        await self.send_all(cid, await briefs.weekly(self))
                    elif trading and self._due(cid, "digest", ny, 17, 30, 150):
                        stocks = self._watchlist(STOCKS)
                        coins = self._watchlist(CRYPTO)
                        await self.send_all(cid, await briefs.research_digest(self, stocks, coins))
                elif cfg.kind == "news" and trading and self._due(cid, "morning", ny, 7, 30, 90):
                    await self.send_all(cid, await briefs.morning_news(self))
            except Exception:
                log.exception("Brief for channel %s failed", cid)

    def _watchlist(self, market: str) -> list[str]:
        from .channels import ChannelConfig
        lists = [cfg.symbols() for _, cfg in self.channels.of_kind(market)]
        merged = list(dict.fromkeys(s for lst in lists for s in lst))
        return merged or ChannelConfig(market).symbols()

    # ----- grading and housekeeping -----

    async def job_grade(self) -> None:
        due = self.impacts.symbols_due()
        if due:
            quotes = await self.engine.yahoo.quotes(list(due))
            self.impacts.grade({s: q.price for s, q in quotes.items()})
        pending = self.predictions.pending_symbols()
        if pending:
            found = await asyncio.gather(*(self.engine.cache.daily(s) for s in pending), return_exceptions=True)
            histories = {s: b for s, b in zip(pending, found) if not isinstance(b, BaseException)}
            self.predictions.grade(histories)

    async def job_prune(self) -> None:
        with self.state.batch():
            self._prune(time.time())

    def _prune(self, now: float) -> None:
        for key, at in self.state.items("news_seen"):
            if now - at > 4 * 86400:
                self.state.delete("news_seen", key)
        today = datetime.now(NEW_YORK).date()
        for section in ("moves", "highs"):
            for key, _ in self.state.items(section):
                day = key.rsplit("|", 1)[-1]
                try:
                    if (today - datetime.strptime(day, "%Y-%m-%d").date()).days > 3:
                        self.state.delete(section, key)
                except ValueError:
                    self.state.delete(section, key)
        for section, keep in (("setup_alerts", 30), ("fast_moves", 2), ("briefs", 10)):
            for key, at in self.state.items(section):
                if now - at > keep * 86400:
                    self.state.delete(section, key)


UPDATE_COMMAND = ("sudo", "-n", "systemctl", "start", "--no-block", "marketbot-update.service")


async def trigger_update() -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*UPDATE_COMMAND, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (OSError, asyncio.TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
    return proc.returncode == 0, out.decode(errors="replace").strip()[:300]


def live_seconds(setting: str | None) -> float:
    setting = (setting or "").strip()
    return max(float(setting), MIN_LIVE_SECONDS) if setting else DEFAULT_LIVE_SECONDS


def on_railway(env=os.environ) -> bool:
    return any(k in env for k in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_NAME"))


def data_folder(env) -> str:
    """Where the bot keeps its data: MARKET_DATA_DIR; else the Railway volume (Railway sets
    RAILWAY_VOLUME_MOUNT_PATH when one is attached); else the folder of an older DATA_FILE setting (a server
    or Railway service set up for ScoreBot, the sports bot this repository used to hold: that folder is the
    writable one); else market-data."""
    if env.get("MARKET_DATA_DIR"):
        return env["MARKET_DATA_DIR"]
    if env.get("RAILWAY_VOLUME_MOUNT_PATH"):
        return env["RAILWAY_VOLUME_MOUNT_PATH"]
    if env.get("DATA_FILE"):
        return str(Path(env["DATA_FILE"]).parent)
    return "market-data"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("MARKET_DISCORD_TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set MARKET_DISCORD_TOKEN (or DISCORD_TOKEN) to your bot's token (see .env.example).")
    data_dir = data_folder(os.environ)
    dev_guild = os.environ.get("DEV_GUILD_ID")
    bot = MarketBot(data_dir, int(dev_guild) if dev_guild else None, live_seconds(os.environ.get("LIVE_INTERVAL")))
    bot.run(token, log_handler=None)
