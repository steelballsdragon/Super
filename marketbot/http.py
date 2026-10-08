"""Every web request the market data sources make goes through here.

Yahoo Finance (and Nasdaq) turn away requests whose TLS handshake doesn't look like a browser's, which is what a
Python HTTP client sends from a cloud host such as Railway: there Yahoo answered nothing at all. curl_cffi makes the
handshake look like Chrome's (the yfinance library does the same). Without curl_cffi it falls back to aiohttp.

Each source's health (last success, last failure and its HTTP status) is kept for /status, and a source that keeps
failing is rested for a short while so the bot moves on to a backup instead of waiting on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

log = logging.getLogger(__name__)

TIMEOUT = 30.0
RETRIES = 2  # extra tries after a network error or a 5xx (a 429 isn't retried: hammering keeps a block going)
FAILS_TO_REST = 3  # failures in a row before a source is rested
REST_SECONDS = (30.0, 60.0, 120.0, 300.0)  # longer each time it fails again right after a rest
# These mean the source is down, blocking us or rate limiting us. Anything else (a 404 for an unknown symbol, a 400
# for a bad request) means it answered.
FAILING = {401, 403, 407, 429}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
              "Safari/537.36")


class HttpError(Exception):
    def __init__(self, source: str, message: str, status: int | None = None):
        super().__init__(message)
        self.source = source
        self.status = status


class _Resting(Exception):
    pass


@dataclass
class Response:
    status: int
    text: str
    url: str = ""
    content: bytes = field(default=b"", compare=False)  # the raw body (for binary files such as ZIPs and PDFs)
    headers: dict | None = field(default=None, compare=False)  # lower-cased names

    def json(self):
        if not self.text.strip():
            return None
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise HttpError("", f"not JSON (HTTP {self.status}): {self.text[:80]!r}", self.status) from exc


@dataclass
class Health:
    """How a source has been doing."""
    ok: int = 0
    failed: int = 0
    streak: int = 0  # failures in a row
    rests: int = 0  # rests in a row (each one longer)
    last_ok: float | None = None
    last_error: str | None = None
    error_at: float | None = None
    status: int | None = None  # the last HTTP status it sent
    rest_until: float = 0.0  # monotonic time

    def resting(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) < self.rest_until

    @property
    def failing(self) -> bool:
        """Its latest request failed."""
        return self.streak > 0

    def line(self) -> str:
        def when(t):
            return f"<t:{int(t)}:R>" if t else "never"

        if self.streak:
            what = self.last_error or "failing"
            return f"⚠️ failing ({what}) since {when(self.error_at)} · last worked {when(self.last_ok)}"
        if self.last_ok:
            return f"✅ ok {when(self.last_ok)}" + (f" · {self.failed} failed calls" if self.failed else "")
        return "not used yet" if not self.failed else f"⚠️ {self.last_error}"


def _quiet(text: str, secrets=()) -> str:
    """Error text without anything that looks like a key (some APIs echo the request URL) or any of `secrets`."""
    text = re.sub(r"(?i)(api[_-]?key|token|crumb|key)=[^&\s]+", r"\1=…", text).replace("\n", " ")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "…")
    return text


def ca_bundle():
    """The certificates to trust: a custom bundle when the host sets one, else curl_cffi's own."""
    return os.environ.get("CURL_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True


class _Curl:
    name = "curl_cffi (Chrome)"

    def __init__(self):
        import importlib
        importlib.import_module("curl_cffi.requests")  # fail now (ImportError) if it isn't installed
        self._session = None

    async def get(self, url: str, headers: dict, timeout: float, proxy: str | None = None) -> Response:
        if self._session is None:
            from curl_cffi.requests import AsyncSession
            # No User-Agent of our own: curl_cffi sends the one that matches its Chrome fingerprint.
            self._session = AsyncSession(impersonate="chrome", timeout=timeout, verify=ca_bundle(), max_clients=16)
        extra = {"proxy": proxy} if proxy else {}
        r = await self._session.get(url, headers=headers, timeout=timeout, allow_redirects=True, **extra)
        content = getattr(r, "content", None) or b""
        binary = content[:4] in (b"PK\x03\x04", b"%PDF")
        return Response(r.status_code, "" if binary else (r.text or ""), str(r.url), content,
                        {k.lower(): v for k, v in (getattr(r, "headers", None) or {}).items()})

    async def close(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()


class _Aiohttp:
    name = "aiohttp"

    def __init__(self):
        self._session = None

    async def get(self, url: str, headers: dict, timeout: float, proxy: str | None = None) -> Response:
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": BROWSER_UA}, cookie_jar=aiohttp.CookieJar())
        async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout),
                                     proxy=proxy) as resp:
            if hasattr(resp, "read"):
                content = await resp.read()
                binary = content[:4] in (b"PK\x03\x04", b"%PDF")
                text = "" if binary else content.decode(getattr(resp, "charset", None) or "utf-8", errors="replace")
            else:
                content, text = b"", await resp.text(errors="replace")
            headers = getattr(resp, "headers", None) or {}
            return Response(resp.status, text, str(resp.url), content, {k.lower(): v for k, v in headers.items()})

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def default_backend():
    try:
        return _Curl()
    except ImportError:
        log.warning("curl_cffi isn't installed; using aiohttp (Yahoo may refuse it on cloud hosts)")
        return _Aiohttp()


class Http:
    def __init__(self, backend=None, retries: int = RETRIES, limits: dict[str, int] | None = None,
                 proxies: dict[str, str] | None = None, sleep=asyncio.sleep):
        self.backend = backend or default_backend()
        self.retries = retries
        self.health: dict[str, Health] = {}
        self._limits = dict(limits or {})
        self.proxies = {k: v for k, v in (proxies or {}).items() if v}  # source -> proxy URL
        self._gates: dict[str, asyncio.Semaphore] = {}
        self._sleep = sleep

    @property
    def transport(self) -> str:
        return getattr(self.backend, "name", type(self.backend).__name__)

    def source(self, name: str) -> Health:
        return self.health.setdefault(name, Health())

    def resting(self, name: str) -> bool:
        h = self.health.get(name)
        return bool(h and h.resting())

    async def close(self) -> None:
        await self.backend.close()

    async def get(self, url: str, *, params=None, headers: dict | None = None, source: str = "web",
                  timeout: float = TIMEOUT, retries: int | None = None, force: bool = False,
                  answered: tuple[int, ...] = ()) -> Response:
        """The response (any status). Raises HttpError when the source can't be reached, sends a 429, keeps
        sending 5xx, or is resting after failing repeatedly (unless force=True). Statuses in `answered` count as
        the source working (e.g. Massive's 403 for data outside the plan)."""
        h = self.source(source)
        if h.resting() and not force:
            raise HttpError(source, f"{source} is resting after {h.streak} failures ({h.last_error})", h.status)
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        tries = 1 + (self.retries if retries is None else retries)
        gate = self._gates.setdefault(source, asyncio.Semaphore(self._limits.get(source, 6)))
        error: HttpError | None = None
        proxy = self.proxies.get(source) or self.proxies.get(source.split()[0])  # "Yahoo cookie" uses Yahoo's
        # Credentials sent in headers never appear in error text (some APIs echo them back).
        secrets = [v.split()[-1] for k, v in (headers or {}).items() if k.lower() == "authorization" and v.split()]
        for attempt in range(tries):
            try:
                async with gate:
                    # Re-checked here: the source may have been rested while this request queued or slept.
                    if h.resting() and not force:
                        raise _Resting()
                    resp = await self.backend.get(url, dict(headers or {}), timeout, proxy)
            except _Resting:
                raise HttpError(source, f"{source} is resting after {h.streak} failures ({h.last_error})", h.status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, timeouts, TLS problems
                error = HttpError(source, f"{type(exc).__name__}: {_quiet(str(exc), secrets)[:160]}")
            else:
                if resp.status in answered or (resp.status not in FAILING and resp.status < 500):
                    self.record_ok(source, resp.status)
                    return resp
                body = _quiet(resp.text.strip(), secrets)[:60]
                error = HttpError(source, f"HTTP {resp.status}" + (f" ({body})" if body and "<" not in body else ""),
                                  resp.status)
                if resp.status in FAILING:
                    # The caller decides (Yahoo refreshes its crumb on a 401); a retry now wouldn't help.
                    self.record_failure(source, error)
                    if resp.status == 429:
                        raise error
                    return resp
            if attempt < tries - 1:
                await self._sleep(1.5 * 2 ** attempt)
        self.record_failure(source, error)
        raise error

    def record_ok(self, source: str, status: int | None = None) -> None:
        h = self.source(source)
        h.ok += 1
        h.streak = h.rests = 0
        h.rest_until = 0.0
        h.last_ok = time.time()
        h.status = status

    def record_failure(self, source: str, error: HttpError) -> None:
        h = self.source(source)
        h.failed += 1
        h.streak += 1
        h.last_error = str(error)[:120]
        h.error_at = time.time()
        h.status = error.status
        if h.streak >= FAILS_TO_REST and not h.resting():
            h.rest_until = time.monotonic() + REST_SECONDS[min(h.rests, len(REST_SECONDS) - 1)]
            h.rests += 1
            log.warning("%s failed %d times in a row (%s); resting it", source, h.streak, h.last_error)
