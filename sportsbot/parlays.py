"""Saves parlays built with /research parlay and grades every leg after the games.

Moneyline legs are graded from the final score. Player legs are graded from the
player's game log (or, for cricket, the match's ball-by-ball scorecard). A
player who didn't play has the leg voided, as books do. When a parlay is fully
settled the result is posted back to the channel it was built in.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict

import discord

from .leagues import LEAGUES
from .props import Leg

VOID_AFTER_SECONDS = 12 * 3600  # a player missing from the box score this long after the final didn't play


class ParlayBook:
    def __init__(self, state) -> None:
        self._state = state

    def record(self, channel_id: int, league: str, style: str, legs: list[Leg]) -> str:
        pid = uuid.uuid4().hex[:8]
        self._state.set("parlays", pid, {
            "id": pid, "channel": channel_id, "league": league, "style": style, "created": time.time(),
            "legs": [{**asdict(leg), "status": "pending"} for leg in legs], "status": "pending",
        })
        return pid

    def pending(self) -> list[dict]:
        return [p for _, p in self._state.items("parlays") if p["status"] == "pending"]

    def pending_leagues(self) -> set[str]:
        return {leg["league"] for p in self.pending() for leg in p["legs"] if leg["status"] == "pending"}

    def save(self, parlay: dict) -> None:
        statuses = [leg["status"] for leg in parlay["legs"]]
        if "miss" in statuses:
            parlay["status"] = "lost" if "pending" not in statuses else "pending"
        elif "pending" in statuses:
            parlay["status"] = "pending"
        elif all(s == "void" for s in statuses):
            parlay["status"] = "void"
        else:
            parlay["status"] = "won"
        self._state.set("parlays", parlay["id"], parlay)

    def summary(self) -> dict:
        """Parlay and leg results overall and per league, with predicted vs actual hit rates."""
        out = {}
        for _, p in self._state.items("parlays"):
            for bucket in ("all", p["league"]):
                s = out.setdefault(bucket, {"won": 0, "lost": 0, "hit": 0, "miss": 0, "predicted": 0.0})
                if p["status"] in ("won", "lost"):
                    s[p["status"]] += 1
                for leg in p["legs"]:
                    if leg["status"] in ("hit", "miss"):
                        s[leg["status"]] += 1
                        s["predicted"] += leg["probability"]
        return out


def grade_value(value: float | None, line: int) -> str:
    return "hit" if value is not None and value >= line else "miss"


async def settle(bot, book: ParlayBook) -> None:
    """Grades legs whose games are over, then posts parlays that just finished."""
    for parlay in book.pending():
        changed = False
        for leg in parlay["legs"]:
            if leg["status"] != "pending":
                continue
            final = await _final(bot, leg)
            if final is None:
                continue  # not over yet
            status, actual = await _grade(bot, leg, final)
            if status:
                leg["status"], leg["actual"] = status, actual
                changed = True
        if changed:
            before = parlay["status"]
            book.save(parlay)
            if before == "pending" and parlay["status"] != "pending":
                await bot._send(parlay["channel"], result_embed(parlay))


async def _final(bot, leg: dict) -> dict | None:
    """{"home": (id, score), "away": (id, score), "ended": ts} once the leg's game is final."""
    game = next((g for g in bot.latest.get(leg["league"], []) if g.id == leg["game_id"]), None)
    if game is not None:
        if game.state != "post":
            return None
        return {"home": (game.home.id, game.home.score), "away": (game.away.id, game.away.score),
                "ended": bot.state.get("finals", f"{leg['league']}:{leg['game_id']}") or _mark_final(bot, leg)}
    try:  # the game left the scoreboard: ask ESPN directly
        summary = await bot.espn.summary(leg["path"] or LEAGUES[leg["league"]].path, leg["game_id"])
    except Exception:
        return None
    comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
    if ((comp.get("status") or {}).get("type") or {}).get("state") != "post":
        return None
    teams = {c.get("homeAway"): (str((c.get("team") or {}).get("id")), _score(c.get("score")))
             for c in comp.get("competitors") or []}
    return {"home": teams.get("home"), "away": teams.get("away"),
            "ended": bot.state.get("finals", f"{leg['league']}:{leg['game_id']}") or _mark_final(bot, leg)}


def _mark_final(bot, leg: dict) -> float:
    now = time.time()
    bot.state.set("finals", f"{leg['league']}:{leg['game_id']}", now)
    return now


def _score(value) -> int:
    try:
        return int(float(str(value).split("/")[0]))
    except (TypeError, ValueError):
        return 0


async def _grade(bot, leg: dict, final: dict) -> tuple[str | None, str]:
    if leg["kind"] == "moneyline":
        (hid, hs), (aid, as_) = final["home"], final["away"]
        if hs == as_:
            return "miss", f"tie {hs}-{as_}"
        winner = hid if hs > as_ else aid
        away, _, home = leg["game"].partition(" @ ")
        score = f"{away} {as_} - {hs} {home}" if home else f"final {as_}-{hs}"
        return ("hit" if winner == leg["side"] else "miss"), score
    value = await _player_value(bot, leg)
    if value is None:
        if time.time() - final["ended"] >= VOID_AFTER_SECONDS:
            return "void", "didn't play"
        return None, ""  # box score not updated yet
    return grade_value(value, leg["line"]), f"{value:g}"


async def _player_value(bot, leg: dict) -> float | None:
    """The player's stat in that game, or None if they're not in its box score (yet)."""
    sport = LEAGUES[leg["league"]].sport
    try:
        if sport == "cricket":
            from .cricket_props import PLAYBYPLAY_URL, WHOLE_MATCH, parse_scorecard
            data = await bot.espn._get_json(PLAYBYPLAY_URL.format(path=leg["path"]),
                                            {"event": leg["game_id"], "limit": WHOLE_MATCH})
            card = parse_scorecard(data, leg["game_id"], "")
            if card is None:
                return None
            if leg["stat"] == "wickets":
                return next((float(r[4]) for r in card.bowling if r[0] == leg["player_id"]), None)
            index = {"runs": 3, "fours": 5, "sixes": 6}[leg["stat"]]
            return next((float(r[index]) for r in card.batting if r[0] == leg["player_id"]), None)
        games, _ = await bot.props.player_games(LEAGUES[leg["league"]].path, leg["player_id"], fresh=True)
    except Exception:
        return None
    game = next((g for g in games if g.event_id == leg["game_id"]), None)
    return game.stats.get(leg["stat"], 0.0) if game else None


ICONS = {"hit": "✅", "miss": "❌", "void": "➖", "pending": "⏳"}


def result_embed(parlay: dict) -> discord.Embed:
    legs = parlay["legs"]
    hits = sum(l["status"] == "hit" for l in legs)
    decided = sum(l["status"] in ("hit", "miss") for l in legs)
    title = {"won": "✅ Parlay won", "lost": "❌ Parlay lost", "void": "➖ Parlay void"}.get(parlay["status"], "⏳ Parlay")
    lines = [f"{ICONS[l['status']]} **{l['pick']}**" + (f" · {l['actual']}" if l.get("actual") else "")
             + f" · predicted ~{l['probability']:.0%}" for l in legs]
    league = LEAGUES.get(parlay["league"])
    embed = discord.Embed(title=f"🎟️ {title}: {hits}/{decided} legs hit",
                          description="\n".join(lines),
                          color=discord.Color.green() if parlay["status"] == "won" else discord.Color.dark_grey())
    embed.set_footer(text=f"{league.name if league else parlay['league']} · {parlay['style']} · /research record for the running record")
    return embed


def record_field(summary: dict) -> tuple[str, str] | None:
    """The parlay part of /research record: results, and how well the estimates held up."""
    s = summary.get("all")
    if not s or not (s["hit"] + s["miss"] + s["won"] + s["lost"]):
        return None
    lines = []
    for bucket, data in sorted(summary.items(), key=lambda kv: kv[0] != "all"):
        legs = data["hit"] + data["miss"]
        if not legs and not data["won"] + data["lost"]:
            continue
        name = "All" if bucket == "all" else LEAGUES[bucket].name if bucket in LEAGUES else bucket
        predicted = f"{100 * data['predicted'] / legs:.0f}%" if legs else "–"
        actual = f"{100 * data['hit'] / legs:.0f}%" if legs else "–"
        lines.append(f"**{name}**: parlays {data['won']}-{data['lost']} · legs {data['hit']}-{data['miss']} "
                     f"(hit {actual} vs predicted {predicted})")
    lines.append("*If legs hit well below the predicted %, trust the estimates less.*")
    return "🎟️ Parlays", "\n".join(lines)
