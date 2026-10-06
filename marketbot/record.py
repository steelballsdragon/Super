"""Every forecast and breakout call the bot posts on its own is saved and graded when its time is up, so
/record shows how often it was right (and whether its percentages mean what they say)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np

from .universe import CRYPTO
from .yahoo import Bars

DAY = 86400
# Calendar days to wait for N trading days (stocks trade 5 days a week; crypto every day).
WAIT = {5: 7, 20: 28, 60: 87}
KEEP = 365 * DAY  # graded calls are kept a year


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


class PredictionBook:
    def __init__(self, store):
        self.store = store  # a StateStore

    # ----- recording -----

    def add_forecast(self, symbol: str, market: str, price: float, up: dict[int, float], base: dict[int, float],
                     label: str, now: float | None = None) -> None:
        now = now or time.time()
        for h, p in up.items():
            if h not in (5, 20):
                continue
            key = f"{symbol}|{_today(now)}|{h}"
            if self.store.get("forecasts", key):
                continue
            days = h if market == CRYPTO else WAIT[h]
            self.store.set("forecasts", key, {"symbol": symbol, "market": market, "price": price, "p": p,
                                              "base": base.get(h, 0.5), "h": h, "label": label, "at": now,
                                              "due": now + days * DAY, "result": None})

    def add_breakout(self, symbol: str, market: str, direction: int, level: float, price: float,
                     prob: float | None, base: float | None, setup: str, now: float | None = None) -> bool:
        """A breakout watch: will price close beyond `level` within 10 trading days? False if already open."""
        now = now or time.time()
        key = f"{symbol}|{direction:+d}"
        open_ = self.store.get("breakouts", key)
        if open_ and open_.get("result") is None:
            return False
        days = 10 if market == CRYPTO else 14
        self.store.set("breakouts", f"{key}|{int(now)}", {
            "symbol": symbol, "market": market, "dir": direction, "level": level, "price": price, "p": prob,
            "base": base, "setup": setup, "at": now, "due": now + days * DAY, "result": None})
        self.store.set("breakouts", key, {"result": None, "at": now})
        return True

    # ----- grading -----

    def pending_symbols(self) -> set[str]:
        out = {v["symbol"] for _, v in self.store.items("forecasts") if v.get("result") is None}
        out |= {v["symbol"] for k, v in self.store.items("breakouts") if "symbol" in v and v.get("result") is None}
        return out

    def grade(self, histories: dict[str, Bars], now: float | None = None) -> int:
        now = now or time.time()
        graded = 0
        with self.store.batch():
            for key, f in self.store.items("forecasts"):
                if f.get("result") is not None:
                    if now - f["due"] > KEEP:
                        self.store.delete("forecasts", key)
                    continue
                bars = histories.get(f["symbol"])
                if bars is None or now < f["due"]:
                    continue
                i = int(np.searchsorted(bars.t, f["due"] - DAY / 2))
                if i >= len(bars):
                    if now - f["due"] > 10 * DAY:
                        self.store.delete("forecasts", key)  # no price ever came: drop it
                    continue
                ret = float(bars.close[i] / f["price"] - 1)
                self.store.set("forecasts", key, {**f, "result": ret, "graded_at": now})
                graded += 1
            for key, b in self.store.items("breakouts"):
                if "symbol" not in b:
                    continue  # the "open call" marker
                if b.get("result") is not None:
                    if now - b["due"] > KEEP:
                        self.store.delete("breakouts", key)
                    continue
                bars = histories.get(b["symbol"])
                if bars is None:
                    continue
                window = (bars.t > b["at"]) & (bars.t <= b["due"])
                closes = bars.close[window]
                hit = bool((closes > b["level"]).any() if b["dir"] > 0 else (closes < b["level"]).any())
                if hit or now >= b["due"]:
                    self.store.set("breakouts", key, {**b, "result": hit, "graded_at": now})
                    self.store.set("breakouts", f"{b['symbol']}|{b['dir']:+d}", {"result": hit, "at": now})
                    graded += 1
        return graded

    # ----- the scorecard -----

    def summary(self) -> dict:
        done = [f for _, f in self.store.items("forecasts") if f.get("result") is not None]
        out = {"forecasts": {}, "breakouts": {}, "open_forecasts": 0, "open_breakouts": 0}
        out["open_forecasts"] = sum(1 for _, f in self.store.items("forecasts") if f.get("result") is None)
        for h in (5, 20):
            rows = [f for f in done if f["h"] == h]
            if not rows:
                continue
            y = np.array([1.0 if f["result"] > 0 else 0.0 for f in rows])
            p = np.array([f["p"] for f in rows])
            base = np.array([f["base"] for f in rows])
            called_up = p >= 0.5
            out["forecasts"][h] = {
                "n": len(rows), "hit": float((called_up == (y == 1)).mean()), "up_rate": float(y.mean()),
                "brier": float(np.mean((p - y) ** 2)), "base_brier": float(np.mean((base - y) ** 2)),
                "avg_p": float(p.mean()),
            }
        calls = [b for k, b in self.store.items("breakouts") if "symbol" in b]
        out["open_breakouts"] = sum(1 for b in calls if b.get("result") is None)
        graded = [b for b in calls if b.get("result") is not None]
        if graded:
            hits = np.array([1.0 if b["result"] else 0.0 for b in graded])
            probs = np.array([b["p"] for b in graded if b.get("p") is not None])
            out["breakouts"] = {"n": len(graded), "hit": float(hits.mean()),
                                "avg_p": float(probs.mean()) if len(probs) else None,
                                "up": int(sum(1 for b in graded if b["dir"] > 0)),
                                "down": int(sum(1 for b in graded if b["dir"] < 0))}
        return out
