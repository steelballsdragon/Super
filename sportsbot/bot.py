"""Discord bot entry point: slash commands plus the live-update loop."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

from .balls import BallFeed
from .espn import ESPNClient
from .formatting import ball_messages, scoreboard_embed, update_embed
from .leagues import LEAGUES
from .plays import AssistResolver, PlayResolver
from .storage import SubscriptionStore
from .tracker import OVERS, WICKET, Tracker

log = logging.getLogger("sportsbot")

PLAY_BY_PLAY_SPORTS = ("football", "baseball", "hockey")

# ESPN refreshes its data every 5-8 seconds, so checking more often than this
# wouldn't make updates any faster.
DEFAULT_POLL_SECONDS = 10
MIN_POLL_SECONDS = 5
# Older installers wrote POLL_INTERVAL=30 into the server's settings; treat that
# as "use the default" so those servers speed up too.
LEGACY_DEFAULT_POLL = "30"

TEAM_LIST_TTL = 6 * 3600


@dataclass
class LeagueHealth:
    checked_at: float | None = None
    live_games: int = 0
    error: str | None = None
    error_at: float | None = None


def code_version() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent), "log", "-1", "--format=%h (%cd)", "--date=short"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"

LEAGUE_CHOICES = [app_commands.Choice(name=l.name, value=l.key) for l in LEAGUES.values()]


class SportsBot(discord.Client):
    def __init__(self, store: SubscriptionStore, poll_interval: float, dev_guild: int | None):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = store
        self.espn = ESPNClient()
        self.tracker = Tracker()
        # NFL, MLB and NHL scores are posted as the actual scoring plays.
        self.play_resolvers = {
            league.key: PlayResolver(self._plays_fetcher(league))
            for league in LEAGUES.values()
            if league.sport in PLAY_BY_PLAY_SPORTS
        }
        # Soccer goals get their assister from the match details.
        self.play_resolvers.update(
            (league.key, AssistResolver(self._goals_fetcher(league)))
            for league in LEAGUES.values()
            if league.sport == "soccer"
        )
        # One ball-by-ball feed per cricket league (IPL, internationals).
        self.ball_feeds = {
            league.key: BallFeed(lambda path, event_id, page: self.espn.balls(path, event_id, page))
            for league in LEAGUES.values()
            if league.sport == "cricket"
        }
        self.dev_guild = dev_guild
        self.poll_interval = poll_interval
        self.poll.change_interval(seconds=poll_interval)
        self.health: dict[str, LeagueHealth] = {}
        self.version = code_version()
        self._team_lists: dict[str, tuple[float, list[tuple[str, str]]]] = {}

    async def team_list(self, key: str) -> list[tuple[str, str]]:
        """(name, abbreviation) of the league's teams, cached for a few hours."""
        cached = self._team_lists.get(key)
        if cached and time.monotonic() - cached[0] < TEAM_LIST_TTL:
            return cached[1]
        teams = await self.espn.teams(LEAGUES[key])
        self._team_lists[key] = (time.monotonic(), teams)
        return teams

    def _plays_fetcher(self, league):
        return lambda event_id: self.espn.scoring_plays(league, event_id)

    def _goals_fetcher(self, league):
        return lambda event_id: self.espn.goal_details(league, event_id)

    async def setup_hook(self) -> None:
        register_commands(self)
        if self.dev_guild:
            guild = discord.Object(id=self.dev_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.poll.start()

    async def close(self) -> None:
        self.poll.cancel()
        await self.espn.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    @tasks.loop(seconds=DEFAULT_POLL_SECONDS)
    async def poll(self) -> None:
        active = self.store.leagues()
        for key in list(LEAGUES):
            if key not in active:
                self.tracker.forget(key)
        await asyncio.gather(*(self._poll_league(key) for key in active if key in LEAGUES))

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def _poll_league(self, key: str) -> None:
        health = self.health.setdefault(key, LeagueHealth())
        try:
            games = await self.espn.scoreboard(LEAGUES[key])
        except Exception as exc:
            log.exception("Failed to fetch %s scoreboard", key)
            health.error, health.error_at = f"{type(exc).__name__}: {exc}"[:200], time.time()
            return
        health.checked_at, health.live_games = time.time(), sum(g.state == "in" for g in games)
        updates = self.tracker.update(key, games)
        resolver = self.play_resolvers.get(key)
        if resolver is not None:
            updates = await resolver.resolve(games, updates)
        subs = self.store.for_league(key)
        if LEAGUES[key].sport == "cricket":
            await self._post_balls(self.ball_feeds[key], games, [s for s in subs if s.ball_by_ball])
        for update in updates:
            channels = {
                s.channel_id
                for s in subs
                if (s.team is None or update.game.involves(s.team))
                # Ball-by-ball channels already see every wicket and over.
                and not (s.ball_by_ball and update.kind in (WICKET, OVERS))
            }
            if not channels:
                continue
            embed = update_embed(update)
            for channel_id in channels:
                await self._send(channel_id, embed)

    async def _post_balls(self, feed: BallFeed, games, subs) -> None:
        live = [g for g in games if g.state == "in"]
        feed.forget_except({g.id for g in live})
        for game in live:
            channels = {s.channel_id for s in subs if s.team is None or game.involves(s.team)}
            if not channels:
                continue
            balls = await feed.new_balls(game)
            for message in ball_messages(game, balls):
                for channel_id in channels:
                    await self._send(channel_id, content=message)

    async def _send(self, channel_id: int, embed: discord.Embed | None = None, content: str | None = None) -> None:
        channel = self.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            await channel.send(content=content, embed=embed)
        except discord.NotFound:
            log.warning("Channel %s no longer exists; dropping its subscriptions", channel_id)
            self.store.remove_channel(channel_id)
        except discord.HTTPException:
            log.exception("Failed to post update to channel %s", channel_id)


def _ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    secs = int(time.time() - ts)
    return f"{secs}s ago" if secs < 120 else f"{secs // 60} min ago"


def register_commands(bot: SportsBot) -> None:
    tree = bot.tree

    async def team_suggestions(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        key = getattr(interaction.namespace, "league", None)
        if key not in LEAGUES:
            return []
        try:
            # Discord drops suggestions that take longer than 3 seconds.
            teams = await asyncio.wait_for(bot.team_list(key), timeout=2.5)
        except Exception:
            log.warning("Couldn't load %s teams for suggestions", key, exc_info=True)
            return []
        q = current.strip().lower()
        matches = [n for n, a in teams if not q or q in n.lower() or q == a.lower()]
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in matches[:25]]

    async def followed_team_suggestions(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        key = getattr(interaction.namespace, "league", None)
        q = current.strip().lower()
        teams = [s.team for s in bot.store.for_channel(interaction.channel_id) if s.league == key and s.team]
        return [app_commands.Choice(name=t[:100], value=t[:100]) for t in teams if q in t][:25]

    @tree.command(name="scores", description="Show current scores for a league")
    @app_commands.describe(league="League to show", team="Only show games for this team (name or abbreviation)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def scores(interaction: discord.Interaction, league: app_commands.Choice[str], team: str | None = None):
        await interaction.response.defer(thinking=True)
        try:
            games = await bot.espn.scoreboard(LEAGUES[league.value])
        except Exception:
            log.exception("Failed to fetch %s scoreboard", league.value)
            await interaction.followup.send("Couldn't reach the score service, try again shortly.")
            return
        if team:
            games = [g for g in games if g.involves(team)]
        await interaction.followup.send(embed=scoreboard_embed(league.value, games, team))

    scores.autocomplete("team")(team_suggestions)

    @tree.command(name="follow", description="Post live updates for a league (or one team) in this channel")
    @app_commands.describe(
        league="League to follow",
        team="Only follow this team (name or abbreviation)",
        ball_by_ball="Cricket only: post every ball (about 240 messages a T20, 600 an ODI)",
    )
    @app_commands.choices(league=LEAGUE_CHOICES)
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def follow(
        interaction: discord.Interaction,
        league: app_commands.Choice[str],
        team: str | None = None,
        ball_by_ball: bool = False,
    ):
        if ball_by_ball and LEAGUES[league.value].sport != "cricket":
            await interaction.response.send_message("Ball-by-ball is only available for cricket.", ephemeral=True)
            return
        target = f"**{team}** in {league.name}" if team else f"all **{league.name}** games"
        if ball_by_ball:
            target += ", ball by ball"
        if bot.store.add(interaction.channel_id, league.value, team, ball_by_ball):
            msg = f"✅ This channel will now get live updates for {target}."
        else:
            msg = f"This channel already follows {target}."
        await interaction.response.send_message(msg)

    follow.autocomplete("team")(team_suggestions)

    @tree.command(name="unfollow", description="Stop live updates for a league (or one team) in this channel")
    @app_commands.describe(league="League to unfollow", team="The team you followed, if any")
    @app_commands.choices(league=LEAGUE_CHOICES)
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unfollow(interaction: discord.Interaction, league: app_commands.Choice[str], team: str | None = None):
        target = f"**{team}** in {league.name}" if team else f"all **{league.name}** games"
        if bot.store.remove(interaction.channel_id, league.value, team):
            msg = f"🛑 Stopped updates for {target}."
        else:
            msg = f"This channel wasn't following {target}. Use `/following` to see what it follows."
        await interaction.response.send_message(msg, ephemeral=True)

    unfollow.autocomplete("team")(followed_team_suggestions)

    @tree.command(name="status", description="Show whether the bot is checking scores and when it last succeeded")
    async def status(interaction: discord.Interaction):
        active = sorted(bot.store.leagues() & LEAGUES.keys(), key=list(LEAGUES).index)
        lines = []
        for key in active:
            h = bot.health.get(key, LeagueHealth())
            line = f"{LEAGUES[key].emoji} **{LEAGUES[key].name}**: checked {_ago(h.checked_at)}, {h.live_games} live"
            if h.error and (h.checked_at is None or (h.error_at or 0) > h.checked_at):
                line += f"\n  ⚠️ Last check failed {_ago(h.error_at)}: `{h.error}`"
            lines.append(line)
        embed = discord.Embed(
            title="ScoreBot status",
            description="\n".join(lines) or "No channel follows anything yet. Use `/follow`.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Version {bot.version} · checks every {bot.poll_interval:g}s")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="following", description="List what this channel is following")
    async def following(interaction: discord.Interaction):
        subs = bot.store.for_channel(interaction.channel_id)
        if not subs:
            await interaction.response.send_message(
                "This channel isn't following anything. Try `/follow`.", ephemeral=True
            )
            return
        lines = [
            f"{LEAGUES[s.league].emoji} {LEAGUES[s.league].name}"
            + (f" — {s.team}" if s.team else " — all games")
            + (" (ball by ball)" if s.ball_by_ball else "")
            for s in subs
            if s.league in LEAGUES
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


def poll_seconds(setting: str | None) -> float:
    """Seconds between checks from the POLL_INTERVAL setting."""
    setting = (setting or "").strip()
    if setting in ("", LEGACY_DEFAULT_POLL):
        return DEFAULT_POLL_SECONDS
    return max(float(setting), MIN_POLL_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable (see .env.example).")
    store = SubscriptionStore(os.environ.get("DATA_FILE", "subscriptions.json"))
    interval = poll_seconds(os.environ.get("POLL_INTERVAL"))
    dev_guild = os.environ.get("DEV_GUILD_ID")
    bot = SportsBot(store, interval, int(dev_guild) if dev_guild else None)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
