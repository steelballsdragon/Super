"""Turns games and updates into Discord embeds."""

from __future__ import annotations

from datetime import datetime

import discord

from .espn import Game, Goal, ScoringPlay, Team, period_label
from .leagues import LEAGUES
from .tracker import FINAL, HALFTIME, INNINGS, KICKOFF, PERIOD, SCORE, WICKET, Update

COLORS = {
    KICKOFF: discord.Color.blue(),
    SCORE: discord.Color.green(),
    PERIOD: discord.Color.teal(),
    HALFTIME: discord.Color.gold(),
    WICKET: discord.Color.red(),
    INNINGS: discord.Color.gold(),
    FINAL: discord.Color.dark_grey(),
}

KICKOFF_TITLES = {
    "soccer": "Kick-off",
    "football": "Game started",
    "basketball": "Tip-off",
    "baseball": "First pitch",
    "hockey": "Puck drop",
    "cricket": "Match started",
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


def _play_team(game: Game, play: ScoringPlay) -> Team | None:
    return next(
        (t for t in game.teams if (play.team_id and t.id == play.team_id) or (play.team_abbrev and t.abbrev == play.team_abbrev)),
        None,
    )


def play_embed(game: Game, play: ScoringPlay) -> discord.Embed:
    league = game.league
    team = _play_team(game, play)
    abbrev = play.team_abbrev or (team.abbrev if team else "")
    title = f"{league.emoji} {(play.category or 'Score').upper()}" + (f" — {abbrev}" if abbrev else "")
    score = f"**{game.away.name} {play.away_score} - {play.home_score} {game.home.name}**"
    desc = f"{score}\n*{play.kind}*\n{play.text}" if play.kind else f"{score}\n{play.text}"
    embed = discord.Embed(title=title, description=desc, color=COLORS[SCORE])
    embed.set_footer(text=f"{league.name} · {play.when}" if play.when else league.name)
    if team and team.logo:
        embed.set_thumbnail(url=team.logo)
    return embed


def _add_leaders(embed: discord.Embed, game: Game) -> None:
    if not game.leaders:
        return
    team_based = game.league.sport != "football"
    embed.add_field(
        name="Top performers" if team_based else "Game leaders",
        value="\n".join(f"**{l.category}** {l.athlete} — {l.stats}" for l in game.leaders),
        inline=False,
    )


def _title(update: Update) -> str:
    game, kind = update.game, update.kind
    sport = game.league.sport
    if kind == KICKOFF:
        text = KICKOFF_TITLES.get(sport, "Game started")
    elif kind == SCORE and update.score_decreased:
        text = "Score correction"
    elif kind == SCORE:
        text = "GOAL!" if sport == "soccer" else "Score update"
    elif kind == PERIOD:
        text = f"End of {period_label(game.period, sport)}"
    elif kind == HALFTIME:
        text = "Half-time"
    elif kind == WICKET:
        text = "WICKET!" if update.count == 1 else f"{update.count} WICKETS!"
    elif kind == INNINGS:
        text = "Innings break"
    else:
        text = {"soccer": "Full-time", "cricket": "Result"}.get(sport, "Final")
    return f"{game.league.emoji} {text}"


def _result(game: Game) -> str:
    if game.league.sport == "cricket":
        return game.summary
    winner = max(game.teams, key=lambda t: t.score)
    loser = min(game.teams, key=lambda t: t.score)
    if winner.score == loser.score:
        return "Draw" if game.league.sport == "soccer" else "Tie"
    return f"{winner.name} win"


def update_embed(update: Update) -> discord.Embed:
    game = update.game
    if update.play is not None:
        return play_embed(game, update.play)

    lines = [f"**{game.scoreline()}**"]
    if update.kind == SCORE:
        if update.new_goals:
            lines += [_goal_line(game, g) for g in update.new_goals]
        elif game.last_play:
            lines.append(game.last_play)
    elif update.kind in (KICKOFF, WICKET, INNINGS) and game.summary:
        lines.append(game.summary)  # cricket: toss result, or the chase equation
    elif update.kind == FINAL:
        lines.append(_result(game))

    embed = discord.Embed(title=_title(update), description="\n".join(l for l in lines if l), color=COLORS[update.kind])
    embed.set_footer(text=f"{game.league.name} · {game.detail}")
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
    icon = "🔴" if game.state == "in" else "✅"
    if game.league.sport == "cricket":
        score = " · ".join(f"{t.abbrev} **{t.score_text}**" if t.score_text else t.abbrev for t in (a, b))
        return f"{icon} {score}" + (f" — {game.summary}" if game.summary else "")
    return f"{icon} {a.abbrev} **{a.score} - {b.score}** {b.abbrev} · {game.detail}"


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
