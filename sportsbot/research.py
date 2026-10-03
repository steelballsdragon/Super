"""Pre-game betting research from ESPN data, with every number traced to its source.

This doesn't promise winners. It lays out what the market says (DraftKings,
via ESPN), what ESPN's own model says, recent form and injuries, and only
suggests a lean where the data points away from the line. Leans are recorded
and graded so their real track record is visible with /record.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from .espn import Game
from .odds import Odds, parse_odds

# A lean needs the model and the market to disagree by at least this many
# percentage points, or the projected total to miss the line by enough.
MODEL_GAP_MIN = 5.0
# Low-scoring sports need a gap in goals/runs; percentages exaggerate there.
TOTAL_GAP_ABSOLUTE = {"soccer": 0.5, "hockey": 0.5, "baseball": 1.0}
TOTAL_GAP_SHARE = 0.07  # football, basketball
MIN_FORM_GAMES = 3
# A spread move this big since the open usually means news (injuries) or sharp money.
BIG_LINE_MOVE = 2.5
# Gaps this big are rarely the market being wrong: usually the model hasn't
# caught up with news like an injury. Such leans are flagged and kept Low.
SUSPICIOUS_GAP = 15.0
INJURY_STATUSES = ("Out", "Doubtful", "Injured Reserve", "Suspension")


def implied(american: str | None) -> float | None:
    """Win chance implied by American odds, before removing the bookmaker's margin."""
    m = re.fullmatch(r"([+-]?)(\d+)", (american or "").strip())
    if not m:
        return None
    n = int(m.group(2))
    return n / (n + 100) if m.group(1) == "-" else 100 / (n + 100)


def no_vig(*prices: str | None) -> list[float] | None:
    """Implied chances with the bookmaker's margin taken out, so they add up to 100%."""
    raw = [implied(p) for p in prices]
    if any(r is None for r in raw):
        return None
    total = sum(raw)
    return [r / total for r in raw]


@dataclass(frozen=True)
class Form:
    results: list[str]  # e.g. ["L 13-17 @DET", ...], most recent first
    scored: float
    allowed: float
    games: int


@dataclass(frozen=True)
class Injury:
    name: str
    position: str
    status: str


@dataclass
class Research:
    game: Game
    odds: Odds | None
    open_home_spread: float | None
    open_favorite: str | None  # abbreviation of the favorite when the line opened
    model: dict[str, float]  # team id -> ESPN win % (NFL Matchup Predictor)
    form: dict[str, Form]
    injuries: dict[str, list[Injury]]
    ats: dict[str, str]


@dataclass(frozen=True)
class Lean:
    market: str  # "moneyline", "spread" or "total"
    pick: str  # e.g. "WSH +4.5", "Under 47.5", "WSH +170"
    side: str  # team id, "over" or "under"
    line: float | None
    price: str | None
    confidence: str  # "Low", "Medium", "High"
    why: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)  # reasons the data might be misleading


def _num(text) -> float | None:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(text or ""))
    return float(m.group()) if m else None


def _form(team_events: dict) -> Form | None:
    results, scored, allowed = [], [], []
    for e in team_events.get("events") or []:
        score = re.fullmatch(r"(\d+)-(\d+)(?:\s.*)?", (e.get("score") or "").strip())
        result = e.get("gameResult")
        if not score or result not in ("W", "L", "T", "D"):
            continue
        hi, lo = sorted((int(score.group(1)), int(score.group(2))), reverse=True)
        mine, theirs = (hi, lo) if result == "W" else (lo, hi)  # ESPN lists the winner's score first
        opp = (e.get("opponent") or {}).get("abbreviation", "?")
        results.append(f"{result} {mine}-{theirs} {e.get('atVs', 'vs')}{opp}")
        scored.append(mine)
        allowed.append(theirs)
    if not results:
        return None
    return Form(results[:5], sum(scored) / len(scored), sum(allowed) / len(allowed), len(results))


def parse_research(summary: dict, game: Game) -> Research:
    pick = (summary.get("pickcenter") or [{}])[0] if isinstance(summary.get("pickcenter"), list) else {}
    odds = parse_odds({"odds": [pick]}) if pick else None
    spread = (pick.get("pointSpread") or {}).get("home") or {}
    open_home = _num((spread.get("open") or {}).get("line"))
    open_fav = None
    for side, team in (("homeTeamOdds", game.home), ("awayTeamOdds", game.away)):
        if (pick.get(side) or {}).get("favoriteAtOpen"):
            open_fav = team.name

    model = {}
    pred = summary.get("predictor") or {}
    for side, team in (("homeTeam", game.home), ("awayTeam", game.away)):
        pct = _num((pred.get(side) or {}).get("gameProjection"))
        if pct is not None:
            model[team.id] = pct

    form = {}
    for t in summary.get("lastFiveGames") or []:
        tid = str((t.get("team") or {}).get("id", ""))
        if (f := _form(t)) is not None:
            form[tid] = f

    injuries = {}
    for t in summary.get("injuries") or []:
        tid = str((t.get("team") or {}).get("id", ""))
        listed = [
            Injury((i.get("athlete") or {}).get("displayName", "?"),
                   ((i.get("athlete") or {}).get("position") or {}).get("abbreviation", ""),
                   i.get("status", ""))
            for i in t.get("injuries") or [] if i.get("status") in INJURY_STATUSES
        ]
        # Quarterbacks first: in the NFL they move lines more than anyone.
        injuries[tid] = sorted(listed, key=lambda i: (i.position != "QB", INJURY_STATUSES.index(i.status)))

    ats = {}
    for t in summary.get("againstTheSpread") or []:
        tid = str((t.get("team") or {}).get("id", ""))
        for rec in t.get("records") or []:
            if rec.get("summary"):
                ats[tid] = f"{rec['summary']} ATS" + (f" ({rec['type']})" if rec.get("type") else "")
                break
    return Research(game, odds, open_home, open_fav, model, form, injuries, ats)


def market_chances(r: Research) -> dict[str, float] | None:
    """No-vig win chances by team id (and "draw" for soccer)."""
    o = r.odds
    if not o or not (o.home_ml and o.away_ml):
        return None
    if o.draw_ml:
        h, d, a = no_vig(o.home_ml, o.draw_ml, o.away_ml) or (None, None, None)
        return None if h is None else {r.game.home.id: h, "draw": d, r.game.away.id: a}
    pair = no_vig(o.home_ml, o.away_ml)
    return None if pair is None else {r.game.home.id: pair[0], r.game.away.id: pair[1]}


def _confidence(gap: float, strong: float, medium: float) -> str:
    return "High" if gap >= strong else "Medium" if gap >= medium else "Low"


def _side_cautions(r: Research, tid: str) -> list[str]:
    """Reasons a lean on this team might be wrong despite the numbers."""
    g = r.game
    team = g.home if tid == g.home.id else g.away
    other = g.away if team is g.home else g.home
    cautions = []
    qbs = [i for i in r.injuries.get(tid, []) if i.position == "QB" and i.status in ("Out", "Doubtful")]
    if qbs:
        cautions.append(f"{team.name} QB {qbs[0].name} is {qbs[0].status}; the model may not reflect it")
    if r.open_home_spread is not None and r.odds and r.odds.home_spread is not None:
        move = r.odds.home_spread - r.open_home_spread  # positive: line moved toward the away team
        toward = g.away if move > 0 else g.home
        if abs(move) >= BIG_LINE_MOVE and toward is other:
            cautions.append(f"The line moved {abs(move):g} points toward {other.name} since it opened "
                            f"({g.home.name} {r.open_home_spread:+g} → {r.odds.home_spread:+g})")
    return cautions


def leans(r: Research) -> list[Lean]:
    g, o = r.game, r.odds
    found: list[Lean] = []
    market = market_chances(r)
    teams = {g.home.id: g.home, g.away.id: g.away}

    # Side: ESPN's model vs the market's no-vig price.
    if market and len(r.model) == 2 and "draw" not in market:
        tid = max(r.model, key=lambda t: r.model[t] - 100 * market[t])
        gap = r.model[tid] - 100 * market[tid]
        if gap >= MODEL_GAP_MIN:
            team = teams[tid]
            why = [f"ESPN Matchup Predictor gives {team.name} {r.model[tid]:.1f}%",
                   f"DraftKings prices {team.name} at {100 * market[tid]:.1f}% (no-vig)",
                   f"Model is {gap:.1f} points higher than the market"]
            cautions = _side_cautions(r, tid)
            if gap >= SUSPICIOUS_GAP:
                cautions.append("A gap this large is unusual and often means the model is missing news "
                                "(injuries, lineup changes); check before relying on it")
            conf = "Low" if cautions else _confidence(gap, 12, 8)
            price = o.home_ml if tid == g.home.id else o.away_ml
            found.append(Lean("moneyline", f"{team.name} {price}", tid, None, price, conf, why, cautions))
            line = o.home_spread if tid == g.home.id else o.away_spread
            if line is not None:
                found.append(Lean("spread", f"{team.name} {line:+g}", tid, line, None, conf, why, cautions))

    # Total: recent scoring vs the over/under.
    fh, fa = r.form.get(g.home.id), r.form.get(g.away.id)
    if o and o.total and fh and fa and min(fh.games, fa.games) >= MIN_FORM_GAMES:
        projected = (fh.scored + fa.allowed) / 2 + (fa.scored + fh.allowed) / 2
        diff = (projected - o.total) / o.total
        needed = TOTAL_GAP_ABSOLUTE.get(g.league.sport)
        gap_size = abs(projected - o.total) / needed if needed else abs(diff) / TOTAL_GAP_SHARE  # 1.0 = just enough
        if gap_size >= 1:
            side = "over" if diff > 0 else "under"
            why = [f"{g.home.name} last {fh.games}: {fh.scored:.1f} scored, {fh.allowed:.1f} allowed per game",
                   f"{g.away.name} last {fa.games}: {fa.scored:.1f} scored, {fa.allowed:.1f} allowed per game",
                   f"Projected total {projected:.1f} vs line {o.total:g} ({100 * diff:+.0f}%)"]
            # Five games of unadjusted scoring is a weak signal, so totals top out at Medium.
            found.append(Lean("total", f"{side.title()} {o.total:g}", side, o.total, None,
                              "Medium" if gap_size >= 1.7 else "Low", why))
    return found


def most_likely(r: Research) -> tuple[str, float, str] | None:
    """The single most likely result by the market: (label, chance, price)."""
    market = market_chances(r)
    if not market:
        return None
    o, g = r.odds, r.game
    price = {g.home.id: o.home_ml, g.away.id: o.away_ml, "draw": o.draw_ml}
    label = {g.home.id: f"{g.home.name} win", g.away.id: f"{g.away.name} win", "draw": "Draw"}
    key = max(market, key=market.get)
    return label[key], market[key], price[key] or ""


def payout_per_unit(price: str | None) -> float:
    """Profit for a 1-unit winning bet at American odds (standard -110 if unknown)."""
    n = _num(price) if price else -110
    n = n or -110
    return n / 100 if n > 0 else 100 / -n


def grade(lean: dict, game: Game) -> str | None:
    """'win', 'loss' or 'push' for a recorded lean, once the game is final."""
    hs, as_ = game.home.score, game.away.score
    if lean["market"] == "total":
        total = hs + as_
        if total == lean["line"]:
            return "push"
        return "win" if (total > lean["line"]) == (lean["side"] == "over") else "loss"
    mine, theirs = (hs, as_) if lean["side"] == game.home.id else (as_, hs)
    if lean["market"] == "spread":
        margin = mine + lean["line"] - theirs
        return "push" if margin == 0 else "win" if margin > 0 else "loss"
    return "push" if mine == theirs else "win" if mine > theirs else "loss"


class LeanBook:
    """Records leans before games start and grades them at the final."""

    def __init__(self, state) -> None:
        self._state = state

    def record(self, game: Game, found: list[Lean]) -> None:
        if game.state != "pre":
            return  # only pre-game leans count
        for lean in found:
            key = f"{game.league_key}:{game.id}:{lean.market}"
            if self._state.get("leans", key) is None:
                self._state.set("leans", key, {**asdict(lean), "game": game.id, "league": game.league_key,
                                               "matchup": f"{game.away.name} @ {game.home.name}", "at": time.time()})

    def settle(self, game: Game) -> list[dict]:
        settled = []
        for key, lean in self._state.items("leans"):
            if lean.get("game") == game.id and lean.get("league") == game.league_key and "result" not in lean:
                lean = {**lean, "result": grade(lean, game), "final": f"{game.away.name} {game.away.score} - {game.home.score} {game.home.name}"}
                self._state.set("leans", key, lean)
                settled.append(lean)
        return settled

    async def settle_pending(self, bot) -> int:
        """Grades leans whose games are over, even if the bot never saw them end (e.g. restarting at
        the time). Postponed/cancelled games, and leans unresolved after a week, are voided."""
        from types import SimpleNamespace
        from .parlays import EXPIRE_SECONDS, game_result
        done = 0
        for key, lean in self._state.items("leans"):
            if "result" in lean:
                continue
            if time.time() - lean.get("at", time.time()) > EXPIRE_SECONDS:
                self._state.set("leans", key, {**lean, "result": "void", "final": "never settled"})
                done += 1
                continue
            final = await game_result(bot, lean["league"], lean["game"])
            if final is None:
                continue
            if final["called_off"]:
                self._state.set("leans", key, {**lean, "result": "void", "final": final["detail"]})
            else:
                (hid, hs), (aid, as_) = final["home"], final["away"]
                g = SimpleNamespace(home=SimpleNamespace(id=hid, score=hs), away=SimpleNamespace(id=aid, score=as_))
                self._state.set("leans", key, {**lean, "result": grade(lean, g), "final": f"{as_}-{hs}"})
            done += 1
        return done

    def pending_leagues(self) -> set[str]:
        return {lean["league"] for _, lean in self._state.items("leans") if "result" not in lean}

    def pending(self) -> int:
        return sum("result" not in lean for _, lean in self._state.items("leans"))

    def summary(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for _, lean in self._state.items("leans"):
            if lean.get("result") not in ("win", "loss", "push"):
                continue  # pending, or voided (postponed games don't count)
            for bucket in ("all", lean["market"], lean.get("confidence", "Low")):
                s = out.setdefault(bucket, {"win": 0, "loss": 0, "push": 0, "units": 0.0})
                s[lean["result"]] += 1
                s["units"] += payout_per_unit(lean.get("price")) if lean["result"] == "win" else -1.0 if lean["result"] == "loss" else 0.0
        return out


# ---------- Discord formatting ----------

import discord  # noqa: E402  (kept with the formatting it's used for)

from .limits import fitted  # noqa: E402

DISCLAIMER = "Research, not advice · leans are tracked: /record"


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


@fitted
def report_embed(r: Research, found: list[Lean]) -> discord.Embed:
    g, o = r.game, r.odds
    a, b = g.teams
    embed = discord.Embed(
        title=f"🔎 {a.name} vs {b.name}",
        description=f"{g.league.emoji} {g.league.name} · " + (f"<t:{int(_start_ts(g))}:f>" if _start_ts(g) else g.detail),
        color=discord.Color.dark_teal(),
    )
    market = market_chances(r)
    if o:
        lines = []
        if o.home_spread is not None and o.away_spread is not None and g.league.sport != "soccer":
            lines.append(f"Spread: {g.away.name} {o.away_spread:+g} · {g.home.name} {o.home_spread:+g}")
        if o.total is not None:
            lines.append(f"Total: O/U {o.total:g}")
        if market:
            parts = [f"{t.name} {(o.home_ml if t is g.home else o.away_ml)} ({_pct(market[t.id])})" for t in (a, b)]
            if "draw" in market:
                parts.insert(1, f"Draw {o.draw_ml} ({_pct(market['draw'])})")
            lines.append("Moneyline: " + " · ".join(parts))
            lines.append("*% = implied chance with the bookmaker's margin removed*")
        if r.open_home_spread is not None and o.home_spread is not None and r.open_home_spread != o.home_spread:
            lines.append(f"Line move: {g.home.name} {r.open_home_spread:+g} at open → {o.home_spread:+g} now")
        embed.add_field(name=f"📈 Market ({o.provider})", value="\n".join(lines) or "No line yet", inline=False)
    if len(r.model) == 2:
        lines = [f"{t.name}: {r.model[t.id]:.1f}%" + (f" (market {_pct(market[t.id])})" if market else "") for t in (a, b)]
        embed.add_field(name="🧮 ESPN Matchup Predictor", value="\n".join(lines), inline=False)
    form_lines = []
    for t in (a, b):
        f = r.form.get(t.id)
        if f:
            form_lines.append(f"**{t.name}** {' · '.join(f.results)}\n  avg {f.scored:.1f} scored, {f.allowed:.1f} allowed")
        if t.id in r.ats:
            form_lines.append(f"  {r.ats[t.id]}")
    if form_lines:
        embed.add_field(name="📋 Last 5 (ESPN)", value="\n".join(form_lines)[:1024], inline=False)
    inj_lines = [f"**{t.name}** " + ", ".join(f"{i.name} ({i.position}, {i.status})" for i in r.injuries[t.id][:5])
                 for t in (a, b) if r.injuries.get(t.id)]
    if inj_lines:
        embed.add_field(name="🩹 Injuries (ESPN)", value="\n".join(inj_lines)[:1024], inline=False)
    if found:
        text = []
        for lean in found:
            text.append(f"**{lean.pick}** ({lean.market}, {lean.confidence} confidence)")
            text += [f"• {w}" for w in lean.why]
            text += [f"⚠️ {c}" for c in lean.cautions]
        embed.add_field(name="💡 Leans", value="\n".join(text)[:1024], inline=False)
    else:
        embed.add_field(name="💡 Leans", value="None: the data doesn't disagree with the line enough to suggest one.", inline=False)
    if (ml := most_likely(r)):
        label, chance, price = ml
        embed.add_field(name="🎯 Most likely result (market)", value=f"{label} · {_pct(chance)} at {price}", inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


def _start_ts(g: Game) -> float | None:
    from .espn import start_time
    s = start_time(g)
    return s.timestamp() if s else None


@fitted
def picks_embed(league_name: str, emoji: str, reports: list[tuple[Research, list[Lean]]]) -> discord.Embed:
    rows = []
    for r, found in reports:
        spread = {l.side: l for l in found if l.market == "spread"}
        for lean in found:
            if lean.market == "spread" and any(l.market == "moneyline" and l.side == lean.side for l in found):
                continue  # shown together with the moneyline lean
            pick = lean.pick
            if lean.market == "moneyline" and lean.side in spread:
                pick = f"{spread[lean.side].pick} / ML {lean.price}"  # e.g. "DAL +3 / ML +136"
            strength = {"High": 3, "Medium": 2, "Low": 1}[lean.confidence]
            rows.append((strength, -len(lean.cautions), r, lean, pick))
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lines, used = [], 0
    for _, _, r, lean, pick in rows[:10]:
        g = r.game
        warn = " ⚠️" if lean.cautions else ""
        entry = f"**{pick}** · {g.away.name} @ {g.home.name} · {lean.confidence}{warn}\n  {lean.why[-1]}"
        if used + len(entry) + 1 > 1000:
            break  # whole entries only, within Discord's 1024-character field limit
        lines.append(entry)
        used += len(entry) + 1
    likely = sorted(((ml, r) for r in (x[0] for x in reports) if (ml := most_likely(r))), key=lambda x: -x[0][1])[:5]
    embed = discord.Embed(title=f"🔎 {emoji} {league_name} research", color=discord.Color.dark_teal())
    embed.add_field(name="💡 Leans (strongest first)",
                    value="\n".join(lines) or "None today: no game's data disagrees with its line enough.", inline=False)
    if likely:
        embed.add_field(
            name="🎯 Most likely results (market)",
            value="\n".join(f"{label} ({r.game.away.name} @ {r.game.home.name}) · {_pct(c)} at {p}" for (label, c, p), r in likely)
            + "\n*Likely ≠ good value: short prices pay little.*",
            inline=False,
        )
    embed.set_footer(text="Add a team to /research for the full data behind a lean · " + DISCLAIMER)
    return embed


@fitted
def record_embed(summary: dict[str, dict], pending: int) -> discord.Embed:
    embed = discord.Embed(title="📒 Research lean record", color=discord.Color.dark_teal())
    if not summary:
        embed.description = f"No graded leans yet ({pending} waiting on games)."
        return embed
    lines = []
    for bucket in ("all", "moneyline", "spread", "total", "High", "Medium", "Low"):
        s = summary.get(bucket)
        if not s:
            continue
        if bucket == "High":
            lines.append("")  # by-confidence breakdown below
        if bucket in ("High", "Medium", "Low"):
            bucket = f"{bucket} confidence"
        decided = s["win"] + s["loss"]
        rate = f"{100 * s['win'] / decided:.0f}%" if decided else "–"
        lines.append(f"**{bucket.title()}**: {s['win']}-{s['loss']}-{s['push']} ({rate}) · {s['units']:+.2f} units")
    lines.append(f"\n{pending} lean(s) waiting on games.")
    lines.append("*Break-even at standard -110 prices is about 52.4%. Small samples swing a lot.*")
    embed.description = "\n".join(lines)
    return embed
