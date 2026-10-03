"""Saves parlays built with /research and grades every leg after the games.

Moneyline legs are graded from the final score. Player legs are graded from the
player's game log (or, for cricket, the match's ball-by-ball scorecard). A
player who didn't play has the leg voided, as books do. When a parlay is fully
settled the result is posted back to the channel it was built in.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from datetime import datetime

import discord

from .leagues import LEAGUES
from .limits import fitted
from .props import Leg

VOID_AFTER_SECONDS = 12 * 3600  # a player missing from the box score this long after the final didn't play
EXPIRE_SECONDS = 7 * 86400  # anything still unresolved a week after it was made is voided
LOOKUP_SECONDS = 600  # how often a game that's not on the scoreboard is looked up while waiting for it to end
STATS_RETRY_SECONDS = 300  # how often a finished game's box score is re-read while a player's line is missing


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
            if time.time() - parlay["created"] > EXPIRE_SECONDS:
                leg["status"], leg["actual"] = "void", "never settled"
                changed = True
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


async def game_result(bot, league: str, game_id: str, path: str = "") -> dict | None:
    """How a game ended, once it has: scores, whether it was called off, and when we first saw it final.

    {"home": (id, score), "away": (id, score), "called_off": bool, "status": str, "ended": ts}
    Uses the latest scoreboard when the game is on it, otherwise asks ESPN for the game directly.
    """
    from .tracker import CALLED_OFF_WORDS
    key = f"{league}:{game_id}"
    game = next((g for g in bot.latest.get(league, []) if g.id == game_id), None)
    if game is not None:
        if game.state != "post":
            return None
        home, away = (game.home.id, game.home.score), (game.away.id, game.away.score)
        status, detail = game.status_name, game.detail
    else:
        # The game isn't on the scoreboard (another day, or its league isn't being checked): ask ESPN
        # directly, but not every minute, and not before it starts.
        lookups = getattr(bot, "result_lookups", {})
        if time.time() < lookups.get(key, 0):
            return None
        lookups[key] = time.time() + LOOKUP_SECONDS
        try:
            summary = await bot.espn.summary(path or LEAGUES[league].path, game_id)
        except Exception:
            return None
        comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
        stype = (comp.get("status") or {}).get("type") or {}
        if stype.get("state") != "post":
            start = _timestamp(comp.get("date"))
            if start and start > lookups[key]:
                lookups[key] = start
            return None
        lookups.pop(key, None)
        teams = {c.get("homeAway"): (str((c.get("team") or {}).get("id")), _score(c.get("score")))
                 for c in comp.get("competitors") or []}
        home, away = teams.get("home"), teams.get("away")
        status, detail = stype.get("name") or "", stype.get("shortDetail") or stype.get("detail") or ""
    if home is None or away is None:
        return None
    ended = bot.state.get("finals", key)
    if ended is None:
        ended = time.time()
        bot.state.set("finals", key, ended)
    return {"home": home, "away": away, "status": status, "ended": ended,
            "called_off": any(w in status for w in CALLED_OFF_WORDS), "detail": detail or "Postponed"}


async def _final(bot, leg: dict) -> dict | None:
    return await game_result(bot, leg["league"], leg["game_id"], leg.get("path", ""))


def _timestamp(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _score(value) -> int:
    try:
        return int(float(str(value).split("/")[0]))
    except (TypeError, ValueError):
        return 0


async def _grade(bot, leg: dict, final: dict) -> tuple[str | None, str]:
    if final["called_off"]:
        return "void", final["detail"]  # postponed/cancelled: books void the leg
    if leg["kind"] == "moneyline":
        (hid, hs), (aid, as_) = final["home"], final["away"]
        if hs == as_:
            return "miss", f"tie {hs}-{as_}"
        winner = hid if hs > as_ else aid
        away, _, home = leg["game"].partition(" @ ")
        score = f"{away} {as_} - {hs} {home}" if home else f"final {as_}-{hs}"
        return ("hit" if winner == leg["side"] else "miss"), score
    # Box scores (for cricket, the whole match's commentary) aren't re-read every minute while we wait.
    key = f"stats:{leg['league']}:{leg['game_id']}:{leg.get('player_id')}"
    lookups = getattr(bot, "result_lookups", {})
    if time.time() < lookups.get(key, 0):
        return None, ""
    value = await _player_value(bot, leg)
    if value is None:
        if time.time() - final["ended"] >= VOID_AFTER_SECONDS:
            return "void", "didn't play"
        lookups[key] = time.time() + STATS_RETRY_SECONDS
        return None, ""  # box score not updated yet
    lookups.pop(key, None)
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
        game = next((g for g in games if g.event_id == leg["game_id"]), None)
        if game is None:
            return None
        value = game.stats.get(leg["stat"], 0.0)
        if sport == "soccer" and leg["stat"] in FANDUEL_ASSIST_STATS:
            value += await _extra_fanduel_assists(bot, leg)
        return value
    except Exception:
        return None


FANDUEL_ASSIST_STATS = ("goalAssists", "goalOrAssist")


async def _extra_fanduel_assists(bot, leg: dict) -> int:
    """Assists FanDuel counts on top of the official ones (penalties or free kicks won, rebounds,
    forced own goals), from the match commentary. Assist bets are settled the FanDuel way."""
    from .espn import fanduel_assists, name_key
    name = leg.get("player") or leg["pick"].rsplit(" To Assist", 1)[0].rsplit(" Goal or Assist", 1)[0]
    summary = await bot.espn.summary(leg.get("path") or LEAGUES[leg["league"]].path, leg["game_id"])
    return sum(1 for f in fanduel_assists(summary) if f.how != "assist" and name_key(f.assist) == name_key(name))


ICONS = {"hit": "✅", "miss": "❌", "void": "➖", "pending": "⏳"}


@fitted
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
    embed.set_footer(text=f"{league.name if league else parlay['league']} · {parlay['style']} · /record for the running record")
    return embed


def record_field(summary: dict) -> tuple[str, str] | None:
    """The parlay part of /record: results, and how well the estimates held up."""
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
