"""Optional: an AI model reads the important headlines as a second opinion on the keyword rules.

Turned on by a key for any of these. With more than one, the first that can answer reads each batch and the others
take over when it's rate limited, out of calls for the day or failing:
- ANTHROPIC_API_KEY: Claude (paid; the sharpest read).
- GROQ_API_KEY: Groq's free plan (console.groq.com, no card needed), running openai/gpt-oss-120b.
- GEMINI_API_KEY: Google's free Gemini plan (aistudio.google.com).

The free plans allow only so many calls and tokens a minute and a day, so their calls are paced to stay under
those limits, and a "slow down" (HTTP 429) pauses that reader for as long as the service asks. Headlines the rules
already rank as worth posting are sent in batches, most important first; the model's read (event, takeaway, which
markets move, which way and roughly how much) replaces the rules' guess for those headlines. Headlines no reader
gets to in time keep the rules' read. Without a key the bot works the same, on rules alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass

from .http import _quiet
from .news import TARGETS, Analysis, Impact

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
BATCH = 10
DAILY_CALLS = 150
REVIEW_SECONDS = 100.0  # longest a news run waits on the readers (pacing included) before posting what it has
TIMEOUT = 90.0

SYSTEM = """You are a markets news desk analyst. For each headline (with its summary), judge how it is likely to \
move US-listed stocks and crypto over the next trading day.

Rules:
- relevant=false for opinion pieces, listicles, explainers, promotional posts, recaps of moves that already \
happened, and news too small or too local to move the listed markets. Keep impacts empty when not relevant.
- importance: 0-100. 90+ only for market-wide shocks (surprise Fed decision, CPI far off consensus, exchange \
collapse). Routine company news 30-55. Stories that only describe a move that already happened: below 30.
- impacts: up to 5 of the listed assets, plus at most 2 company tickers named in the story (asset "TICKER" with \
the ticker filled in). direction is up, down, or either (a big move whose sign is unclear). move_low/move_high \
are the plausible one-day move range in percent (basis points for UST10). Be calibrated: most single headlines \
move the S&P 500 well under 1%.
- takeaway: one plain sentence a trader would want, e.g. "Hotter core CPI cuts the odds of a December cut; \
pressure on long-duration tech and gold."
- confidence: High only when the direction is clear from the facts in the headline, not from tone.
Return one item per headline, echoing its id."""

ASSETS = sorted(TARGETS) + ["TICKER"]
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "event": {"type": "string"},
                    "takeaway": {"type": "string"},
                    "importance": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "polarity": {"type": "string", "enum": ["good", "bad", "mixed"]},
                    "impacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "asset": {"type": "string", "enum": ASSETS},
                                "ticker": {"type": "string"},
                                "direction": {"type": "string", "enum": ["up", "down", "either"]},
                                "move_low": {"type": "number"},
                                "move_high": {"type": "number"},
                            },
                            "required": ["asset", "ticker", "direction", "move_low", "move_high"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "relevant", "event", "takeaway", "importance", "confidence", "polarity",
                             "impacts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


PLAIN_JSON = ("\n\nAnswer with JSON only, no other text, shaped like this:\n"
              '{"items": [{"id": "0", "relevant": true, "event": "short name of the event", '
              '"takeaway": "one plain sentence", "importance": 55, "confidence": "High or Medium or Low", '
              '"polarity": "good or bad or mixed", "impacts": [{"asset": "one of ' + ", ".join(ASSETS) + '", '
              '"ticker": "the company ticker when asset is TICKER, else empty", "direction": "up or down or either", '
              '"move_low": 0.2, "move_high": 0.6}]}]}')


@dataclass
class AIStatus:
    enabled: bool
    model: str
    name: str = "Claude"
    free: bool = False
    calls_today: int = 0
    last_error: str | None = None
    last_ok: float | None = None


@dataclass(frozen=True)
class Limits:
    """A free plan's limits; None where the service doesn't publish one (its 429s then say when to slow down)."""
    rpm: int | None = None
    tpm: int | None = None
    rpd: int | None = None
    tpd: int | None = None


@dataclass(frozen=True)
class Plan:
    """A free service with an OpenAI-style chat API."""
    name: str
    url: str
    key_env: str
    model_env: str
    model: str
    prefer: tuple[str, ...]  # what to look for in the service's model list if the model is retired
    batch: int  # headlines per call
    estimate: int  # tokens a call is expected to use, until answers say
    max_tokens: int
    limits: Limits
    extra: tuple = ()  # (field, value) pairs sent while the service accepts them


GROQ = Plan("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b",
            ("gpt-oss-120b", "gpt-oss-20b", "qwen", "llama-3.3-70b"), batch=5, estimate=4000, max_tokens=3000,
            limits=Limits(rpm=30, tpm=8000, rpd=1000, tpd=200_000), extra=(("reasoning_effort", "low"),))
# Google shows its free limits only per account (aistudio.google.com/rate-limit); these stay under the usual ones.
GEMINI = Plan("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "GEMINI_MODEL",
              "gemini-3.5-flash-lite", ("flash-lite", "flash"), batch=10, estimate=6000, max_tokens=6000,
              limits=Limits(rpm=8, rpd=200), extra=(("reasoning_effort", "low"),))
PLANS = (GROQ, GEMINI)
NOT_CHAT = ("tts", "image", "live", "audio", "embed", "guard", "whisper", "transcribe", "translate", "orpheus")
MAX_PAUSE = 6 * 3600.0
MAX_CALLS_PER_RUN = 40


class ReaderError(Exception):
    def __init__(self, message: str, pause: float = 0.0, again: bool = False):
        super().__init__(message)
        self.pause = pause  # seconds the reader should rest
        self.again = again  # worth another try this run once the pause is over (it was only rate limited)


class _Retry(Exception):
    """Ask again straight away, differently (plain JSON, or another model)."""


class Pacer:
    """Keeps a free plan's calls under its per-minute and per-day limits (with a 10% margin), counting the tokens
    each answer says it used."""

    MARGIN = 0.9

    def __init__(self, limits: Limits, clock=time.time):
        self.limits, self.clock = limits, clock
        self.recent: list[list[float]] = []  # [when, tokens] for each call in the last minute, oldest first
        self.day = ""
        self.calls_today = 0
        self.tokens_today = 0.0
        self.typical = 0.0  # tokens a call has been using

    def roll(self) -> float:
        now = self.clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.day:
            self.day, self.calls_today, self.tokens_today = day, 0, 0.0
        self.recent = [r for r in self.recent if now - r[0] < 60]
        return now

    def wait(self, estimate: float) -> float | None:
        """Seconds until a call of about `estimate` tokens fits (0: now), or None when today's allowance is spent."""
        now = self.roll()
        lim, m = self.limits, self.MARGIN
        if lim.rpd and self.calls_today + 1 > lim.rpd * m:
            return None
        if lim.tpd and self.tokens_today + estimate > lim.tpd * m:
            return None
        wait = 0.0
        if lim.rpm:
            over = len(self.recent) + 1 - max(1, int(lim.rpm * m))  # calls that must age out first
            if over > 0:
                wait = max(wait, self.recent[over - 1][0] + 60 - now)
        if lim.tpm:
            budget = lim.tpm * m
            used = sum(r[1] for r in self.recent)
            need = min(estimate, budget)  # a call bigger than the budget goes alone
            for when, tokens in self.recent:
                if used + need <= budget:
                    break
                used -= tokens
                wait = max(wait, when + 60 - now)
        return max(0.0, wait)

    def start(self, estimate: float) -> list[float]:
        now = self.roll()
        entry = [now, float(estimate)]
        self.recent.append(entry)
        self.calls_today += 1
        self.tokens_today += estimate
        return entry

    def finish(self, entry: list[float], tokens) -> None:
        """What the call cost: the tokens its answer reports (0 for a refused call; None: keep the estimate)."""
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or not math.isfinite(tokens) \
                or tokens < 0:
            return
        self.tokens_today = max(0.0, self.tokens_today + tokens - entry[1])
        entry[1] = float(tokens)
        if tokens:
            self.typical = float(tokens) if not self.typical else 0.7 * self.typical + 0.3 * tokens


class Reader:
    """An AI service that reads a batch of headlines."""

    batch = BATCH
    free = False

    def __init__(self, name: str, model: str, clock=time.time):
        self.status = AIStatus(True, model, name, self.free)
        self.clock = clock
        self.paused_until = 0.0
        self._day = ""

    @property
    def name(self) -> str:
        return self.status.name

    @property
    def model(self) -> str:
        return self.status.model

    def calls_today(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        if day != self._day:
            self._day, self.status.calls_today = day, 0
        return self.status.calls_today

    def wait(self) -> float | None:
        """Seconds until this reader can take a call (0: now), or None when it has no calls left today."""
        return max(0.0, self.paused_until - self.clock())

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, self.clock() + min(seconds, MAX_PAUSE))

    async def read(self, prompt: str) -> list:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class ClaudeReader(Reader):
    def __init__(self, key: str, model: str = DEFAULT_MODEL, client=None, clock=time.time):
        super().__init__("Claude", model, clock)
        if client is None:
            import anthropic  # ImportError: the caller reports it
            client = anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=TIMEOUT)
        self.client = client

    async def read(self, prompt: str) -> list:
        import anthropic

        self.calls_today()
        self.status.calls_today += 1
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            raise ReaderError("rate limited", pause=60)
        except anthropic.APIStatusError as exc:
            log.warning("Claude news review failed: %s", exc)
            raise ReaderError(f"API error {exc.status_code}", pause=60)
        except anthropic.APIConnectionError:
            raise ReaderError("couldn't reach the API", pause=30)
        if response.stop_reason == "refusal":
            raise ReaderError("declined")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)["items"]
        except (ValueError, KeyError, TypeError):
            raise ReaderError("unreadable answer")


class OpenAIReader(Reader):
    """A free plan of a service with an OpenAI-style chat API (Groq, Gemini)."""

    free = True

    def __init__(self, plan: Plan, key: str, model: str | None = None, request=None, clock=time.time):
        super().__init__(plan.name, model or plan.model, clock)
        self.plan, self.key = plan, key
        self.batch = plan.batch
        self.pacer = Pacer(plan.limits, clock)
        self.plain = False  # the service refused structured output (or an extra field): ask for plain JSON
        self.limited = 0  # 429s in a row
        self.searched = False  # looked for another model after a 404
        self._request = request or self._aiohttp
        self._session = None

    def calls_today(self) -> int:
        self.pacer.roll()
        self.status.calls_today = self.pacer.calls_today
        return self.status.calls_today

    def wait(self) -> float | None:
        paced = self.pacer.wait(self.pacer.typical or self.plan.estimate)
        return None if paced is None else max(super().wait(), paced)

    async def read(self, prompt: str) -> list:
        for _ in range(3):  # a refused format or a retired model gets another try straight away
            try:
                return await self._ask(prompt)
            except _Retry:
                continue
        raise ReaderError("the service keeps refusing the request", pause=600)

    async def _ask(self, prompt: str) -> list:
        body = {"model": self.model, "max_completion_tokens": self.plan.max_tokens,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt + (PLAIN_JSON if self.plain else "")}]}
        if self.plain:
            body["response_format"] = {"type": "json_object"}
        else:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "headline_reads", "strict": True, "schema": SCHEMA}}
            body.update(dict(self.plan.extra))
        entry = self.pacer.start(self.pacer.typical or self.plan.estimate)
        self.status.calls_today = self.pacer.calls_today
        try:
            status, headers, text = await self._request("POST", f"{self.plan.url}/chat/completions", self._headers(),
                                                        body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network errors, timeouts
            raise ReaderError(f"couldn't reach {self.name} ({type(exc).__name__})", pause=60) from exc
        if status == 200:
            self.limited = 0
            return self._answer(entry, text)
        self.pacer.finish(entry, 0)
        detail = self._detail(text)
        if status == 429:
            self.limited += 1
            asked = retry_after(headers, text)
            floor = 5.0 * 2 ** (self.limited - 1) if asked is not None else 30.0 * 2 ** (self.limited - 1)
            pause = min(max(asked or 0.0, floor, 1.0), MAX_PAUSE)
            raise ReaderError(f"rate limited · paused {duration(pause)}", pause=pause, again=True)
        if status in (401, 403) or (status == 400 and re.search(r"api[ _-]?key", detail, re.I)):
            raise ReaderError(f"key rejected (HTTP {status})", pause=3600)
        if status == 404 and not self.searched:
            self.searched = True
            other = await self._other_model()
            if other and other != self.model:
                log.warning("%s: model %s isn't available; using %s", self.name, self.model, other)
                self.status.model = other
                raise _Retry
        if status in (400, 422) and not self.plain:
            log.info("%s refused the structured request (%s); asking for plain JSON", self.name, detail)
            self.plain = True
            raise _Retry
        raise ReaderError(f"HTTP {status}" + (f" ({detail})" if detail else ""),
                          pause=60 if status >= 500 or status == 413 else 600)

    def _answer(self, entry: list[float], text: str) -> list:
        try:
            data = json.loads(text)
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ReaderError("unreadable answer")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self.pacer.finish(entry, usage.get("total_tokens"))
        if choice.get("finish_reason") == "length":
            raise ReaderError("answer cut off")
        try:
            return parse_items(content)
        except ValueError:
            raise ReaderError("unreadable answer")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def _detail(self, text: str) -> str:
        """The service's own error message, short and without the key."""
        message = text or ""
        try:
            data = json.loads(text)
            data = data[0] if isinstance(data, list) and data else data
            error = data.get("error") if isinstance(data, dict) else None
            message = (error.get("message") if isinstance(error, dict) else error) or message
        except (ValueError, TypeError, AttributeError):
            pass
        message = str(message).strip()
        return "" if "<" in message[:20] else _quiet(message, [self.key])[:100]

    async def _other_model(self) -> str | None:
        try:
            status, _, text = await self._request("GET", f"{self.plan.url}/models", self._headers(), None)
            models = json.loads(text).get("data") if status == 200 else None
            ids = [str(m.get("id") or "").removeprefix("models/") for m in models or [] if isinstance(m, dict)]
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return pick_model(ids, self.plan.prefer)

    async def _aiohttp(self, method: str, url: str, headers: dict, body: dict | None):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        async with self._session.request(method, url, json=body, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, await resp.text(errors="replace")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


class NewsAI:
    def __init__(self, api_key: str | None = None, model: str | None = None, daily_calls: int | None = None,
                 readers: list[Reader] | None = None, env=None, sleep=asyncio.sleep, clock=time.monotonic):
        env = os.environ if env is None else env
        try:
            self.daily_calls = daily_calls or int(env.get("NEWS_AI_DAILY_CALLS") or DAILY_CALLS)
        except ValueError:
            self.daily_calls = DAILY_CALLS
        self.sleep, self.clock = sleep, clock
        self.problems: list[str] = []  # keys set for a reader that couldn't start
        if readers is not None:
            self.readers = list(readers)
            return
        self.readers: list[Reader] = []
        key = (env.get("ANTHROPIC_API_KEY", "") if api_key is None else api_key).strip()
        if key:
            try:
                self.readers.append(ClaudeReader(key, model or env.get("NEWS_AI_MODEL") or DEFAULT_MODEL))
            except ImportError:
                self.problems.append("ANTHROPIC_API_KEY is set but the anthropic package isn't installed")
                log.warning(self.problems[-1])
        for plan in PLANS:
            key = (env.get(plan.key_env) or "").strip()
            if key:
                self.readers.append(OpenAIReader(plan, key, (env.get(plan.model_env) or "").strip() or None))

    @property
    def enabled(self) -> bool:
        return bool(self.readers)

    def statuses(self) -> list[AIStatus]:
        return [r.status for r in self.readers]

    async def close(self) -> None:
        for reader in self.readers:
            try:
                await reader.close()
            except Exception:
                log.debug("Closing %s failed", reader.name, exc_info=True)

    async def review(self, analyses: list[Analysis]) -> int:
        """Replaces the rules' read with an AI read for these headlines (in place), most important first, until the
        readers run out of time or calls; returns how many changed."""
        if not self.readers or not analyses:
            return 0
        todo = sorted(analyses, key=lambda a: -a.importance)
        started = self.clock()
        done: set[int] = set()  # readers out for this run (failed or declined)
        changed = 0
        for _ in range(MAX_CALLS_PER_RUN):
            if not todo:
                break
            pick = self._pick(done, self.clock() - started)
            if pick is None:
                break
            reader, wait = pick
            if wait > 0:
                await self.sleep(wait)
            batch, todo = todo[:reader.batch], todo[reader.batch:]
            try:
                items = await reader.read(prompt_for(batch))
            except ReaderError as exc:
                reader.status.last_error = str(exc)
                if exc.pause:
                    reader.pause(exc.pause)
                if not exc.again:
                    done.add(id(reader))
                todo = batch + todo
                log.info("%s couldn't read the news: %s", reader.name, exc)
                continue
            reader.status.last_error, reader.status.last_ok = None, time.time()
            changed += fold(batch, items)
        if todo:
            log.info("%d headlines keep the rules' read (no reader free in time)", len(todo))
        return changed

    def _pick(self, done: set[int], elapsed: float) -> tuple[Reader, float] | None:
        """The reader to use next: the first one (in order of preference) that's free now, else the one free
        soonest, among those that still have calls today and can start before the run's time is up."""
        best = None
        for reader in self.readers:
            if id(reader) in done or reader.calls_today() >= self.daily_calls:
                continue
            wait = reader.wait()
            if wait is None or elapsed + wait > REVIEW_SECONDS:
                continue
            if best is None or wait < best[1]:
                best = (reader, wait)
            if wait == 0:
                break
        return best


def prompt_for(batch: list[Analysis]) -> str:
    lines = []
    for n, a in enumerate(batch):
        h = a.headline
        lines.append(json.dumps({"id": str(n), "source": h.source, "headline": h.title,
                                 "summary": h.summary[:400], "tickers": list(a.tickers[:4])}))
    return ("Assets: " + ", ".join(f"{k} = {v[1]}" for k, v in sorted(TARGETS.items())) +
            "\n\nHeadlines (JSON lines):\n" + "\n".join(lines))


def parse_items(text: str) -> list:
    """The items of a JSON answer, also when a model wraps it in a code block or a sentence."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start:end + 1])
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        raise ValueError("no items in the answer")
    return data


def retry_after(headers: dict, text: str) -> float | None:
    """How long a 429 says to wait, in seconds: the Retry-After header, Gemini's retryDelay or Groq's "try again
    in 1m2.5s"."""
    value = (headers or {}).get("retry-after")
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            pass
    text = text or ""
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', text)
    if m:
        return float(m.group(1))
    m = re.search(r"try again in ([\d.]+)ms", text)
    if m:
        return float(m.group(1)) / 1000
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?", text)
    if m and any(m.groups()):
        h, mins, secs = m.groups()
        return int(h or 0) * 3600 + int(mins or 0) * 60 + float(secs or 0)
    return None


def pick_model(ids: list[str], prefer: tuple[str, ...]) -> str | None:
    """The newest stable chat model whose name has the first of `prefer` that any has."""
    usable = [i for i in ids if i and not any(word in i.lower() for word in NOT_CHAT)]
    for want in prefer:
        found = [i for i in usable if want in i.lower()]
        if found:
            return max(found, key=lambda i: (not re.search(r"preview|exp", i), [int(n) for n in re.findall(r"\d+", i)]))
    return None


def duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def number(value) -> float | None:
    """A finite number from an answer field ("1.5", "1.5%", "-20bp" and 1.5 all count)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        m = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:%|bps?)?\s*", value, re.I)
        if not m:
            return None
        value = m.group(1)
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def clean(raw) -> dict | None:
    """An answer item with the types apply() expects. Free models don't always keep to the schema (numbers as text,
    other capitalisation, missing fields); None when it has no id."""
    if not isinstance(raw, dict) or raw.get("id") is None or isinstance(raw.get("id"), (dict, list)):
        return None
    item = {"id": str(raw["id"]).strip()}
    relevant = raw.get("relevant", True)
    if isinstance(relevant, str):
        relevant = relevant.strip().lower() not in ("false", "no", "0", "")
    item["relevant"] = bool(relevant)
    for key in ("event", "takeaway"):
        item[key] = raw[key].strip() if isinstance(raw.get(key), str) else ""
    importance = number(raw.get("importance"))
    if importance is not None:
        item["importance"] = int(round(max(0.0, min(100.0, importance))))
    confidence = str(raw.get("confidence") or "").strip().capitalize()
    if confidence in ("High", "Medium", "Low"):
        item["confidence"] = confidence
    polarity = str(raw.get("polarity") or "").strip().lower()
    item["polarity"] = polarity if polarity in ("good", "bad", "mixed") else "mixed"
    impacts = []
    for imp in raw.get("impacts") if isinstance(raw.get("impacts"), list) else []:
        if not isinstance(imp, dict):
            continue
        asset = str(imp.get("asset") or "").strip().upper()
        low, high = number(imp.get("move_low")), number(imp.get("move_high"))
        if asset not in ASSETS or (low is None and high is None):
            continue
        low, high = (high if low is None else low), (low if high is None else high)
        direction = str(imp.get("direction") or "").strip().lower()
        impacts.append({"asset": asset, "ticker": re.sub(r"[^A-Z0-9.\-]", "", str(imp.get("ticker") or "").upper()),
                        "direction": direction if direction in ("up", "down", "either") else "either",
                        "move_low": max(-50.0, min(50.0, low)), "move_high": max(-50.0, min(50.0, high))})
    item["impacts"] = impacts
    return item


def fold(batch: list[Analysis], items) -> int:
    """Applies an answer's items to their headlines; returns how many headlines got one."""
    changed: set[int] = set()
    for raw in items if isinstance(items, list) else []:
        item = clean(raw)
        if item is None:
            continue
        n = number(item["id"])
        if n is None or n != int(n):
            continue
        n = int(n)
        if 0 <= n < len(batch) and n not in changed:
            apply(batch[n], item)
            changed.add(n)
    return len(changed)


def apply(a: Analysis, item: dict) -> None:
    """Folds a model's read of one headline into its analysis."""
    a.source = "ai"
    a.note = (item.get("takeaway") or "").strip()[:300]
    a.confidence = item.get("confidence") or a.confidence
    a.polarity = {"good": 1, "bad": -1}.get(item.get("polarity"), 0)
    if not item.get("relevant", True):
        a.importance = min(a.importance, 15)
        a.impacts = []
        return
    a.importance = int(max(0, min(100, item.get("importance", a.importance))))
    impacts = []
    for imp in item.get("impacts") or []:
        asset = imp.get("asset")
        direction = {"up": 1, "down": -1}.get(imp.get("direction"), 0)
        low, high = sorted((abs(float(imp.get("move_low") or 0)), abs(float(imp.get("move_high") or 0))))
        if asset == "TICKER":
            ticker = (imp.get("ticker") or "").upper().strip()[:12]
            if not ticker:
                continue
            impacts.append(Impact(f"TICKER:{ticker}", ticker, ticker, direction, round(low, 2), round(high, 2), "%",
                                  (low + high) / 2))
        elif asset in TARGETS:
            symbol, name, unit = TARGETS[asset]
            impacts.append(Impact(asset, symbol, name, direction, round(low, 2), round(high, 2), unit,
                                  (low + high) / 2))
    a.impacts = impacts[:7]
