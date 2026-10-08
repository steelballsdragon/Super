"""Clients for the extra data services (Finnhub, FRED, FMP, SEC EDGAR, Etherscan and the keyless crypto APIs).

Each service goes through the bot's shared Http (Chrome-like connection, per-source health in /status) with its own
rate limits, kept a little under the free plan's. Limits measured over hours or a day are saved, so a restart
doesn't forget what the plan has already counted. A key is sent in a header where the service accepts one, and is
never shown in /status, logs or errors.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..http import Http, HttpError, _quiet
from ..massive import RateLimiter
from ..storage import read_json, write_json

log = logging.getLogger(__name__)

SAVE_WINDOW = 3600.0  # limits over windows this long or longer are saved across restarts


def env_key(*names: str, env=None) -> str:
    """The first of these environment variables that's set (stripped, quotes removed), or ""."""
    env = os.environ if env is None else env
    for name in names:
        value = (env.get(name) or "").strip().strip("\"'").strip()
        if value:
            return value
    return ""


class ApiError(Exception):
    """kind: "key" (missing or rejected), "plan" (not in the free plan), "budget" (our own limit is used up),
    "rate" (the service said slow down), "missing" (no such thing), "network" or "bad" (an unreadable answer)."""

    def __init__(self, source: str, message: str, kind: str, status: int | None = None):
        super().__init__(f"{source}: {message}")
        self.source, self.kind, self.status = source, kind, status


class Api:
    """One data service. `limits` are (calls, seconds) pairs, all of which must have room before a request."""

    def __init__(self, name: str, http: Http, base: str, key: str = "", *, needs_key: bool = True,
                 key_param: str | None = None, key_header: str | None = None, limits=((60, 60.0),),
                 headers: dict | None = None, state_file: Path | None = None, clock=time.monotonic, sleep=None,
                 wall=time.time):
        self.name = name
        self.http = http
        self.base = base.rstrip("/")
        self.key = key
        self.needs_key = needs_key
        self.key_param = key_param
        self.key_header = key_header
        self.headers = dict(headers or {})
        self.state_file = state_file
        kw = {"clock": clock, "wall": wall}
        if sleep is not None:
            kw["sleep"] = sleep
        self.limiters = [RateLimiter(calls, window, **kw) for calls, window in limits]
        self.key_rejected = False
        self.last_error: str | None = None
        self.calls = 0
        self._load()

    @property
    def enabled(self) -> bool:
        return (bool(self.key) or not self.needs_key) and not self.key_rejected

    def status_line(self) -> str:
        if self.needs_key and not self.key:
            return "no key"
        if self.key_rejected:
            return "⚠️ key rejected"
        parts = [f"{self.calls} calls since start"]
        for lim in self.limiters:
            if lim.window >= SAVE_WINDOW:
                parts.append(f"{lim.used()}/{lim.calls} in {_span(lim.window)}")
        return " · ".join(parts) + (f" · ⚠️ {self.last_error}" if self.last_error else "")

    def scrub(self, text) -> str:
        text = _quiet(str(text or ""))
        return text.replace(self.key, "…") if self.key else text

    def wait_time(self) -> float:
        return max((lim.wait_time() for lim in self.limiters), default=0.0)

    async def get(self, path: str, params: dict | None = None, wait: float = 0.0, timeout: float = 20.0,
                  answered: tuple[int, ...] = (), raw: bool = False):
        """The parsed JSON (or the text, with raw=True). Waits up to `wait` seconds for room under the limits;
        raises ApiError."""
        if not self.enabled:
            raise ApiError(self.name, "no usable key" if not self.key_rejected else "key rejected", "key")
        slots = []
        for lim in self.limiters:
            slot = lim.try_slot() if wait <= 0 else await lim.acquire_slot(wait)
            if slot is None:
                for taken, used in zip(self.limiters, slots):
                    taken._sent.remove(used)  # give back what this request won't use
                    taken.total -= 1
                raise ApiError(self.name, "our own rate limit is used up for now", "budget")
            slots.append(slot)
        params = dict(params or {})
        headers = dict(self.headers)
        if self.key and self.key_header:
            headers[self.key_header] = self.key
        elif self.key and self.key_param:
            params[self.key_param] = self.key
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        self.calls += 1
        try:
            resp = await self.http.get(url, params=params, headers=headers, source=self.name, timeout=timeout,
                                       retries=1, answered=(400, 401, 402, 403, 404) + tuple(answered))
        except HttpError as exc:
            if exc.status == 429:
                for lim in self.limiters:
                    lim.penalize(min(lim.window, 120.0))
                self.last_error = "rate limited (backing off)"
                raise ApiError(self.name, "rate limited", "rate", 429) from None
            self.last_error = self.scrub(exc)[:120]
            raise ApiError(self.name, self.scrub(exc)[:160], "network", exc.status) from None
        finally:
            for lim, slot in zip(self.limiters, slots):
                lim.finish(slot)
            self._save()
        text = resp.text or ""
        if resp.status in (401, 403) and self._looks_like_key_problem(resp.status, text):
            self.key_rejected = True
            self.last_error = f"key rejected (HTTP {resp.status})"
            raise ApiError(self.name, self.last_error, "key", resp.status)
        if resp.status in (402, 403):
            raise ApiError(self.name, f"not in the free plan ({self.scrub(text)[:80]})", "plan", resp.status)
        if resp.status == 404:
            raise ApiError(self.name, "not found", "missing", 404)
        if resp.status >= 400 and resp.status not in answered:
            raise ApiError(self.name, f"HTTP {resp.status} ({self.scrub(text)[:80]})", "bad", resp.status)
        self.last_error = None
        if raw:
            return text
        try:
            return resp.json()
        except HttpError:
            raise ApiError(self.name, f"unreadable answer ({self.scrub(text)[:60]})", "bad", resp.status) from None

    def _looks_like_key_problem(self, status: int, text: str) -> bool:
        """401 always means the key; a 403 might instead mean "not in your plan"."""
        if status == 401:
            return True
        low = text.lower()
        return any(w in low for w in ("invalid api key", "invalid key", "api key is invalid", "api_key",
                                      "apikey", "not authorized", "unauthorized", "invalid token"))

    # ----- keeping long windows across restarts -----

    def _load(self) -> None:
        if not self.state_file:
            return
        saved = read_json(self.state_file, {}).get(self.name, {})
        for lim in self.limiters:
            if lim.window >= SAVE_WINDOW:
                lim.restore(saved.get(str(int(lim.window)), []))

    def _save(self) -> None:
        if not self.state_file or not any(lim.window >= SAVE_WINDOW for lim in self.limiters):
            return
        try:
            data = read_json(self.state_file, {})
            data[self.name] = {str(int(lim.window)): lim.recent() for lim in self.limiters
                               if lim.window >= SAVE_WINDOW}
            write_json(self.state_file, data)
        except OSError:
            log.warning("Couldn't save %s's call counts", self.name, exc_info=True)


def _span(seconds: float) -> str:
    if seconds >= 86400:
        return "24h" if seconds == 86400 else f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds:.0f}s"
