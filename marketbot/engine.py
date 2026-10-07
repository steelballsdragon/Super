"""Puts the data and the analysis together: one call gives a symbol's full outlook, a watchlist scan, options
positioning, fundamentals, a backtest, the macro picture or the century view. Number crunching runs in worker
threads so Discord stays responsive."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import forecast, indicators as ind, setups as st, stats
from .cache import HistoryCache
from .data import MarketData
from .directory import Directory
from .feargreed import CNNFearGreed, Gauge, crypto_gauge, estimate_gauge
from .features import FEATURES, build
from .http import Http
from .model import Backtest, MarketModel, Skill, backtest, train
from .sources import LongRun, Sources, fear_greed_label
from .storage import write_json
from .universe import (BENCHMARK, CRYPTO, CRYPTO_TRAINING, STOCK_TRAINING, STOCKS, display_name, looks_like_symbol,
                       market_of, normalize, short)
from .yahoo import Bars, Quote, YahooError

log = logging.getLogger(__name__)

MODEL_MAX_AGE = 3 * 86400
KIND_LABELS = {"stock": "stock", "etf": "ETF", "index": "index", "future": "futures", "crypto": "crypto"}
DIRECTION_TARGETS = {5: "up_5d", 20: "up_20d", 60: "up_60d"}


class UnknownSymbol(Exception):
    pass


class SourcesDown(Exception):
    """A lookup failed because the data sources aren't answering (not because the symbol doesn't exist)."""


@dataclass
class Resolved:
    symbol: str
    name: str
    market: str


@dataclass
class Outlook:
    symbol: str
    name: str
    market: str
    price: float
    quote: Quote | None
    bars: Bars = field(repr=False)
    trend: tuple[str, int]
    vol_regime: tuple[str, float]
    up: dict[int, float]  # horizon days -> final chance of being higher
    base: dict[str, float]  # base rates per model target
    model_probs: dict[str, float]  # raw model output per target ({} while the model trains)
    skill: dict[str, Skill]
    analogs: forecast.AnalogSummary
    cone: forecast.Cone | None
    setups: list[st.ActiveSetup]
    resistance: list[forecast.Level]
    support: list[forecast.Level]
    drivers: list[tuple[str, float]]
    score: int  # -100 (very bearish) .. +100 (very bullish)
    label: str
    confidence: str
    rsi: float
    vs_sma50: float | None
    vs_sma200: float | None
    atr_pct: float
    hi20: float
    lo20: float
    perf: stats.Performance | None
    model_ready: bool

    @property
    def breakout_up(self) -> float | None:
        return self.model_probs.get("breakout_up")

    @property
    def breakout_down(self) -> float | None:
        return self.model_probs.get("breakout_down")


@dataclass
class ScanHit:
    symbol: str
    name: str
    market: str
    price: float
    change_pct: float | None
    setups: list[st.ActiveSetup]
    breakout_up: float | None
    breakout_down: float | None
    base_up: float
    base_down: float
    hi20: float
    lo20: float
    pressure: float  # how strongly a breakout looks set up (for ranking)


@dataclass
class OptionsView:
    expiry: int
    days: float
    spot: float
    atm_strike: float
    atm_iv: float | None
    expected_move: float | None  # straddle price
    put_call_oi: float | None
    put_call_volume: float | None
    max_pain: float | None
    call_wall: float | None
    put_wall: float | None
    realized_vol: float | None
    total_call_oi: float
    total_put_oi: float


@dataclass
class Macro:
    quotes: dict[str, Quote]
    curve: float | None  # 10-year minus 3-month yield, percentage points
    vix_pct: float | None  # VIX percentile since 1990
    mood: float | None  # our 0-100 stock market fear & greed
    mood_parts: dict[str, float]
    crypto_fng: float | None
    crypto_fng_prev_week: float | None
    fng_history: dict | None  # what BTC did after similar readings
    crypto_global: object | None
    stock_fg: Gauge | None = None  # CNN's Fear & Greed, or the mood above when CNN isn't answering
    crypto_fg: Gauge | None = None


class Engine:
    def __init__(self, data_dir: str | Path, data: MarketData | None = None, sources: Sources | None = None):
        self.data_dir = Path(data_dir)
        self.sources = sources or Sources()
        self.data = data or MarketData(directory=Directory.load(self.data_dir),
                                       coins=lambda: self.sources.top_coins(250))
        self.directory = self.data.directory
        self.cache = HistoryCache(self.data, self.data_dir / "history")
        self.models: dict[str, MarketModel] = {}
        self.model_file = self.data_dir / "models.json"
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
        self._training: asyncio.Task | None = None
        self.training_error: str | None = None
        self._resolved: dict[str, Resolved] = {}
        self._suggested: dict[str, tuple[float, list]] = {}
        http = getattr(self.data, "http", None)
        self.cnn = CNNFearGreed(http) if isinstance(http, Http) else None
        self._load_models()

    async def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        await self.data.close()
        await self.sources.close()

    async def run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    # ----- symbols -----

    async def resolve(self, text: str) -> Resolved:
        """A symbol from a ticker, alias or company/coin name ("apple", "btc", "s&p", "brk.b", "hyperliquid").
        The symbol directory answers most lookups without asking any source. Raises UnknownSymbol, or SourcesDown
        when it can't tell because the data sources aren't answering."""
        key = text.strip().lower()
        if key in self._resolved:
            return self._resolved[key]
        found = None
        listing = self.directory.lookup(text)
        if listing:
            found = Resolved(listing.symbol, display_name(listing.symbol, listing.name), listing.market)
        guess = normalize(text)
        if found is None and looks_like_symbol(guess):
            quotes = await self.data.quotes([guess])
            q = quotes.get(guess)
            if q:
                found = Resolved(guess, display_name(guess, q.name), market_of(guess, q.quote_type))
        if found is None:
            matches = self.directory.search(text, 1)
            if matches and _names_match(text, matches[0].name, matches[0].symbol):
                m = matches[0]
                found = Resolved(m.symbol, display_name(m.symbol, m.name), m.market)
        if found is None and self.data.yahoo_ok:
            try:
                results, _ = await self.data.yahoo.search(text, quotes=6)
            except YahooError:
                results = []
            for r in results:
                sym = r.get("symbol")
                if sym and r.get("quoteType") in ("EQUITY", "ETF", "INDEX", "CRYPTOCURRENCY", "FUTURE", "MUTUALFUND"):
                    found = Resolved(sym, display_name(sym, r.get("shortname") or r.get("longname")),
                                     market_of(sym, r.get("quoteType")))
                    break
        if found is None:
            outage = self.data.outage()
            if outage:
                raise SourcesDown(outage)
            raise UnknownSymbol(text)
        if len(self._resolved) > 2000:
            self._resolved.clear()
        self._resolved[key] = found
        return found

    def forget_lookups(self) -> None:
        self._resolved.clear()
        self._suggested.clear()

    async def suggest(self, text: str) -> list[tuple[str, str]]:
        """(label, symbol) suggestions while typing: the symbol directory, plus Yahoo's search for anything it
        doesn't know."""
        key = text.strip().lower()
        if not key:
            return []
        hit = self._suggested.get(key)
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        out = [(f"{short(i.symbol)} · {i.name} ({KIND_LABELS.get(i.kind, i.kind)})"[:100], i.symbol)
               for i in self.directory.search(text, 12)]
        if len(out) < 5 and self.data.yahoo_ok:
            try:
                results, _ = await self.data.yahoo.search(text, quotes=10)
            except YahooError:
                results = []
            for r in results:
                sym = r.get("symbol")
                if not sym or r.get("quoteType") not in ("EQUITY", "ETF", "INDEX", "CRYPTOCURRENCY", "FUTURE"):
                    continue
                name = r.get("shortname") or r.get("longname") or sym
                out.append((f"{sym} · {name} ({r.get('exchDisp') or r.get('typeDisp') or ''})"[:100], sym))
        if len(self._suggested) > 500:
            self._suggested.clear()
        self._suggested[key] = (time.monotonic(), out)
        return out

    # ----- models -----

    def _load_models(self) -> None:
        try:
            raw = json.loads(self.model_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("Saved models are unreadable; they'll be retrained")
            return
        for market, d in raw.items():
            m = MarketModel.from_json(d)
            if m:
                self.models[market] = m

    def _save_models(self) -> None:
        write_json(self.model_file, {k: m.to_json() for k, m in self.models.items()})

    def models_stale(self) -> bool:
        return any(m not in self.models or time.time() - self.models[m].trained_at > MODEL_MAX_AGE
                   for m in (STOCKS, CRYPTO))

    def start_training(self, force: bool = False) -> bool:
        """Retrains both models in the background (minutes); returns False if a run is already going."""
        if self._training and not self._training.done():
            return False
        if not force and not self.models_stale():
            return False
        self._training = asyncio.get_running_loop().create_task(self._train_all())
        return True

    @property
    def training(self) -> bool:
        return bool(self._training and not self._training.done())

    async def _train_all(self) -> None:
        for market, symbols in ((STOCKS, STOCK_TRAINING), (CRYPTO, CRYPTO_TRAINING)):
            try:
                found = await asyncio.gather(*(self.cache.daily(s, fresh=12 * 3600) for s in symbols),
                                             return_exceptions=True)
                histories = [b for b in found if isinstance(b, Bars) and len(b) > 400]
                self.models[market] = await self.run(train, market, histories)
                self.training_error = None
            except Exception as exc:
                self.training_error = f"{market}: {type(exc).__name__}: {exc}"[:300]
                log.exception("Training the %s model failed", market)
        try:
            self._save_models()
        except OSError:
            log.exception("Couldn't save the models")
        release_memory()

    # ----- prices -----

    async def history(self, symbol: str, live: Quote | None = None) -> Bars:
        bars = await self.cache.daily(symbol)
        return with_live(bars, live) if live else bars

    async def quote(self, symbol: str) -> Quote | None:
        try:
            return (await self.data.quotes([symbol])).get(symbol)
        except YahooError:
            return None

    # ----- outlook -----

    async def outlook(self, symbol: str, market: str | None = None, quote: Quote | None = None,
                      name: str | None = None) -> Outlook:
        market = market or market_of(symbol)
        quote = quote or await self.quote(symbol)
        bars = await self.history(symbol, quote)
        bench_sym = BENCHMARK[market]
        extra_syms = [bench_sym, "^IXIC"] if market == STOCKS else [bench_sym, "ETH-USD"]
        extra_syms = [s for s in extra_syms if s != symbol]
        refs = await asyncio.gather(*(self.cache.daily(s) for s in extra_syms), return_exceptions=True)
        refs = [b for b in refs if isinstance(b, Bars)]
        bench = next((b for b in refs if b.symbol == bench_sym), None)
        model = self.models.get(market)
        return await self.run(compute_outlook, symbol, name or display_name(symbol, quote.name if quote else None),
                              market, bars, refs, bench, model, quote)

    async def scan(self, symbols: list[str], quotes: dict[str, Quote], lookback: int = 1) -> list[ScanHit]:
        """Every symbol's fresh setups and breakout odds, strongest breakout pressure first."""
        found = await asyncio.gather(*(self.cache.daily(s) for s in symbols), return_exceptions=True)
        jobs = []
        for sym, bars in zip(symbols, found):
            if isinstance(bars, BaseException) or len(bars) < 120:
                continue
            q = quotes.get(sym)
            jobs.append((sym, with_live(bars, q) if q else bars, q))
        models = dict(self.models)
        return await self.run(scan_all, jobs, models, lookback)

    # ----- research extras -----

    async def options(self, symbol: str, bars: Bars | None = None) -> OptionsView | None:
        try:
            first = await self.data.options(symbol)
        except YahooError:
            return None
        dates = first.get("expirationDates") or []
        if not dates:
            return None
        now = time.time()
        expiry = next((d for d in dates if d - now > 6 * 86400), dates[0])
        chain = first if expiry == dates[0] else await self.data.options(symbol, expiry)
        opts = (chain.get("options") or [{}])[0]
        spot = (chain.get("quote") or {}).get("regularMarketPrice")
        if not spot:
            return None
        rv = None
        if bars is not None and len(bars) > 30:
            rv = float(ind.realized_vol(bars.close, 20)[-1] * np.sqrt(252))
        return options_view(opts.get("calls") or [], opts.get("puts") or [], float(spot), expiry, now, rv)

    async def fundamentals(self, symbol: str) -> dict:
        try:
            return await self.data.summary(symbol, ("financialData", "defaultKeyStatistics", "calendarEvents",
                                                     "recommendationTrend", "summaryProfile", "earningsHistory"))
        except YahooError:
            return {}

    async def backtest(self, symbol: str, market: str) -> Backtest | None:
        bars = await self.cache.daily(symbol)
        pool_syms = (STOCK_TRAINING[:8] if market == STOCKS else CRYPTO_TRAINING[:6])
        found = await asyncio.gather(*(self.cache.daily(s) for s in pool_syms if s != symbol),
                                     return_exceptions=True)
        pool = [b for b in found if isinstance(b, Bars)]
        return await self.run(backtest, bars, pool)

    async def macro(self) -> Macro:
        syms = ["^GSPC", "^VIX", "^TNX", "^IRX", "DX-Y.NYB", "GC=F", "CL=F", "HG=F", "BTC-USD", "ETH-USD", "^MOVE"]
        quotes_task = asyncio.ensure_future(self.data.quotes(syms))
        cnn_task = asyncio.ensure_future(self.cnn.get()) if self.cnn else None
        hist = await asyncio.gather(*(self.cache.daily(s) for s in ("^GSPC", "^VIX", "HYG", "IEF", "BTC-USD")),
                                    return_exceptions=True)
        fng = cg = None
        try:
            fng = await self.sources.fear_greed()
        except Exception:
            log.warning("Fear & Greed index unavailable", exc_info=True)
        try:
            cg = await self.sources.crypto_global()
        except Exception:
            log.warning("CoinGecko unavailable", exc_info=True)
        try:
            quotes = await quotes_task
        except YahooError:
            quotes = {}
        h = {s: b for s, b in zip(("^GSPC", "^VIX", "HYG", "IEF", "BTC-USD"), hist) if isinstance(b, Bars)}
        m = await self.run(build_macro, quotes, h, fng, cg)
        cnn = None
        if cnn_task is not None:
            try:
                cnn = await cnn_task
            except Exception:
                log.warning("CNN Fear & Greed unavailable", exc_info=True)
        m.stock_fg = cnn or estimate_gauge(m.mood, m.mood_parts, time.time())
        if fng is not None:
            m.crypto_fg = crypto_gauge(*fng)
        return m

    async def long_run(self) -> LongRun | None:
        try:
            return await self.sources.long_run()
        except Exception:
            log.warning("Shiller data unavailable", exc_info=True)
            return None


def _names_match(text: str, name: str, symbol: str) -> bool:
    """Whether a search hit plausibly is what was typed: every word typed starts a word of the name or ticker."""
    from .directory import words
    typed = words(text)
    have = words(name) + words(symbol)
    compact = "".join(have)
    return bool(typed) and (all(any(w.startswith(t) for w in have) for t in typed) or compact.startswith("".join(typed)))


def release_memory() -> None:
    """Hands memory freed after a big job (training) back to the system; Python otherwise keeps the peak."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # not Linux/glibc


# ----- the computations (run in worker threads) -----

def with_live(bars: Bars, q: Quote | None) -> Bars:
    """Daily bars with the live quote as today's bar, so signals use the latest price."""
    if q is None or not len(bars) or not q.price or not q.time:
        return bars
    last_day = int(bars.t[-1] // 86400)
    quote_day = int(q.time // 86400)
    if quote_day > last_day:
        return bars.with_last(q.price, q.time, q.day_high, q.day_low, q.volume, new_bar=True)
    if quote_day == last_day:
        return bars.with_last(q.price, q.time, q.day_high, q.day_low, q.volume)
    return bars


def _direction(model_p: float | None, base: float, skill: Skill | None) -> float:
    """The model's chance, shrunk toward the base rate in proportion to how well it did out of sample."""
    if model_p is None:
        return base
    weight = float(np.clip((skill.skill if skill else 0) / 0.02, 0, 1))
    return base + weight * (model_p - base)


def compute_outlook(symbol, name, market, bars: Bars, refs: list[Bars], bench: Bars | None,
                    model: MarketModel | None, quote: Quote | None) -> Outlook:
    if len(bars) < 60:
        raise UnknownSymbol(f"{symbol} doesn't have enough price history yet")
    fs = build(bars)
    x = fs.X[-1]
    model_ok = model is not None and np.isfinite(x).all()
    probs = model.predict(x) if model_ok else {}
    base = dict(model.base_rates) if model else {}
    skill = dict(model.skill) if model else {}
    ref_sets = [(bars, fs)] + [(b, build(b)) for b in refs if len(b) > 300]
    analogs = forecast.find_analogs(fs, ref_sets) if np.isfinite(x).all() else forecast.AnalogSummary([], {}, 0.0)
    default_base = {5: 0.55, 20: 0.58, 60: 0.62} if market == STOCKS else {5: 0.5, 20: 0.5, 60: 0.5}
    up = {}
    for h, target in DIRECTION_TARGETS.items():
        b = base.get(target, default_base[h])
        m = _direction(probs.get(target), b, skill.get(target))
        a = analogs.up_chance(h, b)
        up[h] = float((m + a) / 2)
    cone = forecast.monte_carlo(bars, market)
    active = st.active(bars, lookback=3, bench=bench)
    res, sup = forecast.levels(bars)
    trend = stats.trend_label(bars)
    vreg = stats.volatility_regime(bars)
    c = bars.close
    rsi = float(ind.rsi(c, 14)[-1])
    s50, s200 = ind.sma(c, 50)[-1], ind.sma(c, 200)[-1]
    atr = ind.atr(bars.high, bars.low, c, 14)[-1]
    hi20 = float(bars.high[-21:-1].max()) if len(bars) > 21 else float(bars.high.max())
    lo20 = float(bars.low[-21:-1].min()) if len(bars) > 21 else float(bars.low.min())
    # The composite score.
    f = dict(zip(FEATURES, x))
    parts = {
        "trend": trend[1] / 2,
        "momentum": 0.5 * np.tanh(f.get("ret_60d", 0) / 2) + 0.5 * np.tanh(f.get("ret_250d", 0) / 2),
        "direction": np.clip((up[20] - base.get("up_20d", default_base[20])) / 0.08, -1, 1),
        "breakout": 0.0,
        "setups": 0.0,
    }
    if "breakout_up" in probs:
        tilt = (probs["breakout_up"] - base["breakout_up"]) - (probs["breakout_down"] - base["breakout_down"])
        parts["breakout"] = float(np.clip(tilt / 0.3, -1, 1))
    fresh = [s for s in active if s.direction and st.DEFS[s.key].alert]
    parts["setups"] = float(np.clip(sum(s.direction for s in fresh) / 2, -1, 1))
    for k, v in parts.items():
        parts[k] = 0.0 if not np.isfinite(v) else float(v)
    weights = {"trend": 30, "momentum": 20, "direction": 25, "breakout": 15, "setups": 10}
    score = int(round(sum(weights[k] * parts[k] for k in weights)))
    label = ("Strongly bullish" if score >= 45 else "Bullish" if score >= 15 else "Neutral" if score > -15
             else "Bearish" if score > -45 else "Strongly bearish")
    signs = [np.sign(v) for v in parts.values() if abs(v) > 0.15]
    agree = abs(sum(signs)) / len(signs) if signs else 0
    confidence = "Medium" if agree >= 0.75 and abs(score) >= 30 else "Low"
    try:
        perf = stats.performance(bars)
    except Exception:
        perf = None
    return Outlook(
        symbol=symbol, name=name, market=market, price=float(c[-1]), quote=quote, bars=bars, trend=trend,
        vol_regime=vreg, up=up, base=base, model_probs=probs, skill=skill, analogs=analogs, cone=cone,
        setups=active, resistance=res, support=sup, drivers=model.drivers(x) if model_ok else [], score=score,
        label=label, confidence=confidence, rsi=rsi,
        vs_sma50=float(c[-1] / s50 - 1) if np.isfinite(s50) else None,
        vs_sma200=float(c[-1] / s200 - 1) if np.isfinite(s200) else None,
        atr_pct=float(atr / c[-1]) if np.isfinite(atr) else 0.0, hi20=hi20, lo20=lo20, perf=perf,
        model_ready=model_ok,
    )


def scan_all(jobs: list[tuple[str, Bars, Quote | None]], models: dict[str, MarketModel], lookback: int) -> list[ScanHit]:
    hits = []
    for sym, bars, q in jobs:
        try:
            market = market_of(sym, q.quote_type if q else None)
            active = [a for a in st.active(bars, lookback=lookback) if st.DEFS[a.key].alert]
            model = models.get(market)
            p_up = p_down = None
            base_up = base_down = 0.0
            if model:
                fs = build(bars)
                if np.isfinite(fs.X[-1]).all():
                    probs = model.predict(fs.X[-1])
                    p_up, p_down = probs["breakout_up"], probs["breakout_down"]
                    base_up, base_down = model.base_rates["breakout_up"], model.base_rates["breakout_down"]
            hi20 = float(bars.high[-21:-1].max())
            lo20 = float(bars.low[-21:-1].min())
            pressure = max((p_up or 0) - base_up, (p_down or 0) - base_down, 0) * 2
            pressure += sum(1.0 if a.kind == "breakout" else 0.7 if a.kind == "pre-breakout" else 0.3 for a in active)
            hits.append(ScanHit(sym, display_name(sym, q.name if q else None), market, float(bars.close[-1]),
                                q.change_pct if q else None, active, p_up, p_down, base_up, base_down, hi20, lo20,
                                pressure))
        except Exception:
            log.warning("Scanning %s failed", sym, exc_info=True)
    hits.sort(key=lambda h: -h.pressure)
    return hits


def options_view(calls: list[dict], puts: list[dict], spot: float, expiry: int, now: float,
                 realized: float | None) -> OptionsView | None:
    """Positioning in one expiry's chain. Outside market hours Yahoo blanks bids, open interest and implied
    volatility, so volatility then comes from the last straddle price and positioning from today's volume."""
    if not calls or not puts:
        return None

    def mid(o):
        bid, ask = o.get("bid") or 0, o.get("ask") or 0
        return (bid + ask) / 2 if bid and ask else o.get("lastPrice") or 0

    strikes = sorted({o["strike"] for o in calls} & {o["strike"] for o in puts})
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    c_atm = next(o for o in calls if o["strike"] == atm)
    p_atm = next(o for o in puts if o["strike"] == atm)
    straddle = mid(c_atm) + mid(p_atm)
    days = max((expiry - now) / 86400, 0.5)
    ivs = [o.get("impliedVolatility") for o in (c_atm, p_atm) if (o.get("impliedVolatility") or 0) > 0.03]
    iv = float(np.mean(ivs)) if ivs else None
    if iv is None and straddle:
        iv = straddle / (0.8 * spot * np.sqrt(days / 365))  # an at-the-money straddle ≈ 0.8·S·σ·√T
    call_oi = sum(o.get("openInterest") or 0 for o in calls)
    put_oi = sum(o.get("openInterest") or 0 for o in puts)
    call_vol = sum(o.get("volume") or 0 for o in calls)
    put_vol = sum(o.get("volume") or 0 for o in puts)
    weight = "openInterest" if call_oi + put_oi > 0 else "volume"
    c_w = np.array([[o["strike"], o.get(weight) or 0] for o in calls], dtype=float)
    p_w = np.array([[o["strike"], o.get(weight) or 0] for o in puts], dtype=float)
    pain = None
    if c_w[:, 1].sum() + p_w[:, 1].sum() > 0:
        all_strikes = np.array(sorted(set(c_w[:, 0]) | set(p_w[:, 0])))
        K = all_strikes[:, None]
        payout = (np.maximum(K - c_w[:, 0], 0) * c_w[:, 1]).sum(axis=1) + \
                 (np.maximum(p_w[:, 0] - K, 0) * p_w[:, 1]).sum(axis=1)
        pain = float(all_strikes[int(np.argmin(payout))])
    above = c_w[c_w[:, 0] >= spot]
    below = p_w[p_w[:, 0] <= spot]
    call_wall = float(above[np.argmax(above[:, 1]), 0]) if len(above) and above[:, 1].max() > 0 else None
    put_wall = float(below[np.argmax(below[:, 1]), 0]) if len(below) and below[:, 1].max() > 0 else None
    return OptionsView(expiry, days, spot, atm, iv, straddle or None, put_oi / call_oi if call_oi else None,
                       put_vol / call_vol if call_vol else None, pain, call_wall, put_wall, realized, call_oi, put_oi)


def _pct_rank(series: np.ndarray, value: float) -> float:
    s = series[np.isfinite(series)]
    return float((s < value).mean()) if len(s) else 0.5


def build_macro(quotes: dict[str, Quote], hist: dict[str, Bars], fng, cg) -> Macro:
    tnx, irx = quotes.get("^TNX"), quotes.get("^IRX")
    curve = tnx.price - irx.price if tnx and irx else None
    vix_pct = None
    if "^VIX" in hist:
        v = hist["^VIX"]
        price = quotes["^VIX"].price if "^VIX" in quotes else v.close[-1]
        vix_pct = _pct_rank(v.close, price)
    mood, parts = None, {}
    window = 504  # judge each gauge against the last two years
    if "^GSPC" in hist:
        c = hist["^GSPC"].close
        mom = c / ind.sma(c, 125) - 1
        parts["Momentum (S&P vs 125-day avg)"] = _pct_rank(mom[-window:], mom[-1])
        r20 = c / ind.shift(c, 20) - 1
        parts["Price strength (20-day return)"] = _pct_rank(r20[-window:], r20[-1])
    if "^VIX" in hist:
        v = hist["^VIX"].close
        rel = v / ind.sma(v, 50)
        parts["Volatility (VIX vs 50-day avg)"] = 1 - _pct_rank(rel[-window:], rel[-1])
    if "HYG" in hist and "IEF" in hist:
        a, b = hist["HYG"], hist["IEF"]
        common, ia, ib = np.intersect1d(stats.days(a), stats.days(b), return_indices=True)
        if len(common) > 300:
            ratio = a.close[ia] / b.close[ib]
            rr = ratio / ind.shift(ratio, 20) - 1
            parts["Junk bond demand (HYG vs IEF)"] = _pct_rank(rr[-window:], rr[-1])
    if parts:
        mood = 100 * float(np.mean(list(parts.values())))
    fng_now = fng_week = None
    fng_hist = None
    if fng is not None and len(fng[1]):
        ts, vals = fng
        fng_now = float(vals[-1])
        fng_week = float(vals[-8]) if len(vals) > 8 else None
        btc = hist.get("BTC-USD")
        if btc is not None:
            d_btc = stats.days(btc)
            d_fng = ts.astype("datetime64[s]").astype("datetime64[D]")
            common, i_f, i_b = np.intersect1d(d_fng, d_btc, return_indices=True)
            close = btc.close[i_b]
            fwd = np.full(len(common), np.nan)
            fwd[:-30] = close[30:] / close[:-30] - 1
            similar = (np.abs(vals[i_f] - fng_now) <= 5) & np.isfinite(fwd)
            if similar.sum() >= 20:
                fng_hist = {"n": int(similar.sum()), "up": float((fwd[similar] > 0).mean()),
                            "median": float(np.median(fwd[similar])), "all_up": float((fwd[np.isfinite(fwd)] > 0).mean())}
    return Macro(quotes, curve, vix_pct, mood, parts, fng_now, fng_week, fng_hist, cg)


def mood_label(value: float) -> str:
    return fear_greed_label(value)
