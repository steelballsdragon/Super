import pytest

np = pytest.importorskip("numpy")

from marketbot import forecast, indicators as ind, setups, stats  # noqa: E402
from marketbot.cache import merge  # noqa: E402
from marketbot.features import FEATURES, TARGETS, build, targets  # noqa: E402
from marketbot.model import Logit, auc, dataset, fit_logit, score, train, walk_forward  # noqa: E402
from marketbot.yahoo import parse_chart  # noqa: E402
from tests.market_helpers import from_closes, walk  # noqa: E402


# ----- indicators -----

def test_moving_averages_and_rolling_windows():
    x = np.arange(1, 11, dtype=float)
    assert np.isnan(ind.sma(x, 3)[1]) and ind.sma(x, 3)[2] == 2.0 and ind.sma(x, 3)[-1] == 9.0
    assert ind.rolling_max(x, 4)[-1] == 10 and ind.rolling_min(x, 4)[-1] == 7
    e = ind.ema(x, 3)
    assert np.isnan(e[1]) and e[2] == 2.0 and e[3] == pytest.approx(3.0)
    assert ind.shift(x, 1)[1] == 1 and np.isnan(ind.shift(x, 1)[0]) and ind.shift(x, -1)[0] == 2


def test_rsi_extremes():
    assert ind.rsi(np.arange(1, 40, dtype=float))[-1] == 100.0
    assert ind.rsi(np.arange(40, 1, -1, dtype=float))[-1] == pytest.approx(0.0)
    assert ind.rsi(np.full(40, 5.0))[-1] == 50.0


def test_indicators_never_look_ahead():
    b = walk(600)
    full = ind.rsi(b.close)
    cut = ind.rsi(b.close[:400])
    np.testing.assert_allclose(full[:400], cut, equal_nan=True)
    fs_full = build(b)
    fs_cut = build(b.slice(0, 400))
    np.testing.assert_allclose(fs_full.X[:400], fs_cut.X, equal_nan=True)


def test_percentile_rank_and_drawdown():
    x = np.array([1, 2, 3, 4, 5, 1], dtype=float)
    r = ind.percentile_rank(x, 5)
    assert r[4] == 1.0 and r[5] == 0.0
    assert ind.drawdown(np.array([10, 12, 6, 12.0]))[2] == pytest.approx(-0.5)


def test_swing_points_find_peaks_and_troughs():
    c = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3, 4, 5, 6, 7], dtype=float)
    highs, lows = ind.swing_points(c, c, 3)
    assert 4 in highs and 8 in lows


# ----- features and targets -----

def test_features_are_finite_after_warmup():
    fs = build(walk(1200))
    assert fs.X.shape[1] == len(FEATURES)
    assert fs.valid[:260].sum() == 0
    assert fs.valid[300:].mean() > 0.95


def test_targets_describe_the_future():
    b = from_closes([100] * 30 + [101, 102, 99, 120] + [120] * 30)
    tg = targets(b)
    i = 29  # the day before the rally
    assert tg["up_5d"][i] == 1.0
    assert tg["breakout_up"][i] == 1.0
    assert np.isnan(tg["up_5d"][-1])  # unknown future


def test_history_without_volume_still_builds():
    fs = build(walk(800, volume=False))
    assert fs.valid.sum() > 400


# ----- the model -----

def test_logistic_regression_learns_a_simple_rule():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 3))
    y = (X[:, 0] + 0.2 * rng.normal(size=4000) > 0).astype(float)
    m = fit_logit(X, y)
    p = m.predict(X)
    assert auc(y, p) > 0.95
    assert m.coef[0] > abs(m.coef[1]) * 5
    m2 = Logit.from_json(m.to_json())
    np.testing.assert_allclose(m2.predict(X[:5]), p[:5])


def test_auc_and_skill():
    y = np.array([0, 0, 1, 1])
    assert auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    s = score(np.array([1, 0] * 50, dtype=float), np.full(100, 0.5))
    assert s.skill == pytest.approx(0.0) and s.grade == "weak"


def test_walk_forward_only_trains_on_the_past():
    histories = [walk(5000, seed=s, symbol=f"S{s}") for s in range(3)]
    data = dataset(histories, stride=2)
    skill, p, rows = walk_forward(data, "breakout_up", test_years=4, block=2)
    assert skill is not None and len(p) == len(rows)
    assert data.year[rows].min() >= data.year.max() - 5
    # Breakouts depend on where price sits in its range, which the features capture even in random walks.
    assert skill.auc > 0.7


def test_direction_on_a_random_walk_has_no_skill():
    data = dataset([walk(5000, seed=s, symbol=f"S{s}") for s in range(3)], stride=2)
    skill, _, _ = walk_forward(data, "up_20d", test_years=4, block=2)
    assert skill.auc < 0.58


def test_train_saves_and_loads():
    from marketbot.model import MarketModel
    m = train("stocks", [walk(3000, seed=s, symbol=f"S{s}") for s in range(2)])
    assert set(m.models) == set(TARGETS)
    again = MarketModel.from_json(m.to_json())
    x = build(walk(400, seed=9)).X[-1]
    assert again.predict(x) == pytest.approx(m.predict(x))
    assert MarketModel.from_json({**m.to_json(), "features": ["old"]}) is None


# ----- analogs, Monte Carlo, levels -----

def test_analogs_never_use_the_unknown_future():
    b = walk(3000)
    fs = build(b)
    a = forecast.find_analogs(fs, [(b, fs)], k=10)
    assert len(a.matches) == 10
    assert all(m.t < b.t[-61] for m in a.matches)
    days = sorted(m.t for m in a.matches)
    assert min(np.diff(days)) >= forecast.MIN_GAP * 86400
    assert 0 <= a.up_chance(20, 0.5) <= 1


def test_monte_carlo_cone_is_ordered_and_scales_with_volatility():
    calm = forecast.monte_carlo(walk(2000, vol=0.005), "stocks")
    wild = forecast.monte_carlo(walk(2000, vol=0.03), "stocks")
    for cone in (calm, wild):
        h = cone.horizons[21]
        assert h["p5"] < h["p25"] < h["p50"] < h["p75"] < h["p95"]
        assert cone.bands.shape == (5, 252)
    spread = lambda c: np.log(c.horizons[21]["p95"] / c.horizons[21]["p5"])
    assert spread(wild) > spread(calm) * 3


def test_levels_sit_on_the_right_side_of_price():
    b = walk(600, seed=3)
    res, sup = forecast.levels(b)
    price = b.close[-1]
    assert all(l.price > price for l in res) and all(l.price < price for l in sup)


# ----- setups -----

def test_breakout_detected_with_its_plan():
    closes = list(100 + np.sin(np.arange(150) / 3)) + [104]
    b = from_closes(closes)
    b.volume[-1] = 5e6  # loud breakout
    active = setups.active(b, lookback=1)
    keys = {a.key for a in active}
    assert "breakout_20d" in keys
    plan = next(a.plan for a in active if a.key == "breakout_20d")
    assert plan.trigger < 104 and plan.target > 104 and plan.stop < plan.trigger


def test_golden_cross_and_history_stats():
    closes = list(np.linspace(200, 100, 260)) + list(np.linspace(100, 190, 200))
    b = from_closes(closes)
    ctx = setups.context(b)
    sig = setups.detect(ctx)
    assert sig["golden_cross"].sum() == 1
    s = setups.stats(setups.context(walk(4000)), setups.detect(setups.context(walk(4000)))["breakout_20d"])
    assert s is None or 0 <= s.up_rate <= 1


def test_every_setup_is_defined():
    sig = setups.detect(setups.context(walk(800)))
    assert set(sig) == set(setups.DEFS)


# ----- stats -----

def test_drawdowns_and_performance():
    b = from_closes([100, 120, 60, 130, 65, 140])
    dds = stats.drawdowns(b, 0.2)
    assert dds[0].depth == pytest.approx(-0.5) and dds[0].recovered is not None
    p = stats.performance(walk(2000))
    assert p.returns["1D"] is not None and p.max_dd.depth < 0 and 0 < p.up_days < 1


def test_presidential_years():
    assert [stats.presidential_year(y) for y in (2025, 2026, 2027, 2028)] == [1, 2, 3, 4]


def test_seasonality_and_decades():
    b = walk(252 * 30, step=86400 * 365 // 252)
    assert len(stats.seasonality(b)) == 12
    assert stats.decades(b)


def test_cape_view_from_shiller_style_data():
    from marketbot.sources import LongRun
    n = 1500
    years = 1871 + np.arange(n) // 12
    months = np.arange(n) % 12 + 1
    rng = np.random.default_rng(2)
    price = 5 * np.exp(np.cumsum(rng.normal(0.005, 0.04, n)))
    earnings = price / rng.uniform(10, 30, n)
    cape = rng.uniform(8, 40, n)
    nan = np.full(n, np.nan)
    lr = LongRun(years, months, price, nan, earnings, np.full(n, 100.0), nan, price, earnings, cape)
    v = stats.cape_view(lr, float(price[-1]), float(years[-1]) + 0.5)
    assert v is not None and v.band[0] <= v.band[1] and 0 <= v.percentile <= 1


# ----- data plumbing -----

def test_parse_chart_adjusts_and_cleans():
    result = {"meta": {"symbol": "X"}, "timestamp": [1, 2, 3, 3],
              "indicators": {"quote": [{"open": [10, None, 12, 12], "high": [11, None, 13, 13], "low": [9, None, 11, 11],
                                        "close": [10, None, 12, 12.5], "volume": [100, None, 0, 5]}],
                             "adjclose": [{"adjclose": [5, None, 6, 6.25]}]}}
    b = parse_chart(result)
    assert list(b.t) == [1, 3]  # the empty bar and the repeated one are dropped
    assert b.close[0] == 5 and b.high[0] == pytest.approx(5.5)  # adjusted by adjclose/close


def test_merge_tops_up_and_detects_readjusted_history():
    old = from_closes([1, 2, 3, 4, 5])
    new = from_closes([4, 5.5, 6], start=old.t[3])
    merged = merge(old, new)
    assert list(merged.close) == [1, 2, 3, 4, 5.5, 6]  # yesterday's mid-day close is replaced
    readjusted = from_closes([3.5, 4, 5], start=old.t[2])
    assert merge(old, readjusted) is None
