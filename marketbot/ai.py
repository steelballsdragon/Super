"""Optional: Claude reads the important headlines as a second opinion on the keyword rules.

Turned on by setting ANTHROPIC_API_KEY (and installing the `anthropic` package). Headlines the rules already
rank as worth posting are sent in batches; Claude's read (event, takeaway, which markets move, which way and
roughly how much) replaces the rules' guess for those headlines. Without a key the bot works the same, on
rules alone.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from .news import TARGETS, Analysis, Impact

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
BATCH = 10
DAILY_CALLS = 150

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


@dataclass
class AIStatus:
    enabled: bool
    model: str
    calls_today: int = 0
    last_error: str | None = None
    last_ok: float | None = None


class NewsAI:
    def __init__(self, api_key: str | None = None, model: str | None = None, daily_calls: int | None = None):
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("NEWS_AI_MODEL") or DEFAULT_MODEL
        self.daily_calls = daily_calls or int(os.environ.get("NEWS_AI_DAILY_CALLS") or DAILY_CALLS)
        self.client = None
        self.status = AIStatus(False, self.model)
        self._day = ""
        if not key:
            return
        try:
            import anthropic
        except ImportError:
            self.status.last_error = "ANTHROPIC_API_KEY is set but the anthropic package isn't installed"
            log.warning(self.status.last_error)
            return
        self.client = anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=90.0)
        self.status.enabled = True

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _budget_left(self) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._day:
            self._day, self.status.calls_today = day, 0
        return self.status.calls_today < self.daily_calls

    async def review(self, analyses: list[Analysis]) -> int:
        """Replaces the rules' read with Claude's for these headlines (in place); returns how many changed."""
        if not self.enabled or not analyses:
            return 0
        changed = 0
        for i in range(0, len(analyses), BATCH):
            if not self._budget_left():
                break
            changed += await self._review_batch(analyses[i:i + BATCH])
        return changed

    async def _review_batch(self, batch: list[Analysis]) -> int:
        import anthropic

        lines = []
        for n, a in enumerate(batch):
            h = a.headline
            lines.append(json.dumps({"id": str(n), "source": h.source, "headline": h.title,
                                     "summary": h.summary[:400], "tickers": list(a.tickers[:4])}))
        prompt = ("Assets: " + ", ".join(f"{k} = {v[1]}" for k, v in sorted(TARGETS.items())) +
                  "\n\nHeadlines (JSON lines):\n" + "\n".join(lines))
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
            self.status.last_error = "rate limited"
            return 0
        except anthropic.APIStatusError as exc:
            self.status.last_error = f"API error {exc.status_code}"
            log.warning("Claude news review failed: %s", exc)
            return 0
        except anthropic.APIConnectionError:
            self.status.last_error = "couldn't reach the API"
            return 0
        if response.stop_reason == "refusal":
            self.status.last_error = "declined"
            return 0
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            items = json.loads(text)["items"]
        except (ValueError, KeyError, TypeError):
            self.status.last_error = "unreadable answer"
            return 0
        self.status.last_error, self.status.last_ok = None, time.time()
        changed = 0
        for item in items:
            try:
                a = batch[int(item["id"])]
            except (ValueError, IndexError, KeyError):
                continue
            apply(a, item)
            changed += 1
        return changed


def apply(a: Analysis, item: dict) -> None:
    """Folds Claude's read of one headline into its analysis."""
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
