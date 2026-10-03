"""Turns games and updates into Discord embeds."""

from __future__ import annotations

from datetime import datetime

import discord

from .espn import Game, Goal, ScoringPlay
from .leagues import LEAGUES
from .tracker import FINAL, HALFTIME, KICKOFF, SCORE, Update

COLORS = {
    KICKOFF: discord.Color.blue(),
    SCORE: discord.Color.green(),
    HALFTIME: discord.Color.gold(),
    FINAL: discord.Color.dark_grey(),
}


def _timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"<t:{int(dt.timestamp())}:f>"


def _goal_line(game: Game, goal: Goal) -> str:
    team = next((t.abbrev for t in game.teams if t.id == goal.team_id), None)
    return f"⚽ {goal.describe()}" + (f" ({team})" if team else "")


def _period(n: int) -> str:
    return f"Q{n}" if n <= 4 else "OT" if n == 5 else f"{n - 4}OT"


def play_embed(game: Game, play: ScoringPlay) -> discord.Embed:
    league = LEAGUES[game.league_key]
    title = f"{league.emoji} {(play.category or 'Score').upper()} — {play.team_abbrev}".rstrip(" —")
    score = f"**{game.away.name} {play.away_score} - {play.home_score} {game.home.name}**"
    desc = f"{score}\n*{play.kind}*\n{play.text}" if play.kind else f"{score}\n{play.text}"
    embed = discord.Embed(title=title, description=desc, color=COLORS[SCORE])
    embed.set_footer(text=f"{league.name} · {_period(play.period)} {play.clock}".strip())
    team = next((t for t in game.teams if t.abbrev == play.team_abbrev), None)
    if team and team.logo:
        embed.set_thumbnail(url=team.logo)
    return embed


def _add_leaders(embed: discord.Embed, game: Game) -> None:
    if game.leaders:
        embed.add_field(
            name="Game leaders",
            value="\n".join(f"**{l.category}** {l.athlete} — {l.stats}" for l in game.leaders),
            inline=False,
        )


def update_embed(update: Update) -> discord.Embed:
    game = update.game
    league = LEAGUES[game.league_key]
    is_soccer = league.sport == "soccer"
    if update.play is not None:
        return play_embed(game, update.play)

    if update.kind == KICKOFF:
        title = f"{league.emoji} {'Kick-off' if is_soccer else 'Game started'}"
    elif update.kind == SCORE and update.score_decreased:
        title = f"{league.emoji} Score correction"
    elif update.kind == SCORE:
        title = f"{league.emoji} {'GOAL!' if is_soccer else 'Score update'}"
    elif update.kind == HALFTIME:
        title = f"{league.emoji} Half-time"
    else:
        title = f"{league.emoji} Full-time" if is_soccer else f"{league.emoji} Final"

    lines = [f"**{game.scoreline()}**"]
    if update.kind == SCORE:
        if update.new_goals:
            lines += [_goal_line(game, g) for g in update.new_goals]
        elif game.last_play:
            lines.append(game.last_play)
    elif update.kind == FINAL:
        winner = max(game.teams, key=lambda t: t.score)
        loser = min(game.teams, key=lambda t: t.score)
        if winner.score == loser.score:
            lines.append("Draw" if is_soccer else "Tie")
        else:
            lines.append(f"{winner.name} win")

    embed = discord.Embed(title=title, description="\n".join(lines), color=COLORS[update.kind])
    embed.set_footer(text=f"{league.name} · {game.detail}")
    leader = max(game.teams, key=lambda t: t.score)
    if update.kind == SCORE and leader.logo:
        embed.set_thumbnail(url=leader.logo)
    if update.kind in (HALFTIME, FINAL):
        _add_leaders(embed, game)
    return embed


def game_line(game: Game) -> str:
    a, b = game.teams
    if game.state == "pre":
        return f"🕒 {a.abbrev} vs {b.abbrev} · {_timestamp(game.start)}"
    score = f"{a.abbrev} **{a.score} - {b.score}** {b.abbrev}"
    if game.state == "in":
        return f"🔴 {score} · {game.detail}"
    return f"✅ {score} · {game.detail}"


def scoreboard_embed(league_key: str, games: list[Game], team: str | None = None) -> discord.Embed:
    league = LEAGUES[league_key]
    order = {"in": 0, "pre": 1, "post": 2}
    games = sorted(games, key=lambda g: (order.get(g.state, 3), g.start))
    title = f"{league.emoji} {league.name} scores"
    if team:
        title += f" — {team}"
    if not games:
        desc = "No games on the current scoreboard."
    else:
        lines = [game_line(g) for g in games]
        desc = ""
        for i, line in enumerate(lines):
            if len(desc) + len(line) + 1 > 3900:
                desc += f"\n…and {len(lines) - i} more"
                break
            desc += line + "\n"
    return discord.Embed(title=title, description=desc, color=discord.Color.blurple())
