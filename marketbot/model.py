"""Probability models: L2-regularised logistic regression, trained on decades of pooled history and judged
only on years it never saw (walk-forward), so the accuracy it reports is the accuracy to expect."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np

from .features import FEATURES, TARGETS, build, targets
from .yahoo import Bars

log = logging.getLogger(__name__)

STRIDE = 3  # every third day: neighbouring days' outcomes overlap almost entirely anyway
MAX_ROWS = 150_000
TEST_YEARS = 12  # walk-forward over the last dozen years
BLOCK_YEARS = 2


@dataclass
class Logit:
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float

    def _z(self, X):
        return np.nan_to_num((X - self.mean) / self.scale)

    def predict(self, X: np.ndarray) -> np.ndarray:
        z = self._z(np.atleast_2d(X)) @ self.coef + self.intercept
        return 1 / (1 + np.exp(-np.clip(z, -30, 30)))

    def contributions(self, x: np.ndarray) -> np.ndarray:
        """How much each feature pushes today's log-odds up or down."""
        return self._z(np.atleast_2d(x))[0] * self.coef

    def to_json(self) -> dict:
        return {"mean": self.mean.tolist(), "scale": self.scale.tolist(), "coef": self.coef.tolist(),
                "intercept": self.intercept}

    @classmethod
    def from_json(cls, d: dict) -> "Logit":
        return cls(np.array(d["mean"]), np.array(d["scale"]), np.array(d["coef"]), float(d["intercept"]))


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 2.0, iters: int = 30) -> Logit:
    """Newton-Raphson on standardised inputs; the penalty keeps weights small (it's a noisy problem)."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale < 1e-9] = 1.0
    Z = (X - mean) / scale
    n, k = Z.shape
    A = np.hstack([Z, np.ones((n, 1))])
    w = np.zeros(k + 1)
    base = np.clip(y.mean(), 1e-4, 1 - 1e-4)
    w[-1] = np.log(base / (1 - base))
    penalty = np.full(k + 1, l2)
    penalty[-1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(A @ w, -30, 30)))
        grad = A.T @ (p - y) + penalty * w
        H = (A * (p * (1 - p))[:, None]).T @ A + np.diag(penalty + 1e-9)
        step = np.linalg.solve(H, grad)
        w -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return Logit(mean, scale, w[:-1], float(w[-1]))


def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Chance that a random 'yes' day was scored above a random 'no' day (0.5 = no skill)."""
    y = np.asarray(y).astype(bool)
    pos, neg = y.sum(), (~y).sum()
    if not pos or not neg:
        return 0.5
    _, inverse, counts = np.unique(np.asarray(p), return_inverse=True, return_counts=True)
    ranks = (np.cumsum(counts) - (counts - 1) / 2)[inverse]  # 1-based, ties share their average rank
    return float((ranks[y].sum() - pos * (pos + 1) / 2) / (pos * neg))


@dataclass
class Skill:
    n: int
    base_rate: float
    accuracy: float  # share of days the side with p >= 0.5 was right
    auc: float
    brier: float
    base_brier: float  # Brier score of always forecasting the base rate
    buckets: list = field(default_factory=list)  # [(forecast range, count, how often it happened)]

    @property
    def skill(self) -> float:
        """Brier skill score: 0 = no better than the base rate, > 0 = better."""
        return 1 - self.brier / self.base_brier if self.base_brier else 0.0

    @property
    def grade(self) -> str:
        s, a = self.skill, self.auc
        if a >= 0.6 and s >= 0.03:
            return "strong"
        if a >= 0.56 and s > 0.01:
            return "useful"
        if a >= 0.53 and s > 0:
            return "slight"
        return "weak"


def score(y: np.ndarray, p: np.ndarray, base_rate: float | None = None) -> Skill:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    base = float(y.mean()) if base_rate is None else base_rate
    edges = [0, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 1.0001]
    buckets = []
    for lo, hi in zip(edges, edges[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum() >= 30:
            buckets.append((f"{lo:.0%}–{min(hi, 1):.0%}", int(m.sum()), float(y[m].mean())))
    return Skill(len(y), float(y.mean()), float(((p >= 0.5) == (y == 1)).mean()), auc(y, p),
                 float(np.mean((p - y) ** 2)), float(np.mean((base - y) ** 2)), buckets)


@dataclass
class Dataset:
    X: np.ndarray
    Y: dict[str, np.ndarray]
    year: np.ndarray
    symbol: np.ndarray


def year_of(t: np.ndarray) -> np.ndarray:
    return 1970 + t / (365.2425 * 86400)


def dataset(histories: list[Bars], stride: int = STRIDE, max_rows: int = MAX_ROWS) -> Dataset:
    Xs, Ys, years, syms = [], {k: [] for k in TARGETS}, [], []
    for bars in histories:
        if len(bars) < 400:
            continue
        fs = build(bars)
        tg = targets(bars)
        rows = np.where(fs.valid)[0][::stride]
        if not len(rows):
            continue
        Xs.append(fs.X[rows].astype(np.float32))
        for k in TARGETS:
            Ys[k].append(tg[k][rows].astype(np.float32))
        years.append(year_of(bars.t[rows].astype(float)))
        syms.append(np.full(len(rows), bars.symbol))
    if not Xs:
        raise ValueError("no usable history to train on")
    X = np.vstack(Xs)
    Y = {k: np.concatenate(v) for k, v in Ys.items()}
    year = np.concatenate(years)
    symbol = np.concatenate(syms)
    if len(X) > max_rows:  # keep the most recent rows: today's markets resemble recent decades most
        keep = np.argsort(year)[-max_rows:]
        keep.sort()
        X, year, symbol = X[keep], year[keep], symbol[keep]
        Y = {k: v[keep] for k, v in Y.items()}
    return Dataset(X, Y, year, symbol)


def walk_forward(data: Dataset, target: str, test_years: int = TEST_YEARS, block: int = BLOCK_YEARS,
                 gap_years: float = 0.3) -> tuple[Skill | None, np.ndarray, np.ndarray]:
    """Trains on everything before each block of years and tests on the block. Returns the out-of-sample
    skill, predictions and the rows they belong to."""
    y_all = data.Y[target]
    known = np.isfinite(y_all)
    last = float(np.floor(data.year[known].max())) if known.any() else 0
    first_test = last - test_years + 1
    preds, rows = [], []
    for start in np.arange(first_test, last + 1, block):
        train = known & (data.year < start - gap_years)
        test = known & (data.year >= start) & (data.year < start + block)
        if train.sum() < 2000 or test.sum() < 100:
            continue
        model = fit_logit(data.X[train], y_all[train])
        preds.append(model.predict(data.X[test]))
        rows.append(np.where(test)[0])
    if not preds:
        return None, np.array([]), np.array([], dtype=int)
    p = np.concatenate(preds)
    r = np.concatenate(rows)
    train_base = float(np.nanmean(y_all[known & (data.year < first_test)])) if (data.year < first_test).any() else None
    return score(y_all[r], p, train_base), p, r


@dataclass
class MarketModel:
    market: str
    models: dict[str, Logit]
    skill: dict[str, Skill]
    base_rates: dict[str, float]
    trained_at: float
    rows: int
    symbols: list[str]
    first_year: int

    def predict(self, x: np.ndarray) -> dict[str, float]:
        return {k: float(m.predict(x)[0]) for k, m in self.models.items()}

    def drivers(self, x: np.ndarray, target: str = "up_20d", top: int = 4) -> list[tuple[str, float]]:
        """The features pushing this prediction most, with signed log-odds contributions."""
        contrib = self.models[target].contributions(x)
        order = np.argsort(-np.abs(contrib))[:top]
        return [(FEATURES[i], float(contrib[i])) for i in order if abs(contrib[i]) > 0.01]

    def to_json(self) -> dict:
        return {"market": self.market, "models": {k: m.to_json() for k, m in self.models.items()},
                "skill": {k: asdict(s) for k, s in self.skill.items()}, "base_rates": self.base_rates,
                "trained_at": self.trained_at, "rows": self.rows, "symbols": self.symbols,
                "first_year": self.first_year, "features": FEATURES}

    @classmethod
    def from_json(cls, d: dict) -> "MarketModel | None":
        if d.get("features") != FEATURES:
            return None  # saved by a version with different inputs: retrain
        skill = {k: Skill(**{**s, "buckets": [tuple(b) for b in s.get("buckets", [])]})
                 for k, s in d.get("skill", {}).items()}
        return cls(d["market"], {k: Logit.from_json(m) for k, m in d["models"].items()}, skill,
                   d.get("base_rates", {}), d["trained_at"], d.get("rows", 0), d.get("symbols", []),
                   d.get("first_year", 0))


def train(market: str, histories: list[Bars]) -> MarketModel:
    started = time.monotonic()
    data = dataset(histories)
    models, skills, base = {}, {}, {}
    for target in TARGETS:
        y = data.Y[target]
        known = np.isfinite(y)
        skill, _, _ = walk_forward(data, target)
        if skill:
            skills[target] = skill
        models[target] = fit_logit(data.X[known], y[known])
        base[target] = float(y[known].mean())
    first = int(np.floor(data.year.min()))
    log.info("Trained the %s model on %d rows (%s, from %d) in %.1fs", market, len(data.X),
             ", ".join(sorted(set(data.symbol.tolist())))[:200], first, time.monotonic() - started)
    return MarketModel(market, models, skills, base, time.time(), len(data.X), sorted(set(data.symbol.tolist())), first)


@dataclass
class Backtest:
    symbol: str
    start_year: int
    skill: Skill | None
    strategy_cagr: float
    hold_cagr: float
    strategy_dd: float
    hold_dd: float
    exposure: float  # share of days invested
    trades: int
    hit_rate: float  # invested 20-day periods that ended higher
    equity: np.ndarray = field(repr=False, default=None)
    hold: np.ndarray = field(repr=False, default=None)
    t: np.ndarray = field(repr=False, default=None)


def backtest(bars: Bars, extra: list[Bars], test_years: int = 10) -> Backtest | None:
    """Walk-forward test of a simple rule on one symbol: hold it while the model's chance of being higher in
    20 days beats the usual rate (the share of up 20-day spells in the years it trained on), else stay in
    cash. Each year is predicted by a model trained only on earlier years (of this symbol and the reference
    histories)."""
    fs = build(bars)
    tg = targets(bars)
    years = year_of(bars.t.astype(float))
    pool = dataset(extra, stride=STRIDE) if extra else None
    rows_all = np.where(fs.valid)[0]
    if len(rows_all) < 500:
        return None
    last = int(np.floor(years[-1]))
    start_year = max(last - test_years + 1, int(np.floor(years[rows_all[0]])) + 3)
    p = np.full(len(bars), np.nan)
    bar = np.full(len(bars), np.nan)  # the hurdle each prediction had to clear
    for year in range(start_year, last + 1):
        own = rows_all[(years[rows_all] < year - 0.1) & np.isfinite(tg["up_20d"][rows_all])][::STRIDE]
        X = fs.X[own]
        y = tg["up_20d"][own]
        if pool is not None:
            m = (pool.year < year - 0.1) & np.isfinite(pool.Y["up_20d"]) & (pool.symbol != bars.symbol)
            X = np.vstack([X, pool.X[m]])
            y = np.concatenate([y, pool.Y["up_20d"][m]])
        if len(y) < 1500:
            continue
        model = fit_logit(X, y)
        test = rows_all[(years[rows_all] >= year) & (years[rows_all] < year + 1)]
        if len(test):
            p[test] = model.predict(fs.X[test])
            bar[test] = float(y.mean())
    tested = np.where(np.isfinite(p))[0]
    if len(tested) < 100:
        return None
    first = tested[0]
    daily = np.diff(np.log(bars.close))[first:]  # return from day i to i+1
    signal = np.where(np.isfinite(p[first:-1]), p[first:-1] > bar[first:-1], False)  # decided at day i's close
    strat = np.where(signal, daily, 0.0)
    span = (bars.t[-1] - bars.t[first]) / (365.2425 * 86400)
    eq, hold = np.exp(np.cumsum(strat)), np.exp(np.cumsum(daily))

    def cagr(curve):
        return float(curve[-1] ** (1 / span) - 1) if span > 0 else 0.0

    def max_dd(curve):
        return float((curve / np.maximum.accumulate(curve) - 1).min())

    known = tested[np.isfinite(tg["up_20d"][tested])]
    sk = score(tg["up_20d"][known], p[known]) if len(known) > 50 else None
    invested = known[p[known] > bar[known]]
    hit = float(tg["up_20d"][invested].mean()) if len(invested) else 0.0
    trades = int(np.sum(np.diff(signal.astype(int)) == 1) + (1 if signal[0] else 0))
    return Backtest(bars.symbol, int(np.floor(years[first])), sk, cagr(eq), cagr(hold), max_dd(eq), max_dd(hold),
                    float(signal.mean()), trades, hit, eq, hold, bars.t[first + 1:])


def describe_age(trained_at: float) -> str:
    return datetime.fromtimestamp(trained_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
