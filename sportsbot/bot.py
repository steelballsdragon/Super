"""Discord bot entry point: slash commands plus the live-update loop."""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks

from .espn import ESPNClient
from .formatting import scoreboard_embed, update_embed
from .leagues import LEAGUES
from .plays import PlayResolver
from .storage import SubscriptionStore
from .tracker import Tracker

log = logging.getLogger("sportsbot")

PLAY_BY_PLAY_SPORTS = ("football", "baseball", "hockey")

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
        self.dev_guild = dev_guild
        self.poll.change_interval(seconds=poll_interval)

    def _plays_fetcher(self, league):
        return lambda event_id: self.espn.scoring_plays(league, event_id)

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

    @tasks.loop(seconds=30)
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
        try:
            games = await self.espn.scoreboard(LEAGUES[key])
        except Exception:
            log.exception("Failed to fetch %s scoreboard", key)
            return
        updates = self.tracker.update(key, games)
        resolver = self.play_resolvers.get(key)
        if resolver is not None:
            updates = await resolver.resolve(games, updates)
        if not updates:
            return
        subs = self.store.for_league(key)
        for update in updates:
            channels = {
                s.channel_id
                for s in subs
                if s.team is None or update.game.involves(s.team)
            }
            if not channels:
                continue
            embed = update_embed(update)
            for channel_id in channels:
                await self._send(channel_id, embed)

    async def _send(self, channel_id: int, embed: discord.Embed) -> None:
        channel = self.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            await channel.send(embed=embed)
        except discord.NotFound:
            log.warning("Channel %s no longer exists; dropping its subscriptions", channel_id)
            self.store.remove_channel(channel_id)
        except discord.HTTPException:
            log.exception("Failed to post update to channel %s", channel_id)


def register_commands(bot: SportsBot) -> None:
    tree = bot.tree

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

    @tree.command(name="follow", description="Post live updates for a league (or one team) in this channel")
    @app_commands.describe(league="League to follow", team="Only follow this team (name or abbreviation)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def follow(interaction: discord.Interaction, league: app_commands.Choice[str], team: str | None = None):
        target = f"**{team}** in {league.name}" if team else f"all **{league.name}** games"
        if bot.store.add(interaction.channel_id, league.value, team):
            msg = f"✅ This channel will now get live updates for {target}."
        else:
            msg = f"This channel already follows {target}."
        await interaction.response.send_message(msg)

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

    @tree.command(name="following", description="List what this channel is following")
    async def following(interaction: discord.Interaction):
        subs = bot.store.for_channel(interaction.channel_id)
        if not subs:
            await interaction.response.send_message(
                "This channel isn't following anything. Try `/follow`.", ephemeral=True
            )
            return
        lines = [
            f"{LEAGUES[s.league].emoji} {LEAGUES[s.league].name}" + (f" — {s.team}" if s.team else " — all games")
            for s in subs
            if s.league in LEAGUES
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable (see .env.example).")
    store = SubscriptionStore(os.environ.get("DATA_FILE", "subscriptions.json"))
    interval = float(os.environ.get("POLL_INTERVAL", "30"))
    dev_guild = os.environ.get("DEV_GUILD_ID")
    bot = SportsBot(store, interval, int(dev_guild) if dev_guild else None)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
