"""Player stats for a game (live or finished), from ESPN's box score: /stats, and the 📊 button on finals."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from .leagues import LEAGUES
from .limits import fitted

PLAYERS_PER_TEAM = 8  # the busiest players per team; a full box score would be too long for Discord


@dataclass(frozen=True)
class TeamStats:
    name: str
    lines: list[str]  # one per player, best first
    summary: str = ""  # team totals, e.g. possession and shots in soccer


def _num(value) -> float:
    try:
        return float(str(value).replace("+", ""))
    except ValueError:
        return 0.0


def _rows(group: dict) -> list[tuple[str, dict]]:
    """(player name, {stat key: value}) for everyone with stats in a box score group."""
    keys = group.get("keys") or []
    out = []
    for a in group.get("athletes") or []:
        stats = a.get("stats") or []
        if a.get("didNotPlay") or not stats:
            continue
        out.append(((a.get("athlete") or {}).get("displayName", "?"), dict(zip(keys, stats))))
    return out


def _basketball(groups: list[dict]) -> list[str]:
    rows = [r for g in groups for r in _rows(g) if r[1].get("minutes") not in (None, "0", "--")]
    rows.sort(key=lambda r: (_num(r[1].get("points")), _num(r[1].get("rebounds")) + _num(r[1].get("assists"))), reverse=True)
    lines = []
    for name, s in rows[:PLAYERS_PER_TEAM]:
        threes = s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "")
        lines.append(f"**{name}** {s.get('points', 0)} PTS · {s.get('rebounds', 0)} REB · {s.get('assists', 0)} AST"
                     + (f" · {threes.replace('-', '/')} 3PT" if threes and not threes.startswith("0-") else "")
                     + f" · {s.get('minutes', '?')} MIN")
    return lines


def _hockey(groups: list[dict]) -> list[str]:
    skaters, goalies, seen = [], [], set()
    for g in groups:
        for name, s in _rows(g):
            if name in seen:
                continue  # ESPN lists skaters under forwards/defense and again as skaters
            seen.add(name)
            (goalies if "saves" in s else skaters).append((name, s))
    skaters = [r for r in skaters if _num(r[1].get("goals")) + _num(r[1].get("assists")) or _num(r[1].get("shotsTotal")) >= 3]
    skaters.sort(key=lambda r: (_num(r[1].get("goals")) + _num(r[1].get("assists")), _num(r[1].get("goals")),
                                _num(r[1].get("shotsTotal"))), reverse=True)
    lines = []
    for name, s in skaters[:PLAYERS_PER_TEAM]:
        parts = [f"{s['goals']} G" if _num(s.get("goals")) else "", f"{s['assists']} A" if _num(s.get("assists")) else "",
                 f"{s.get('shotsTotal', 0)} SOG"]
        lines.append(f"**{name}** " + " · ".join(p for p in parts if p))
    for name, s in goalies:
        lines.append(f"🥅 **{name}** {s.get('saves', 0)}/{s.get('shotsAgainst', 0)} saves ({s.get('savePct', '')})")
    return lines


def _football(groups: list[dict]) -> list[str]:
    by_name = {g.get("name"): _rows(g) for g in groups}
    lines = []
    for name, s in by_name.get("passing", [])[:1]:
        lines.append(f"🎯 **{name}** {s.get('completions/passingAttempts', '')}, {s.get('passingYards', 0)} yds, "
                     f"{s.get('passingTouchdowns', 0)} TD, {s.get('interceptions', 0)} INT")
    rushing = sorted(by_name.get("rushing", []), key=lambda r: _num(r[1].get("rushingYards")), reverse=True)
    for name, s in [r for r in rushing if _num(r[1].get("rushingYards")) >= 20 or _num(r[1].get("rushingTouchdowns"))][:2]:
        lines.append(f"🏃 **{name}** {s.get('rushingAttempts', 0)} car, {s.get('rushingYards', 0)} yds"
                     + (f", {s['rushingTouchdowns']} TD" if _num(s.get("rushingTouchdowns")) else ""))
    receiving = sorted(by_name.get("receiving", []), key=lambda r: _num(r[1].get("receivingYards")), reverse=True)
    for name, s in receiving[:3]:
        lines.append(f"🙌 **{name}** {s.get('receptions', 0)} rec, {s.get('receivingYards', 0)} yds"
                     + (f", {s['receivingTouchdowns']} TD" if _num(s.get("receivingTouchdowns")) else ""))
    sackers = sorted((r for r in by_name.get("defensive", []) if _num(r[1].get("sacks"))),
                     key=lambda r: _num(r[1].get("sacks")), reverse=True)
    for name, s in sackers[:3]:
        lines.append(f"💥 **{name}** {s['sacks']} sack{'s' if _num(s['sacks']) != 1 else ''}, {s.get('totalTackles', 0)} tkl")
    for name, s in by_name.get("interceptions", []):
        lines.append(f"🛡️ **{name}** {s.get('interceptions', 1)} INT")
    return lines


def _baseball(groups: list[dict]) -> list[str]:
    batting = next((g for g in groups if g.get("type") == "batting"), {})
    pitching = next((g for g in groups if g.get("type") == "pitching"), {})
    hitters = [r for r in _rows(batting) if _num(r[1].get("hits")) or _num(r[1].get("RBIs")) or _num(r[1].get("runs"))]
    hitters.sort(key=lambda r: (_num(r[1].get("homeRuns")), _num(r[1].get("RBIs")), _num(r[1].get("hits"))), reverse=True)
    lines = []
    for name, s in hitters[:PLAYERS_PER_TEAM - 2]:
        parts = [s.get("hits-atBats", ""), f"{s['homeRuns']} HR" if _num(s.get("homeRuns")) else "",
                 f"{s['RBIs']} RBI" if _num(s.get("RBIs")) else "", f"{s['runs']} R" if _num(s.get("runs")) else ""]
        lines.append(f"**{name}** " + " · ".join(p for p in parts if p))
    for name, s in _rows(pitching)[:2]:
        lines.append(f"⚾ **{name}** {s.get('fullInnings.partInnings', '?')} IP, {s.get('hits', 0)} H, "
                     f"{s.get('earnedRuns', 0)} ER, {s.get('strikeouts', 0)} K")
    return lines


def _soccer_players(roster: list[dict], team_saves: int | None = None) -> list[str]:
    rows = []
    keepers = sum(1 for p in roster if (p.get("position") or {}).get("abbreviation") == "G"
                  and (p.get("starter") or any(x.get("name") == "subIns" and _num(x.get("value")) for x in p.get("stats") or [])))
    for p in roster:
        s = {x.get("name"): _num(x.get("value", x.get("displayValue"))) for x in p.get("stats") or []}
        name = (p.get("athlete") or {}).get("displayName", "?")
        goalkeeper = (p.get("position") or {}).get("abbreviation") == "G"
        if goalkeeper and (p.get("starter") or s.get("subIns")):
            saves = max(0, int(s.get("shotsFaced", 0) - s.get("goalsConceded", 0)))
            if team_saves is not None and keepers == 1:
                saves = team_saves  # ESPN's per-keeper shots faced is often 0; the team's saves are right
            rows.append((-1, f"🧤 **{name}** {saves} save{'s' if saves != 1 else ''}, {int(s.get('goalsConceded', 0))} conceded"))
            continue
        goals, assists, shots, on = (int(s.get(k, 0)) for k in ("totalGoals", "goalAssists", "totalShots", "shotsOnTarget"))
        cards = "🟥" * int(s.get("redCards", 0)) + "🟨" * int(s.get("yellowCards", 0))
        if not (goals or assists or shots or cards):
            continue
        parts = ([f"⚽ {goals}"] if goals else []) + ([f"🅰️ {assists}"] if assists else []) \
            + ([f"{shots} shot{'s' if shots != 1 else ''} ({on} on target)"] if shots else []) + ([cards] if cards else [])
        rows.append((goals * 100 + assists * 10 + on + shots / 10, f"**{name}** " + " · ".join(parts)))
    rows.sort(key=lambda r: r[0], reverse=True)
    players = [line for score, line in rows if score >= 0][:PLAYERS_PER_TEAM]
    return players + [line for score, line in rows if score < 0]


SOCCER_TEAM_STATS = (("possessionPct", "{}% possession"), ("totalShots", "{} shots"), ("shotsOnTarget", "{} on target"),
                     ("wonCorners", "{} corners"), ("foulsCommitted", "{} fouls"))


def _soccer_team(stats: list[dict]) -> str:
    values = {s.get("name"): s.get("displayValue") for s in stats}
    return " · ".join(fmt.format(values[k]) for k, fmt in SOCCER_TEAM_STATS if values.get(k) not in (None, ""))


SPORTS = {"basketball": _basketball, "hockey": _hockey, "football": _football, "baseball": _baseball}


def team_stats(summary: dict, sport: str) -> list[TeamStats]:
    """Each team's standout players (and, in soccer, team totals), in ESPN's team order."""
    if sport == "soccer":
        teams = {str((t.get("team") or {}).get("id")): t.get("statistics") or []
                 for t in (summary.get("boxscore") or {}).get("teams") or []}

        def saves(tid):
            found = next((x.get("displayValue") for x in teams.get(tid, []) if x.get("name") == "saves"), None)
            return int(_num(found)) if found not in (None, "") else None
        out = []
        for r in summary.get("rosters") or []:
            tid = str((r.get("team") or {}).get("id"))
            if r.get("roster"):
                out.append(TeamStats((r.get("team") or {}).get("displayName", "?"),
                                     _soccer_players(r["roster"], saves(tid)), _soccer_team(teams.get(tid, []))))
        return out
    parse = SPORTS.get(sport)
    if parse is None:
        return []
    return [TeamStats((t.get("team") or {}).get("displayName", "?"), parse(t.get("statistics") or []))
            for t in (summary.get("boxscore") or {}).get("players") or []]


def _scoreline(summary: dict, sport: str) -> tuple[str, str]:
    comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
    side = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    name = lambda c: (c.get("team") or {}).get("displayName", "?")
    home, away = side.get("home", {}), side.get("away", {})
    if sport == "soccer":  # the soccer way: home team first
        line = f"{name(home)} {home.get('score', '')} - {away.get('score', '')} {name(away)}"
    else:
        line = f"{name(away)} {away.get('score', '')} @ {name(home)} {home.get('score', '')}"
    status = ((comp.get("status") or {}).get("type") or {}).get("shortDetail", "")
    return line, status


@fitted
def stats_embed(summary: dict, league_key: str) -> discord.Embed:
    league = LEAGUES[league_key]
    line, status = _scoreline(summary, league.sport)
    embed = discord.Embed(title=f"📊 {league.emoji} {line}", color=discord.Color.blurple())
    teams = team_stats(summary, league.sport)
    if not any(t.lines or t.summary for t in teams):
        embed.description = "No player stats on ESPN for this game yet."
    for t in teams:
        value = "\n".join(([f"*{t.summary}*"] if t.summary else []) + t.lines) or "No stats yet."
        embed.add_field(name=t.name, value=value[:1024], inline=False)
    embed.set_footer(text=f"{league.name} · {status}" if status else league.name)
    return embed


class StatsButton(discord.ui.DynamicItem[discord.ui.Button], template=r"stats:(?P<league>[a-z0-9_]+):(?P<gid>[0-9]+)"):
    """The 📊 Player stats button under a final score. It keeps working after restarts."""

    def __init__(self, league_key: str, game_id: str):
        super().__init__(discord.ui.Button(label="Player stats", emoji="📊", style=discord.ButtonStyle.secondary,
                                           custom_id=f"stats:{league_key}:{game_id}"))
        self.league_key, self.game_id = league_key, game_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["league"], match["gid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        league = LEAGUES.get(self.league_key)
        if league is None:
            await interaction.response.send_message("I don't follow that league any more.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await interaction.client.espn.summary(league.path, self.game_id)
        except Exception:
            await interaction.followup.send("Couldn't reach ESPN for the stats, try again shortly.", ephemeral=True)
            return
        await interaction.followup.send(embed=stats_embed(summary, self.league_key), ephemeral=True)


STATS_SPORTS = ("basketball", "hockey", "football", "baseball", "soccer")


def stats_view(game) -> discord.ui.View | None:
    """The stats button for a game's post, for sports with box scores."""
    if game.league.sport not in STATS_SPORTS:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(StatsButton(game.league_key, game.id))
    return view
