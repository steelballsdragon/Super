"""Betting lines (DraftKings, via ESPN) and grading them at the final."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass

from .espn import Game

KEEP_SECONDS = 3 * 86400  # forget lines a few days after a game


@dataclass(frozen=True)
class Odds:
    provider: str
    home_ml: str | None = None  # e.g. "+170"
    away_ml: str | None = None
    draw_ml: str | None = None  # soccer
    home_spread: float | None = None  # e.g. 4.5 for +4.5
    away_spread: float | None = None
    total: float | None = None  # over/under line


def _num(text) -> float | None:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(text or ""))
    return float(m.group()) if m else None


def _american(value) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if text.lower() == "even":
        return "+100"
    n = _num(text)
    if n is None:
        return None
    return f"{n:+.0f}"


def _close(market: dict | None, side: str, field: str):
    side_data = (market or {}).get(side) or {}
    return (side_data.get("close") or side_data.get("open") or {}).get(field)


def parse_odds(comp: dict) -> Odds | None:
    raw = (comp.get("odds") or [None])[0]
    if not isinstance(raw, dict):
        return None
    ml, spread, total = raw.get("moneyline"), raw.get("pointSpread"), raw.get("total")
    odds = Odds(
        provider=(raw.get("provider") or {}).get("name") or "Sportsbook",
        home_ml=_american(_close(ml, "home", "odds")),
        away_ml=_american(_close(ml, "away", "odds")),
        draw_ml=_american((raw.get("drawOdds") or {}).get("moneyLine")),
        home_spread=_num(_close(spread, "home", "line")),
        away_spread=_num(_close(spread, "away", "line")),
        total=_num(_close(total, "over", "line")) or _num(raw.get("overUnder")),
    )
    has_line = any(v is not None for v in (odds.home_ml, odds.away_ml, odds.home_spread, odds.total))
    return odds if has_line else None


def _fmt_spread(line: float) -> str:
    return "PK" if line == 0 else f"{line:+g}"


def line_text(game: Game, odds: Odds) -> str:
    """The pre-game line, e.g. for the kick-off post."""
    home, away = game.home.name, game.away.name
    lines = []
    if game.league.sport != "soccer" and odds.home_spread is not None and odds.away_spread is not None:
        lines.append(f"Spread: {away} {_fmt_spread(odds.away_spread)} · {home} {_fmt_spread(odds.home_spread)}")
    if odds.total is not None:
        unit = " goals" if game.league.sport in ("soccer", "hockey") else ""
        lines.append(f"Total: O/U {odds.total:g}{unit}")
    if odds.home_ml or odds.away_ml:
        price = {game.home.id: odds.home_ml, game.away.id: odds.away_ml}
        first, second = game.teams  # home first for soccer, away first for US sports
        parts = [f"{first.name} {price[first.id] or '–'}"]
        if odds.draw_ml:
            parts.append(f"Draw {odds.draw_ml}")
        parts.append(f"{second.name} {price[second.id] or '–'}")
        lines.append("Moneyline: " + " · ".join(parts))
    return "\n".join(lines)


def _regulation_score(game: Game) -> tuple[int, int]:
    """Soccer bets settle on the 90-minute score, so leave out extra-time goals."""
    def minute(g):
        m = re.match(r"(\d+)", g.minute)
        return int(m.group(1)) if m else 0
    reg = [g for g in game.goals if minute(g) <= 90]
    return (sum(g.team_id == game.home.id for g in reg), sum(g.team_id == game.away.id for g in reg))


def grade_text(game: Game, odds: Odds) -> str:
    """How the pre-game line settled, e.g. 'Spread: IND -4.5 ✅ covered'."""
    home, away = game.home, game.away
    hs, as_ = home.score, away.score
    note = ""
    went_long = "AET" in game.status_name or "PEN" in game.status_name or game.detail in ("AET", "FT-Pens")
    if game.league.sport == "soccer" and went_long and game.goals:
        hs, as_ = _regulation_score(game)
        note = f"\n*Settled on the 90-minute score: {home.name} {hs}-{as_} {away.name}*"
    lines = []
    if game.league.sport != "soccer" and odds.home_spread is not None:
        margin = hs + odds.home_spread - as_
        if margin == 0:
            lines.append(f"Spread: push ({home.name} {_fmt_spread(odds.home_spread)})")
        else:
            team, line = (home, odds.home_spread) if margin > 0 else (away, odds.away_spread if odds.away_spread is not None else -odds.home_spread)
            lines.append(f"Spread: {team.name} {_fmt_spread(line)} ✅ covered")
    if odds.total is not None:
        total = hs + as_
        if total == odds.total:
            lines.append(f"Total: push ({total})")
        else:
            lines.append(f"Total: {'Over' if total > odds.total else 'Under'} {odds.total:g} ✅ ({total})")
    if odds.home_ml or odds.away_ml:
        if hs == as_:
            if odds.draw_ml:
                lines.append(f"Moneyline: Draw {odds.draw_ml} ✅")
        else:
            winner, price = (home, odds.home_ml) if hs > as_ else (away, odds.away_ml)
            lines.append(f"Moneyline: {winner.name} {price or ''} ✅".replace("  ", " "))
    return "\n".join(lines) + note


class OddsBook:
    """Keeps each game's last pre-game line, since ESPN may change or drop it once play starts."""

    def __init__(self, state) -> None:
        self._state = state

    def _key(self, game: Game) -> str:
        return f"{game.league_key}:{game.id}"

    def remember(self, games: list[Game]) -> None:
        for g in games:
            if g.state != "pre" or g.odds is None:
                continue
            entry = {"odds": asdict(g.odds), "at": time.time()}
            stored = self._state.get("odds", self._key(g))
            if not stored or stored["odds"] != entry["odds"]:
                self._state.set("odds", self._key(g), entry)

    def get(self, game: Game) -> Odds | None:
        stored = self._state.get("odds", self._key(game))
        return Odds(**stored["odds"]) if stored else None

    def prune(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        for key, entry in self._state.items("odds"):
            if entry.get("at", 0) < cutoff:
                self._state.delete("odds", key)
