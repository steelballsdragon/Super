"""Discord bot entry point: slash commands plus the live-update loop."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import tasks

from .balls import BallFeed
from .espn import ESPNClient
from .espn import start_time
from .formatting import ball_messages, board_embed, reminder_text, schedule_embed, scoreboard_embed, update_embed
from .leagues import LEAGUES
from .odds import OddsBook, grade_text, line_text
from .research import LeanBook, leans, parse_research, picks_embed, record_embed, report_embed
from .schedule import COMMON_TIMEZONES, games_on, today
from .plays import AssistResolver, PlayResolver
from .settings import SettingsStore, StateStore
from .storage import SubscriptionStore
from .tracker import CALLED_OFF, FINAL, KICKOFF, OVERS, WICKET, Tracker

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

# Game threads: the start and the result go in the channel, everything else in
# the game's thread. Threads are forgotten a few days after they're made.
CHANNEL_KINDS = (KICKOFF, FINAL, CALLED_OFF)
THREAD_KEEP_SECONDS = 4 * 86400

REMINDER_SECONDS = 15 * 60
# The daily schedule goes out once a day, at the chosen hour or within the next
# two hours if the bot was restarting right then.
DAILY_WINDOW_HOURS = 2


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
    def __init__(
        self,
        store: SubscriptionStore,
        poll_interval: float,
        dev_guild: int | None,
        settings: SettingsStore | None = None,
        state: StateStore | None = None,
    ):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = store
        # Settings and state live next to the subscriptions file.
        self.settings = settings or SettingsStore(Path(store.path).with_name("settings.json"))
        self.state = state or StateStore(Path(store.path).with_name("state.json"))
        self.latest: dict[str, list] = {}  # latest games per league, for scoreboards
        self.odds = OddsBook(self.state)
        self.leans = LeanBook(self.state)
        self._boards_shown: dict[int, dict] = {}
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
        self._prune_threads()
        self._prune_reminders()
        self.odds.prune()
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
        # Leagues with research leans still to grade are checked even if no channel follows them.
        active = self.store.leagues() | self.leans.pending_leagues()
        for key in list(LEAGUES):
            if key not in active:
                self.tracker.forget(key)
        await asyncio.gather(*(self._poll_league(key) for key in active if key in LEAGUES))
        await self._refresh_boards()
        await self._send_reminders()
        await self._post_daily_schedules()

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
        self.latest[key] = games
        self.odds.remember(games)
        updates = self.tracker.update(key, games)
        resolver = self.play_resolvers.get(key)
        if resolver is not None:
            updates = await resolver.resolve(games, updates)
        subs = self.store.for_league(key)
        if LEAGUES[key].sport == "cricket":
            await self._post_balls(self.ball_feeds[key], games, [s for s in subs if s.ball_by_ball])
        for update in updates:
            if update.kind == FINAL:
                self.leans.settle(update.game)
            channels = {
                s.channel_id
                for s in subs
                if (s.team is None or update.game.involves(s.team))
                # Ball-by-ball channels already see every wicket and over.
                and not (s.ball_by_ball and update.kind in (WICKET, OVERS))
            }
            if not channels:
                continue
            plain = update_embed(update)
            with_odds = self._with_odds(update)
            for channel_id in channels:
                embed = with_odds if with_odds and self.settings.get(channel_id).odds else plain
                await self._deliver(channel_id, update.game, update.kind, embed=embed)

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
                    await self._deliver(channel_id, game, "ball", content=message)

    def _with_odds(self, update):
        """The update's embed plus the betting line (start) or how it settled (final)."""
        if update.kind not in (KICKOFF, FINAL):
            return None
        odds = self.odds.get(update.game)
        if odds is None:
            return None
        text = line_text(update.game, odds) if update.kind == KICKOFF else grade_text(update.game, odds)
        if not text:
            return None
        embed = update_embed(update)
        name = f"📊 Line ({odds.provider})" if update.kind == KICKOFF else f"📊 Bets ({odds.provider} closing line)"
        embed.add_field(name=name, value=text, inline=False)
        return embed

    async def _channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _send(self, channel_id: int, embed: discord.Embed | None = None, content: str | None = None):
        """Posts to a channel or thread; returns the message, or None if it failed."""
        try:
            channel = await self._channel(channel_id)
            return await channel.send(content=content, embed=embed)
        except discord.NotFound:
            log.warning("Channel %s no longer exists; dropping its subscriptions", channel_id)
            self.store.remove_channel(channel_id)
        except discord.HTTPException:
            log.exception("Failed to post update to channel %s", channel_id)
        return None

    # ----- game threads -----

    def _thread_key(self, channel_id: int, game) -> str:
        return f"{channel_id}:{game.league_key}:{game.id}"

    async def _deliver(self, channel_id: int, game, kind: str, embed=None, content=None) -> None:
        """Sends an update, into the game's thread when the channel uses threads."""
        if not self.settings.get(channel_id).threads:
            await self._send(channel_id, embed, content)
            return
        key = self._thread_key(channel_id, game)
        if kind in CHANNEL_KINDS:
            message = await self._send(channel_id, embed, content)
            if kind == KICKOFF and message is not None:
                await self._open_thread(key, game, message)
            elif kind != KICKOFF and (thread_id := self._thread_id(key)):
                await self._send_to_thread(thread_id, embed, content)  # keep the thread complete
                self.state.delete("threads", key)
            return
        thread_id = self._thread_id(key)
        if thread_id is None:
            # We didn't see the start (e.g. followed mid-game): post a header to hang the thread on.
            header = discord.Embed(description=f"🔴 **{game.scoreline()}**\nLive updates in the thread below.")
            message = await self._send(channel_id, header)
            thread_id = await self._open_thread(key, game, message) if message else None
        if thread_id is None or not await self._send_to_thread(thread_id, embed, content):
            await self._send(channel_id, embed, content)  # no thread permissions: fall back

    def _thread_id(self, key: str) -> int | None:
        entry = self.state.get("threads", key)
        return entry["id"] if entry else None

    async def _open_thread(self, key: str, game, message) -> int | None:
        a, b = game.teams
        name = f"{game.league.emoji} {a.abbrev} v {b.abbrev} · {game.league.name}"[:100]
        try:
            thread = await message.create_thread(name=name, auto_archive_duration=1440)
        except discord.HTTPException:
            log.warning("Couldn't create a thread for %s (missing Create Public Threads?)", key, exc_info=True)
            return None
        self.state.set("threads", key, {"id": thread.id, "at": time.time()})
        return thread.id

    async def _send_to_thread(self, thread_id: int, embed=None, content=None) -> bool:
        try:
            thread = await self._channel(thread_id)
            await thread.send(content=content, embed=embed)
            return True
        except discord.HTTPException:
            log.warning("Couldn't post in thread %s", thread_id, exc_info=True)
            return False

    def _prune_threads(self) -> None:
        cutoff = time.time() - THREAD_KEEP_SECONDS
        for key, entry in self.state.items("threads"):
            if entry.get("at", 0) < cutoff:
                self.state.delete("threads", key)

    # ----- betting research -----

    async def research(self, game):
        """The research report and leans for a game; pre-game leans are recorded for grading."""
        summary = await self.espn.summary(game.path or LEAGUES[game.league_key].path, game.id)
        r = parse_research(summary, game)
        found = leans(r) if game.state == "pre" else []
        self.leans.record(game, found)
        return r, found

    # ----- schedule and reminders -----

    def _followed_games(self, channel_id: int, key: str, games: list) -> list:
        teams = [s.team for s in self.store.for_channel(channel_id) if s.league == key]
        if not teams:
            return []
        return games if None in teams else [g for g in games if any(g.involves(t) for t in teams)]

    async def _send_reminders(self, now: float | None = None) -> None:
        now = now or time.time()
        for channel_id, settings in self.settings.channels_with(lambda s: s.reminders):
            for key, games in self.latest.items():
                for game in self._followed_games(channel_id, key, games):
                    start = start_time(game)
                    if game.state != "pre" or start is None or not 0 < start.timestamp() - now <= REMINDER_SECONDS:
                        continue
                    rkey = f"{channel_id}:{key}:{game.id}"
                    if self.state.get("reminded", rkey):
                        continue
                    self.state.set("reminded", rkey, now)
                    text = reminder_text(game)
                    odds = self.odds.get(game) if settings.odds else None
                    if odds and (line := line_text(game, odds)):
                        text += "\n" + line.replace("\n", " · ")
                    await self._send(channel_id, content=text)

    def _prune_reminders(self) -> None:
        cutoff = time.time() - 3 * 86400
        for key, at in self.state.items("reminded"):
            if at < cutoff:
                self.state.delete("reminded", key)

    def _zone(self, settings) -> ZoneInfo:
        try:
            return ZoneInfo(settings.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("America/Toronto")

    async def schedule_for(self, channel_id: int, tz: ZoneInfo):
        day = today(tz)
        keys = [k for k in LEAGUES if any(s.league == k for s in self.store.for_channel(channel_id))]
        sections = []
        for key in keys:
            try:
                games = await games_on(self.espn, key, day, tz)
            except Exception:
                log.exception("Couldn't load today's %s schedule", key)
                games = []
            sections.append((key, self._followed_games(channel_id, key, games)))
        return schedule_embed(f"{day:%a %b} {day.day}", sections)

    async def _post_daily_schedules(self, now: datetime | None = None) -> None:
        for channel_id, settings in self.settings.channels_with(lambda s: s.daily_hour is not None):
            tz = self._zone(settings)
            local = (now or datetime.now(timezone.utc)).astimezone(tz)
            if not settings.daily_hour <= local.hour < settings.daily_hour + DAILY_WINDOW_HOURS:
                continue
            if self.state.get("daily", str(channel_id)) == local.date().isoformat():
                continue
            self.state.set("daily", str(channel_id), local.date().isoformat())
            embed = await self.schedule_for(channel_id, tz)
            if embed is not None:  # nothing on today: stay quiet
                await self._send(channel_id, embed)

    # ----- live scoreboards -----

    def _board_sections(self, channel_id: int) -> list[tuple[str, list]]:
        sections = []
        subs = self.store.for_channel(channel_id)
        for key in [k for k in LEAGUES if any(s.league == k for s in subs)]:
            teams = [s.team for s in subs if s.league == key]
            games = self.latest.get(key, [])
            if None not in teams:
                games = [g for g in games if any(g.involves(t) for t in teams)]
            sections.append((key, games))
        return sections

    async def _refresh_boards(self) -> None:
        for channel_id, settings in self.settings.channels_with(lambda s: s.board_message_id):
            embed = board_embed(self._board_sections(channel_id))
            if self._boards_shown.get(channel_id) == embed.to_dict():
                continue  # nothing changed, so don't edit
            try:
                channel = await self._channel(channel_id)
                await channel.get_partial_message(settings.board_message_id).edit(embed=embed)
                self._boards_shown[channel_id] = embed.to_dict()
            except discord.NotFound:
                log.info("Scoreboard in channel %s was deleted; turning it off", channel_id)
                self.settings.update(channel_id, board_message_id=None)
            except discord.HTTPException:
                log.exception("Failed to update the scoreboard in channel %s", channel_id)


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

    @tree.command(name="scoreboard", description="Post a live scoreboard here that keeps itself up to date")
    @app_commands.describe(enabled="Turn the live scoreboard on (default) or off")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def scoreboard(interaction: discord.Interaction, enabled: bool = True):
        # Posting and pinning can take longer than Discord's 3-second reply window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = interaction.followup.send
        cid = interaction.channel_id
        old = bot.settings.get(cid).board_message_id
        if old:
            try:
                await (await bot._channel(cid)).get_partial_message(old).delete()
            except discord.HTTPException:
                pass  # already gone
            bot.settings.update(cid, board_message_id=None)
            bot._boards_shown.pop(cid, None)
        if not enabled:
            await reply("🛑 Live scoreboard removed.", ephemeral=True)
            return
        embed = board_embed(bot._board_sections(cid))
        message = await bot._send(cid, embed)
        if message is None:
            await reply("I couldn't post in this channel.", ephemeral=True)
            return
        bot.settings.update(cid, board_message_id=message.id)
        bot._boards_shown[cid] = embed.to_dict()
        note = ""
        try:
            await message.pin()
        except discord.HTTPException:
            note = " I couldn't pin it; give my role **Manage Messages** if you'd like it pinned."
        follows = "" if bot.store.for_channel(cid) else " This channel doesn't follow anything yet, so use `/follow` first."
        await reply(f"📺 Live scoreboard posted.{note}{follows}", ephemeral=True)

    @tree.command(name="threads", description="Put each game's updates in its own thread")
    @app_commands.describe(enabled="On: the start and result stay in the channel, everything else goes in the game's thread")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def threads(interaction: discord.Interaction, enabled: bool):
        bot.settings.update(interaction.channel_id, threads=enabled)
        if enabled:
            msg = ("🧵 Game threads on. Each game's start and result will post here, with everything in between in "
                   "the game's thread. My role needs **Create Public Threads** and **Send Messages in Threads**.")
        else:
            msg = "Game threads off. Updates will post straight in this channel."
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="odds", description="Show the betting line at each game's start and how bets settled at the final")
    @app_commands.describe(enabled="On (default) or off for this channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def odds(interaction: discord.Interaction, enabled: bool):
        bot.settings.update(interaction.channel_id, odds=enabled)
        msg = ("📊 Odds on: game starts show the line, and finals show how the spread, total and moneyline settled."
               if enabled else "Odds off for this channel.")
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="schedule", description="Today's games for everything this channel follows")
    async def schedule(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        embed = await bot.schedule_for(interaction.channel_id, bot._zone(bot.settings.get(interaction.channel_id)))
        if embed is None:
            await interaction.followup.send("No games today for what this channel follows.")
        else:
            await interaction.followup.send(embed=embed)

    @tree.command(name="daily", description="Post today's games here every morning")
    @app_commands.describe(
        enabled="Turn the daily schedule on or off",
        hour="Hour to post it, 0-23 (default 9)",
        timezone="Your time zone, e.g. America/Toronto (default)",
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def daily(
        interaction: discord.Interaction,
        enabled: bool,
        hour: app_commands.Range[int, 0, 23] = 9,
        timezone: str = "America/Toronto",
    ):
        if not enabled:
            bot.settings.update(interaction.channel_id, daily_hour=None)
            await interaction.response.send_message("Daily schedule off.", ephemeral=True)
            return
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            await interaction.response.send_message(
                f"I don't recognise the time zone `{timezone}`. Pick one from the list, e.g. `America/Toronto`.",
                ephemeral=True,
            )
            return
        bot.settings.update(interaction.channel_id, daily_hour=hour, timezone=timezone)
        await interaction.response.send_message(
            f"📅 Every day at {hour:02d}:00 ({timezone}) I'll post today's games for what this channel follows. "
            "Use `/schedule` to see today's now.",
            ephemeral=True,
        )

    @daily.autocomplete("timezone")
    async def timezone_suggestions(interaction: discord.Interaction, current: str):
        q = current.lower()
        return [app_commands.Choice(name=z, value=z) for z in COMMON_TIMEZONES if q in z.lower()][:25]

    @tree.command(name="reminders", description="Post a heads-up 15 minutes before each followed game")
    @app_commands.describe(enabled="Turn reminders on or off for this channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def reminders(interaction: discord.Interaction, enabled: bool):
        bot.settings.update(interaction.channel_id, reminders=enabled)
        msg = ("⏰ Reminders on: I'll post 15 minutes before each game this channel follows."
               if enabled else "Reminders off.")
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="update", description="Check GitHub for a new version of the bot right now")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def update(interaction: discord.Interaction):
        ok, detail = await trigger_update()
        if ok:
            msg = (f"🔄 Checking GitHub now (running {bot.version}). If there's a new version I'll restart with it "
                   "in about a minute; run `/status` afterwards to see the new version.")
        else:
            msg = ("I can't start an update from here yet. The server sets this up on its next automatic check "
                   f"(within the hour on older setups), so try again later.\n`{detail}`")
        await interaction.response.send_message(msg, ephemeral=True)

    research = app_commands.Group(name="research", description="Betting research from ESPN data, with sources")

    def _find_game(games, team):
        mine = [g for g in games if g.involves(team) and g.state != "post"]
        return min(mine, key=lambda g: (g.state != "in", g.start), default=None)

    async def _next_game(lg, team):
        """The team's live or next game: today's scoreboard, then up to a week ahead."""
        game = _find_game(await bot.espn.scoreboard(lg), team)
        if game is None and lg.feed == "scoreboard":
            from datetime import date, timedelta
            for ahead in range(1, 8):
                day = (date.today() + timedelta(days=ahead)).strftime("%Y%m%d")
                if (game := _find_game(await bot.espn.scoreboard(lg, day), team)) is not None:
                    break
        return game

    @research.command(name="game", description="Market, ESPN model, form, injuries and any leans for a team's next game")
    @app_commands.describe(league="League", team="Team (name or abbreviation)")
    @app_commands.choices(league=[c for c in LEAGUE_CHOICES if LEAGUES[c.value].sport != "cricket"])
    async def research_game(interaction: discord.Interaction, league: app_commands.Choice[str], team: str):
        await interaction.response.defer(thinking=True)
        try:
            game = await _next_game(LEAGUES[league.value], team)
            if game is None:
                await interaction.followup.send(f"No {league.name} game for **{team}** in the next week on ESPN.")
                return
            r, found = await bot.research(game)
        except Exception:
            log.exception("Research failed for %s %s", league.value, team)
            await interaction.followup.send("Couldn't load ESPN's data for that game, try again shortly.")
            return
        await interaction.followup.send(embed=report_embed(r, found))

    research_game.autocomplete("team")(team_suggestions)

    @research.command(name="picks", description="Today's games ranked by how strongly the data disagrees with the line")
    @app_commands.describe(league="League")
    @app_commands.choices(league=[c for c in LEAGUE_CHOICES if LEAGUES[c.value].sport != "cricket"])
    async def research_picks(interaction: discord.Interaction, league: app_commands.Choice[str]):
        await interaction.response.defer(thinking=True)
        lg = LEAGUES[league.value]
        try:
            games = [g for g in await bot.espn.scoreboard(lg) if g.state == "pre"]
        except Exception:
            await interaction.followup.send("Couldn't reach ESPN, try again shortly.")
            return
        limit = asyncio.Semaphore(5)

        async def one(g):
            async with limit:
                try:
                    return await bot.research(g)
                except Exception:
                    log.warning("Research failed for %s", g.id, exc_info=True)
                    return None
        reports = [x for x in await asyncio.gather(*(one(g) for g in games[:20])) if x]
        if not reports:
            await interaction.followup.send(f"No upcoming {lg.name} games with lines on ESPN right now.")
            return
        await interaction.followup.send(embed=picks_embed(lg.name, lg.emoji, reports))

    @research.command(name="record", description="How the research leans have done so far")
    async def research_record(interaction: discord.Interaction):
        await interaction.response.send_message(embed=record_embed(bot.leans.summary(), bot.leans.pending()))

    tree.add_command(research)

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


UPDATE_COMMAND = ("sudo", "-n", "systemctl", "start", "--no-block", "scorebot-update.service")


async def trigger_update() -> tuple[bool, str]:
    """Asks the server to check for updates now (allowed by deploy/system-setup.sh)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *UPDATE_COMMAND, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (OSError, asyncio.TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
    return proc.returncode == 0, out.decode(errors="replace").strip()[:300]


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
