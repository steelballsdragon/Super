"""Discord bot entry point: slash commands plus the live-update loop."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import tasks

from .balls import BallFeed
from .bankroll import STYLES, BetBook, PlacedButton, bankroll_embed, money, placed_view, units_for
from .espn import EASTERN, ESPNClient
from .espn import start_time
from .formatting import ball_messages, board_embed, reminder_text, schedule_embed, scoreboard_embed, update_embed
from .leagues import LEAGUES
from .limits import MESSAGE, clip, fit_embed
from .odds import OddsBook, grade_text, line_text
from .cricket_props import CricketHistory
from .parlays import ParlayBook, record_field, settle
from .props import (LONGSHOTS, MAX_LEGS, MAX_LEGS_PER_GAME, TARGETS, PropsClient, apply_matchup, build_to_target,
                    chance_at_least, combined, availability, expected_goals, moneyline_leg, parlay_embed, pick_round_robin, round_robin_embed,
                    scorer_lines, trend_legs, trends_embed)
from .research import LeanBook, leans, market_chances, parse_research, picks_embed, record_embed, report_embed
from .schedule import COMMON_TIMEZONES, games_on, today
from .plays import AssistResolver, PlayResolver
from .settings import SettingsStore, StateStore, move_sections
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
WEEK_CACHE_SECONDS = 600
GAME_INFO_SECONDS = 120
PARLAY_GAMES = 8  # parlays look ahead (up to a week) until there are at least this many games to build from
SETTLE_SECONDS = 60  # how often finished parlay legs are graded
PLAY_POST_SECONDS = 20 * 60  # scoring play posts are remembered this long, to edit when ESPN fills them in
LINEUP_CHECK_SECONDS = 75 * 60  # open slips' players are checked against lineups and injuries this long before kickoff
LINEUP_SPORTS = ("soccer", "baseball")  # sports whose starting lineups ESPN has before the game
PRUNE_SECONDS = 3600  # how often old state entries are cleaned out
LOOP_RESTART_SECONDS = 5
# The daily schedule goes out once a day, at the chosen hour or within the next
# two hours if the bot was restarting right then.
DAILY_WINDOW_HOURS = 2


@dataclass
class LeagueHealth:
    checked_at: float | None = None
    live_games: int = 0
    error: str | None = None
    error_at: float | None = None


def release_memory() -> None:
    """Hands memory freed after a big job (e.g. a parlay over a week of games) back to the server.
    Without it Python keeps the peak's memory reserved; the server has only 1 GB."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # not Linux/glibc: nothing to do


def code_version() -> str:
    repo = str(Path(__file__).resolve().parent.parent)
    try:
        # The code is owned by root on the server, so git needs to be told it's safe to read.
        out = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", repo, "log", "-1", "--format=%h (%cd)", "--date=short"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"

TeamName = app_commands.Range[str, 1, 100]  # Discord would otherwise accept up to 6000 characters
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
        # The betting record (every lean and parlay, kept for /record) grows over months, so it has its own
        # file: state.json is saved far more often and stays small.
        self.records = StateStore(self.state.path.with_name("record.json"))
        move_sections(self.state, self.records, ("leans", "parlays"))
        self.leans = LeanBook(self.records)
        self._boards_shown: dict[int, dict] = {}
        self.espn = ESPNClient()
        self.props = PropsClient(self.espn)
        # Recorded cricket scorecards get their own file: they're big, and state.json is rewritten often.
        self.cricket = CricketHistory(self.props, StateStore(self.state.path.with_name("cricket.json")))
        self.parlays = ParlayBook(self.records)
        self.bets = BetBook(self.records)
        self._tasks: set[asyncio.Task] = set()
        self._last_prune = time.monotonic()
        self._last_settle = 0.0
        self._cleaned_guilds = False
        self.result_lookups: dict[str, float] = {}
        self._week: dict[str, tuple[float, list]] = {}  # league -> (fetched at, the week's upcoming games)  # when to next ask ESPN how an off-scoreboard game ended
        self.tracker = Tracker()
        # NFL, MLB and NHL scores are posted as the actual scoring plays.
        self.play_posts: dict[tuple[str, str], list] = {}  # (game, play) -> [(when, channel, message)], for edits
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
        # One ball-by-ball feed per cricket league (IPL, internationals); where each
        # match is up to is saved in its own small file so restarts carry on from it.
        self.ball_state = StateStore(self.state.path.with_name("balls.json"))
        self.ball_feeds = {
            league.key: BallFeed(lambda path, event_id, page: self.espn.balls(path, event_id, page),
                                 self.ball_state, league.key)
            for league in LEAGUES.values()
            if league.sport == "cricket"
        }
        self.dev_guild = dev_guild
        self.poll_interval = poll_interval
        self.poll.change_interval(seconds=poll_interval)
        self.health: dict[str, LeagueHealth] = {}
        self.version = code_version()
        self._team_lists: dict[str, tuple[float, list[tuple[str, str]]]] = {}

    async def week_games(self, key: str) -> list:
        """The league's games that haven't started, over the next week, soonest first (cached for 10 minutes,
        so game suggestions answer within Discord's 3 seconds)."""
        cached = self._week.get(key)
        if cached and time.monotonic() - cached[0] < WEEK_CACHE_SECONDS:
            return cached[1]
        league = LEAGUES[key]
        boards = [self.espn.scoreboard(league)]
        if league.feed == "scoreboard":
            today = datetime.now(EASTERN).date()
            boards += [self.espn.scoreboard(league, f"{today + timedelta(days=d):%Y%m%d}") for d in range(8)]
        games = {}
        for found in await asyncio.gather(*boards, return_exceptions=True):
            if isinstance(found, BaseException):
                continue
            games.update((g.id, g) for g in found if g.state == "pre")
        week = sorted(games.values(), key=lambda g: g.start)
        self._week[key] = (time.monotonic(), week)
        return week

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
        self._prune_state()
        register_commands(self)
        self.add_dynamic_items(PlacedButton)  # "I placed it" buttons on slips keep working after restarts
        self.poll.start()
        try:
            if self.dev_guild:
                guild = discord.Object(id=self.dev_guild)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException:
            # Commands registered by an earlier start keep working; live updates carry on regardless.
            log.exception("Couldn't register the slash commands with Discord")

    async def close(self) -> None:
        self.poll.cancel()
        await self.espn.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        if not self._cleaned_guilds:
            self._cleaned_guilds = True
            await self._remove_old_server_commands()

    async def _remove_old_server_commands(self) -> None:
        """Deletes slash commands an older version registered to individual servers.

        The current commands are global, so any server-only copies are outdated
        duplicates (e.g. the old /research subcommands) that would otherwise stay listed.
        """
        for guild in self.guilds:
            if guild.id == self.dev_guild:
                continue  # that server's commands are the current ones on purpose
            try:
                if await self.tree.fetch_commands(guild=guild):
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    log.info("Removed outdated commands from server %s", guild.id)
            except discord.HTTPException:
                log.warning("Couldn't check server %s for outdated commands", guild.id, exc_info=True)

    @tasks.loop(seconds=DEFAULT_POLL_SECONDS)
    async def poll(self) -> None:
        try:
            await self._poll_once()
        except Exception:
            log.exception("Update cycle failed; trying again next cycle")

    async def _poll_once(self) -> None:
        # One save of the state at the end of the cycle, not one per change (dozens when lines move).
        with self.state.batch(), self.records.batch(), self.ball_state.batch():
            await self._poll_cycle()

    async def _poll_cycle(self) -> None:
        # Leagues with research leans or parlay legs still to grade are checked even if no channel follows them.
        active = (self.store.leagues() | self.leans.pending_leagues() | self.parlays.pending_leagues()) & LEAGUES.keys()
        for key in list(LEAGUES):
            if key not in active:
                self.tracker.forget(key)
                self.latest.pop(key, None)
        # Every league and every step is isolated: one failure is logged, never fatal to the loop.
        await asyncio.gather(*(self._guarded(f"{key} update", self._poll_league(key)) for key in active))
        await self._guarded("scoreboards", self._refresh_boards())
        if time.monotonic() - self._last_settle >= SETTLE_SECONDS:
            self._last_settle = time.monotonic()
            await self._guarded("grading parlays", settle(self, self.parlays))
            await self._guarded("grading leans", self.leans.settle_pending(self))
        await self._guarded("reminders", self._send_reminders())
        await self._guarded("lineup checks", self._check_lineups())
        await self._guarded("daily schedules", self._post_daily_schedules())
        if time.monotonic() - self._last_prune >= PRUNE_SECONDS:
            self._last_prune = time.monotonic()
            self._prune_state()

    def _prune_state(self) -> None:
        """Drops old entries so state.json stays small however long the bot runs."""
        release_memory()
        self._prune_threads()
        self._prune_reminders()
        self._prune_lineup_checks()
        self._prune_play_posts()
        self.odds.prune()
        now = time.time()
        self.result_lookups = {k: t for k, t in self.result_lookups.items() if t > now}
        cutoff = now - 14 * 86400
        for key, at in self.state.items("finals"):
            if at < cutoff:
                self.state.delete("finals", key)

    def _background(self, coro) -> None:
        """Runs a job alongside the loop, keeping a reference so Python doesn't drop it midway."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, what: str, step) -> None:
        try:
            await step
        except Exception:
            log.exception("%s failed; carrying on", what)

    @poll.error
    async def _poll_error(self, error: BaseException) -> None:
        # Last resort: if anything still escapes, restart the loop rather than stop posting for good.
        # The handler runs before the loop has fully stopped, so the restart is scheduled for after.
        log.error("Update loop crashed; restarting it in %ss", LOOP_RESTART_SECONDS, exc_info=error)
        asyncio.get_running_loop().call_later(LOOP_RESTART_SECONDS, self._restart_poll)

    def _restart_poll(self) -> None:
        if not self.poll.is_running() and not self.is_closed():
            self.poll.start()

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
        games = await self._with_vanished_games(LEAGUES[key], games)
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
                if update.game.league.feed == "scorepanel":
                    # ESPN has no international cricket history: keep our own as matches finish.
                    self._background(self._guarded("recording a cricket scorecard", self.cricket.record(update.game)))
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
            if update.edit:  # ESPN filled in or corrected a play already posted: edit those posts
                await self._edit_play(update, plain, with_odds)
                continue
            for channel_id in channels:
                embed = with_odds if with_odds and self.settings.get(channel_id).odds else plain
                message = await self._deliver(channel_id, update.game, update.kind, embed=embed)
                if update.play is not None and message is not None:
                    self.play_posts.setdefault((update.game.id, update.play.id), []).append(
                        (time.time(), channel_id, message))

    async def _edit_play(self, update, plain, with_odds) -> None:
        for _, channel_id, message in self.play_posts.get((update.game.id, update.play.id), []):
            embed = with_odds if with_odds and self.settings.get(channel_id).odds else plain
            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                log.warning("Couldn't edit the post for play %s", update.play.id, exc_info=True)

    def _prune_play_posts(self) -> None:
        cutoff = time.time() - PLAY_POST_SECONDS
        self.play_posts = {k: [p for p in posts if p[0] > cutoff] for k, posts in self.play_posts.items()}
        self.play_posts = {k: posts for k, posts in self.play_posts.items() if posts}

    async def _with_vanished_games(self, league, games: list) -> list:
        """Keeps following a live game that dropped off ESPN's scoreboard before it ended (ESPN moved
        on to the next day, or a glitchy response) by fetching its own day, so its result still posts."""
        if league.feed != "scoreboard":
            return games
        ids = {g.id for g in games}
        gone = {g.id: g for g in self.tracker.games(league.key) if g.state == "in" and g.id not in ids}
        days = {start.astimezone(EASTERN).strftime("%Y%m%d") for g in gone.values() if (start := start_time(g))}
        for day in sorted(days):
            try:
                found = await self.espn.scoreboard(league, day)
            except Exception:
                log.warning("Couldn't look up %s games from %s that left the scoreboard", league.key, day, exc_info=True)
                continue
            games = games + [g for g in found if g.id in gone and g.id not in ids]
        return games

    async def _post_balls(self, feed: BallFeed, games, subs) -> None:
        wanted = []
        for game in (g for g in games if g.state == "in"):
            if channels := {s.channel_id for s in subs if s.team is None or game.involves(s.team)}:
                wanted.append((game, channels))
        # Matches nobody follows ball by ball are dropped, so following again starts from the current ball.
        feed.forget_except({g.id for g, _ in wanted})
        for game, channels in wanted:
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

    async def _deliver(self, channel_id: int, game, kind: str, embed=None, content=None):
        """Sends an update, into the game's thread when the channel uses threads. Returns the message
        (the one in the game's thread, if it went there), or None if it couldn't be posted."""
        if not self.settings.get(channel_id).threads:
            return await self._send(channel_id, embed, content)
        key = self._thread_key(channel_id, game)
        if kind in CHANNEL_KINDS:
            message = await self._send(channel_id, embed, content)
            if kind == KICKOFF and message is not None:
                await self._open_thread(key, game, message)
            elif kind != KICKOFF and (thread_id := self._thread_id(key)):
                await self._send_to_thread(thread_id, embed, content)  # keep the thread complete
                self.state.delete("threads", key)
            return message
        thread_id = self._thread_id(key)
        if thread_id is None:
            # We didn't see the start (e.g. followed mid-game): post a header to hang the thread on.
            header = discord.Embed(description=f"🔴 **{game.scoreline()}**\nLive updates in the thread below.")
            message = await self._send(channel_id, header)
            thread_id = await self._open_thread(key, game, message) if message else None
        if thread_id is not None and (message := await self._send_to_thread(thread_id, embed, content)):
            return message
        return await self._send(channel_id, embed, content)  # no thread permissions: fall back

    def _thread_id(self, key: str) -> int | None:
        entry = self.state.get("threads", key)
        return entry["id"] if entry else None

    async def _open_thread(self, key: str, game, message) -> int | None:
        a, b = game.teams
        name = f"{game.league.emoji} {a.name} v {b.name} · {game.league.name}"[:100]
        try:
            thread = await message.create_thread(name=name, auto_archive_duration=1440)
        except discord.HTTPException:
            log.warning("Couldn't create a thread for %s (missing Create Public Threads?)", key, exc_info=True)
            return None
        self.state.set("threads", key, {"id": thread.id, "at": time.time()})
        return thread.id

    async def _send_to_thread(self, thread_id: int, embed=None, content=None):
        """Posts in a thread; returns the message, or None if it failed."""
        try:
            thread = await self._channel(thread_id)
            return await thread.send(content=content, embed=embed)
        except discord.HTTPException:
            log.warning("Couldn't post in thread %s", thread_id, exc_info=True)
            return None

    def _prune_threads(self) -> None:
        cutoff = time.time() - THREAD_KEEP_SECONDS
        for key, entry in self.state.items("threads"):
            if entry.get("at", 0) < cutoff:
                self.state.delete("threads", key)

    # ----- betting research -----

    async def longshots(self, game, kind: str, payout: str | None = None):
        """The round robin's long shots in this game (e.g. full-backs to assist, role players' 3+ threes),
        within the chances for the payout picked (Lotto: the +450 to +1500 kind)."""
        shot = LONGSHOTS[kind]
        low, high = shot.band(payout)
        r, available = await self._game_info(game)
        # A little either side of the range at first: the matchup can move players in or out of it.
        trends = await self.props.longshot_trends(game, shot, available, (low, high))
        if game.league.sport in ("soccer", "hockey"):  # assists come and go with the team's goals
            matchup = expected_goals(game, market_chances(r), r.odds.total if r.odds else None, r.form)
            trends = apply_matchup(trends, game, matchup, bar=0.0)
        return [t for t in trends if low <= t.probability <= high]

    async def game_props(self, game, bigger: bool = False, underdog: bool = False, scorers: bool = False):
        """Player trends and a moneyline leg (the favorite's, or with underdog=True the underdog's) for one game.
        With scorers=True, the trends are goalscorer and assist bets only."""
        r, available = await self._game_info(game)
        if game.league.sport == "cricket":
            trends = await self.cricket.trends(game, bigger)
        else:
            trends = await self.props.game_trends(game, available, bigger, scorers)
            if scorers:  # who's likely to score depends on the matchup, not just the player's record
                matchup = expected_goals(game, market_chances(r), r.odds.total if r.odds else None, r.form)
                trends = apply_matchup(trends, game, matchup)
        return trends, moneyline_leg(game, market_chances(r), r.odds, underdog)

    async def _game_info(self, game):
        """A game's ESPN summary, boiled down to the research data and who can play (injuries, and the starting
        lineups once they're out). Kept for a couple of minutes: one parlay or report looks at each game several
        times (normal, longer and goalscorer lines), and lineups drop about an hour before kickoff."""
        async def fetch():
            summary = await self.espn.summary(game.path or LEAGUES[game.league_key].path, game.id)
            return parse_research(summary, game), availability(summary)
        return await self.props.cached(f"game-info:{game.league_key}:{game.id}", GAME_INFO_SECONDS, fetch)

    async def _availability(self, league_key: str, game_id: str, path: str = ""):
        """Who can play in a game, for checking a slip's players before kickoff."""
        async def fetch():
            return availability(await self.espn.summary(path or LEAGUES[league_key].path, game_id))
        return await self.props.cached(f"available:{league_key}:{game_id}", GAME_INFO_SECONDS, fetch)

    async def _check_lineups(self, now: float | None = None) -> None:
        """In the last LINEUP_CHECK_SECONDS before kickoff, checks every open slip's players: as each team's
        lineup comes out (soccer about an hour before, MLB batting orders), says which of its players start;
        in any sport, says if a player is ruled out on the injury report. Each thing is said once."""
        now = now or time.time()
        for parlay in self.parlays.pending():
            games: dict[tuple, list[dict]] = {}
            for leg in parlay["legs"]:
                start = _iso_seconds(leg.get("start", ""))
                if (leg["status"] == "pending" and leg.get("player_id") and start is not None
                        and 0 < start - now <= LINEUP_CHECK_SECONDS):
                    games.setdefault((leg["league"], leg["game_id"], leg.get("path", "")), []).append(leg)
            for (league, gid, path), legs in games.items():
                start = _iso_seconds(legs[0]["start"])
                key = f"{parlay['id']}:{gid}"
                done = self.records.get("lineup_checks", key) or {"at": now, "said": []}
                try:
                    available = await self._availability(league, gid, path)
                except Exception:
                    log.warning("Couldn't check lineups for %s %s", league, gid, exc_info=True)
                    continue
                said = list(done["said"])
                news, ruled_out = [], []
                for leg in legs:
                    team = leg.get("team", "")
                    status = available.status(leg["player_id"], leg.get("player", ""), team)
                    if available.announced(team):
                        if f"lineups:{team}" not in done["said"]:  # this team's lineup just came out
                            news.append((leg, status))
                            said.append(f"lineups:{team}")
                    elif status == "injured" and f"injured:{leg['player_id']}" not in said:
                        ruled_out.append((leg, status))
                        said.append(f"injured:{leg['player_id']}")
                said = list(dict.fromkeys(said))
                lines = [lineup_line(leg, status) for leg, status in news + ruled_out]
                when = f"{legs[0]['game']} (kickoff in {_minutes(start - now)})"
                if news:
                    problems = sum(status != "starts" for _, status in news + ruled_out)
                    head = (f"📋 **Lineups are out** for {when}: "
                            + ("they all start ✅" if not problems else
                               f"**{problems} of your picks {'is' if problems == 1 else 'are'} not starting**: "
                               "swap or drop them before kickoff"))
                else:
                    head = f"🩹 **Pick ruled out** for {when}"
                if not lines:
                    continue
                self.records.set("lineup_checks", key, {"at": done["at"], "said": said})
                bettors = sorted({b["user"] for b in self.bets.bets() if b["parlay"] == parlay["id"]})
                text = "\n".join([f"{head} · your {parlay['style']} slip:", *lines]
                                  + ([" ".join(f"<@{u}>" for u in bettors)] if bettors else []))
                await self._send(parlay["channel"], content=clip(text, MESSAGE))

    def _prune_lineup_checks(self) -> None:
        cutoff = time.time() - 3 * 86400
        for key, entry in self.records.items("lineup_checks"):
            if entry.get("at", 0) < cutoff:
                self.records.delete("lineup_checks", key)

    async def research(self, game):
        """The research report and leans for a game; pre-game leans are recorded for grading."""
        r, _ = await self._game_info(game)
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


def _iso_seconds(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min"


LINEUP_WORDS = {
    "starts": "✅ {pick}: starts",
    "bench": "🪑 {pick}: **on the bench**",
    "out": "❌ {pick}: **not in the squad**",
    "injured": "❌ {pick}: **ruled out** (injury report)",
    "unknown": "❔ {pick}: not in the lineup ESPN has",
}


def lineup_line(leg: dict, status: str) -> str:
    return LINEUP_WORDS[status].format(pick=leg["pick"])


def _ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    secs = int(time.time() - ts)
    return f"{secs}s ago" if secs < 120 else f"{secs // 60} min ago"


_STILL_LOADING: set = set()


def _finished_loading(task) -> None:
    _STILL_LOADING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("Loading suggestions failed: %r", task.exception())


async def gather_within(seconds: float, *jobs) -> list:
    """Results of the jobs that finish within `seconds` (Discord drops suggestions after 3). The rest keep
    running in the background rather than being cancelled, so what they load is cached for the next try."""
    tasks = [asyncio.ensure_future(job) for job in jobs]
    for task in tasks:
        _STILL_LOADING.add(task)  # held until done, so Python doesn't drop it part-way
        task.add_done_callback(_finished_loading)
    done, _ = await asyncio.wait(tasks, timeout=seconds)
    return [t.result() for t in tasks if t in done and not t.cancelled() and t.exception() is None]


def register_commands(bot: SportsBot) -> None:
    tree = bot.tree

    def channel_leagues(channel_id: int) -> list[str]:
        """The leagues this channel follows, in the usual league order."""
        followed = {s.league for s in bot.store.for_channel(channel_id)}
        return [k for k in LEAGUES if k in followed]

    def team_matches(teams, team: str) -> bool:
        q = team.strip().lower()
        return any(q == a.lower() or q in n.lower() for n, a in teams)

    async def pick_league(interaction: discord.Interaction, league, team: str | None = None, game: str | None = None):
        """The league asked for or, when left out, the one this channel follows. With several followed,
        the team or picked game decides (e.g. "Chiefs" in a channel following NFL and MLB). Returns (key, problem)."""
        if league is not None:
            return league.value, None
        keys = channel_leagues(interaction.channel_id)
        if len(keys) == 1:
            return keys[0], None
        if not keys:
            return None, "Pick a league: this channel doesn't follow one yet (or `/follow` one to skip this next time)."
        if game:  # a game picked from the suggestions: the league whose week has it
            for key in keys:
                try:
                    if any(g.id == game for g in await bot.week_games(key)):
                        return key, None
                except Exception:
                    log.warning("Couldn't load %s games", key, exc_info=True)
        if team:
            found = []
            for key in keys:
                try:
                    if team_matches(await bot.team_list(key), team):
                        found.append(key)
                except Exception:
                    log.warning("Couldn't load %s teams", key, exc_info=True)
            if len(found) == 1:
                return found[0], None
        names = ", ".join(LEAGUES[k].name for k in keys)
        return None, f"This channel follows {names}. Pick the league too."

    async def team_suggestions(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        key = getattr(interaction.namespace, "league", None)
        keys = [key] if key in LEAGUES else channel_leagues(interaction.channel_id)
        if not keys:
            return []
        lists = await gather_within(2.5, *(bot.team_list(k) for k in keys))
        teams = list(dict.fromkeys(t for found in lists for t in found))
        q = current.strip().lower()
        matches = [n for n, a in teams if not q or q in n.lower() or q == a.lower()]
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in matches[:25]]

    async def followed_team_suggestions(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        key = getattr(interaction.namespace, "league", None)
        q = current.strip().lower()
        teams = [s.team for s in bot.store.for_channel(interaction.channel_id) if (key is None or s.league == key) and s.team]
        return [app_commands.Choice(name=t[:100], value=t[:100]) for t in teams if q in t][:25]

    @tree.command(name="scores", description="Show current scores for a league")
    @app_commands.describe(league="League to show (leave out for the ones this channel follows)",
                           team="Only show games for this team (name or abbreviation)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def scores(interaction: discord.Interaction, league: app_commands.Choice[str] | None = None,
                     team: TeamName | None = None):
        await interaction.response.defer(thinking=True)
        key, problem = await pick_league(interaction, league, team)
        if key is not None:
            keys = [key]
        elif league is None and not team and (keys := channel_leagues(interaction.channel_id)):
            pass  # every league this channel follows, one scoreboard each
        else:
            await interaction.followup.send(problem)
            return
        for key in keys:
            try:
                games = await bot.espn.scoreboard(LEAGUES[key])
            except Exception:
                log.exception("Failed to fetch %s scoreboard", key)
                await interaction.followup.send(f"Couldn't reach the score service for {LEAGUES[key].name}, try again shortly.")
                continue
            if team:
                games = [g for g in games if g.involves(team)]
            await interaction.followup.send(embed=scoreboard_embed(key, games, team))

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
        team: TeamName | None = None,
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
    @app_commands.describe(league="League to unfollow (leave out if this channel follows just one)",
                           team="The team you followed, if any")
    @app_commands.choices(league=LEAGUE_CHOICES)
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unfollow(interaction: discord.Interaction, league: app_commands.Choice[str] | None = None,
                       team: TeamName | None = None):
        key = league.value if league else None
        if key is None:
            # A followed team decides the league; otherwise the channel's only league.
            leagues = {s.league for s in bot.store.for_channel(interaction.channel_id)
                       if team and s.team and s.team == team.strip().lower()} or set(channel_leagues(interaction.channel_id))
            if len(leagues) != 1:
                names = ", ".join(LEAGUES[k].name for k in channel_leagues(interaction.channel_id))
                msg = f"This channel follows {names}. Pick the league too." if names else "This channel isn't following anything."
                await interaction.response.send_message(msg, ephemeral=True)
                return
            key = leagues.pop()
        name = LEAGUES[key].name
        target = f"**{team}** in {name}" if team else f"all **{name}** games"
        if bot.store.remove(interaction.channel_id, key, team):
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

    def _find_game(games, team):
        mine = [g for g in games if g.involves(team) and g.state != "post"]
        return min(mine, key=lambda g: (g.state != "in", g.start), default=None)

    async def _week(lg) -> list:
        """The week's games that haven't started yet (from the cached week, so started ones drop out)."""
        now = time.time()
        return [g for g in await bot.week_games(lg.key) if (st := start_time(g)) is None or st.timestamp() > now]

    async def _upcoming(lg, want: int = 1):
        """Games that haven't started: the soonest day's, then the following days' until there are at least
        `want` (soccer can have a single midweek game before a full weekend), soonest first."""
        games, day = [], None
        for g in await _week(lg):
            start = start_time(g)
            g_day = start.astimezone(EASTERN).date() if start else None
            if len(games) >= want and g_day != day:
                break  # enough games, and this one starts a new day
            games.append(g)
            day = g_day
        return games[:16]

    async def _next_game(lg, team):
        """The team's live or next game: today's scoreboard (live games first), then the rest of the week."""
        return _find_game(await bot.espn.scoreboard(lg), team) or _find_game(await _week(lg), team)

    def _when(game) -> str:
        start = start_time(game)
        return f"{start.astimezone(EASTERN):%a %b} {start.astimezone(EASTERN).day}" if start else ""

    async def _find_picked_game(lg, picked: str):
        """The game picked from the suggestions (its ESPN id), or one typed by team name."""
        week = await bot.week_games(lg.key)
        game = next((g for g in week if g.id == picked), None)
        if game is None:  # typed rather than picked: "Chelsea" or "Bournemouth @ Chelsea"
            names = [part.strip() for part in picked.replace(" vs ", " @ ").split(" @ ") if part.strip()]
            game = next((g for g in week if names and all(g.involves(n) for n in names)), None)
        return game

    async def _report(interaction, game):
        """Everything on one game: market, model, form, injuries, leans and player trends."""
        if game.league.sport != "cricket":  # ESPN has no lines or model for cricket
            r, found = await bot.research(game)
            await interaction.followup.send(embed=report_embed(r, found))
        trends, ml = await bot.game_props(game)
        embed = trends_embed(game, trends, ml)
        if game.league.sport in ("soccer", "hockey"):
            scorers = (await bot.game_props(game, scorers=True))[0]
            if text := scorer_lines(scorers):
                embed.add_field(name="⚽ Goals and assists" if game.league.sport == "soccer" else "🏒 Goals and assists",
                                value=text, inline=False)
        await interaction.followup.send(embed=fit_embed(embed))

    async def _lineup_note(legs) -> str:
        """Whether the slip's players come from confirmed lineups, and the check to come if not."""
        players = [leg for leg in legs if leg.player_id]
        if not players:
            return ""
        lined = {(l.league, l.game_id, l.path) for l in players if LEAGUES[l.league].sport in LINEUP_SPORTS}
        if not lined:
            return "\n🩹 I'll post here if a pick is ruled out before the game."

        async def out(league, gid, path):
            try:
                return bool((await bot._availability(league, gid, path)).lineups)
            except Exception:
                return False
        confirmed = sum(await asyncio.gather(*(out(*g) for g in lined)))
        if confirmed == len(lined):
            return "\n✅ Picked from the **confirmed lineups**: every player starts."
        later = ("When the lineups drop (about an hour before kickoff) I'll check every pick and post here "
                 "if anyone isn't starting.")
        if confirmed:
            return f"\n✅ {confirmed} of {len(lined)} games' lineups are out: only starters picked there. ⏳ {later}"
        return f"\n⏳ Lineups aren't out yet. {later}"

    bot._lineup_note_for_tests = _lineup_note

    def _units_text(chance: float) -> str:
        units = units_for(chance)
        return f"{units:g} unit{'s' if units > 1 else ''} (see /bankroll)"

    async def _parlay(interaction, lg, target, games, one_game: bool, scorers: bool = False):
        async def one(g):
            try:
                if scorers:  # anytime goalscorers and assists, like a FanDuel goal/assist slip
                    return [leg for leg in trend_legs(g, (await bot.game_props(g, scorers=True))[0])
                            if leg.stat != "goalOrAssist"]
                trends, ml = await bot.game_props(g, target.bigger)
                legs = trend_legs(g, trends) + ([ml] if ml else [])
                if target.bigger:  # the near-certain lines too, to finish near the target
                    legs += trend_legs(g, (await bot.game_props(g))[0])
                if target.key == "lotto":  # long shots pay more per leg: underdogs, goalscorers
                    if dog := (await bot.game_props(g, underdog=True))[1]:
                        legs.append(dog)
                    if lg.sport in ("soccer", "hockey"):
                        legs += trend_legs(g, (await bot.game_props(g, scorers=True))[0])
                return legs
            except Exception:
                log.warning("Parlay research failed for %s", g.id, exc_info=True)
                return []
        candidates = {(leg.pick, leg.game_id): leg for found in await asyncio.gather(*(one(g) for g in games))
                      for leg in found}
        # One game (picked, a team's, or the only one coming up): every leg comes from it.
        same_game = one_game or len(games) == 1
        chosen = build_to_target(list(candidates.values()), target, per_game=MAX_LEGS if same_game else MAX_LEGS_PER_GAME,
                                 balance=scorers)
        if len(chosen) < max(2, target.min_legs):
            if same_game:
                where = f"{games[0].away.name} @ {games[0].home.name}"
            else:
                where = f"the {len(games)} {lg.name} games coming up in the next week"
            kind = "goalscorer and assist legs" if scorers else "strong legs"
            await interaction.followup.send(f"Not enough {kind} in {where} for a {target.name} parlay. "
                                            "Try a smaller payout, or another game.")
            return
        embed, slip = parlay_embed(lg.name, lg.emoji, chosen, target, same_game=same_game)
        pid = bot.parlays.record(interaction.channel_id, lg.key, target.name, chosen)
        await interaction.followup.send(embed=embed)
        note = await _lineup_note(chosen)
        await interaction.followup.send(f"📋 Copy or screenshot for your odds bot:\n{slip}{note}\n"
                                        f"💵 Stake: {_units_text(combined(chosen))}. Tap **I placed it** to log your "
                                        "stake and price.\nI'll grade every leg after the games and post the result here.",
                                        view=placed_view(pid))

    async def _overview(interaction, lg):
        """The league's strongest leans, a safe parlay, and how the research has done."""
        if lg.sport != "cricket":
            games = [g for g in await bot.espn.scoreboard(lg) if g.state == "pre"]
            limit = asyncio.Semaphore(5)

            async def one(g):
                async with limit:
                    try:
                        return await bot.research(g)
                    except Exception:
                        log.warning("Research failed for %s", g.id, exc_info=True)
                        return None
            reports = [x for x in await asyncio.gather(*(one(g) for g in games[:20])) if x]
            if reports:
                embed = picks_embed(lg.name, lg.emoji, reports)
                if field := record_field(bot.parlays.summary()):
                    embed.add_field(name=field[0], value=field[1], inline=False)
                await interaction.followup.send(embed=fit_embed(embed))
        if games := await _upcoming(lg, want=PARLAY_GAMES):
            await _parlay(interaction, lg, TARGETS["safe"], games, one_game=False)

    BETS = [app_commands.Choice(name="All bets", value="all"),
            app_commands.Choice(name="Goalscorers & assists", value="scorers"),
            app_commands.Choice(name="Assists round robin (soccer)", value="rr-assists"),
            app_commands.Choice(name="3-pointers round robin (NBA)", value="rr-threes")]

    async def _round_robin(interaction, lg, shot, games, one_game: bool, size: int, payout: str | None = None):
        async def one(g):
            try:
                return trend_legs(g, await bot.longshots(g, shot.key, payout))
            except Exception:
                log.warning("Round robin research failed for %s", g.id, exc_info=True)
                return []
        candidates = [leg for found in await asyncio.gather(*(one(g) for g in games)) for leg in found]
        same_game = one_game or len(games) == 1
        # Spread across games like a typical round robin; in one game, one per team still applies to assists.
        chosen = pick_round_robin(candidates, size, per_game=size if same_game else (2 if shot.sport == "basketball" else 1))
        if len(chosen) < 3:  # a round robin of 2's needs at least 3 picks
            await interaction.followup.send(f"Not enough long shots in the upcoming {lg.name} games for a {shot.name} "
                                            "yet. Try again closer to game day, or another league.")
            return
        embed, slip = round_robin_embed(lg.name, lg.emoji, chosen, shot, same_game=same_game)
        pid = bot.parlays.record(interaction.channel_id, lg.key, shot.name, chosen, round_robin=2)
        await interaction.followup.send(embed=embed)
        pairs = math.comb(len(chosen), 2)
        note = await _lineup_note(chosen)
        await interaction.followup.send(f"📋 Copy or screenshot for your odds bot:\n{slip}{note}\n"
                                        f"Bet them as a round robin of 2's. 💵 Stake: "
                                        f"{_units_text(chance_at_least([leg.probability for leg in chosen], 2))} in total, "
                                        f"split across the {pairs} bets. Tap **I placed it** to log your stake and "
                                        "each pick's price.\nI'll grade every pick after the games and post how many "
                                        "pairs cashed.", view=placed_view(pid))

    @tree.command(name="research", description="Betting research: a league's best picks, a team's game, or a parlay")
    @app_commands.describe(
        league="League (leave out to use the one this channel follows)",
        team="A team: everything on its next game (with a parlay: legs from that game only)",
        game="A specific game: everything on it (with a parlay: a same-game parlay)",
        parlay="Safe (around +100), Big payout (+1000 to +10000) or Lotto (4-10 legs, +3000 to +20000)",
        bets="Goalscorers & assists, or a round robin of long shots: assists (soccer) or 3-pointers (NBA)",
        picks="Round robins: how many picks (3-6, default 3)",
    )
    @app_commands.choices(league=LEAGUE_CHOICES, bets=BETS,
                          parlay=[app_commands.Choice(name=t.name, value=t.key) for t in TARGETS.values()])
    async def research(interaction: discord.Interaction, league: app_commands.Choice[str] | None = None,
                       team: TeamName | None = None, game: app_commands.Range[str, 1, 100] | None = None,
                       parlay: app_commands.Choice[str] | None = None, bets: app_commands.Choice[str] | None = None,
                       picks: app_commands.Range[int, 3, 6] = 3):
        await interaction.response.defer(thinking=True)
        key, problem = await pick_league(interaction, league, team, game)
        if key is None:
            await interaction.followup.send(problem)
            return
        lg = LEAGUES[key]
        scorers = bets is not None and bets.value == "scorers"
        if scorers and lg.sport not in ("soccer", "hockey"):
            await interaction.followup.send("Goalscorer and assist bets are for soccer and the NHL.")
            return
        shot = LONGSHOTS.get(bets.value) if bets is not None else None
        if shot and lg.sport != shot.sport:
            await interaction.followup.send(f"The {shot.name} is for {'soccer' if shot.sport == 'soccer' else 'the NBA'}.")
            return
        try:
            picked = None
            if game:
                picked = await _find_picked_game(lg, game)
                if picked is None:
                    await interaction.followup.send(f"Couldn't find that {lg.name} game in the next week. "
                                                    "Pick one from the suggestions.")
                    return
            elif team:
                picked = await _next_game(lg, team)
                if picked is None:
                    await interaction.followup.send(f"No {lg.name} game for **{team}** in the next week on ESPN.")
                    return
            if shot:
                if picked is not None:
                    if picked.state != "pre":
                        await interaction.followup.send("That game has already started.")
                        return
                    await _round_robin(interaction, lg, shot, [picked], one_game=True, size=picks,
                                       payout=parlay.value if parlay else None)
                elif games := await _upcoming(lg, want=PARLAY_GAMES):
                    await _round_robin(interaction, lg, shot, games, one_game=False, size=picks,
                                       payout=parlay.value if parlay else None)
                else:
                    await interaction.followup.send(f"No {lg.name} games in the next week on ESPN.")
            elif parlay or scorers:
                # Goalscorer/assist slips are long shots (4 legs is usually +3000 or more), so they default to a Lotto.
                target = TARGETS[parlay.value if parlay else "lotto" if scorers else "safe"]
                if picked is not None:
                    if picked.state != "pre":
                        await interaction.followup.send("That game has already started, so there's no parlay to build.")
                        return
                    await _parlay(interaction, lg, target, [picked], one_game=True, scorers=scorers)
                elif games := await _upcoming(lg, want=PARLAY_GAMES):
                    await _parlay(interaction, lg, target, games, one_game=False, scorers=scorers)
                else:
                    await interaction.followup.send(f"No {lg.name} games in the next week on ESPN.")
            elif picked is not None:
                await _report(interaction, picked)
            else:
                await _overview(interaction, lg)
        except Exception:
            log.exception("Research failed for %s %s %s", key, team, game)
            await interaction.followup.send("Couldn't load ESPN's data for that, try again shortly.")
        finally:
            release_memory()

    research.autocomplete("team")(team_suggestions)

    @research.autocomplete("game")
    async def game_suggestions(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        key = getattr(interaction.namespace, "league", None)
        keys = [key] if key in LEAGUES else channel_leagues(interaction.channel_id)
        if not keys:
            return []
        weeks = await gather_within(2.5, *(bot.week_games(k) for k in keys))
        q = current.strip().lower()
        now = time.time()
        out = []
        for g in sorted((g for week in weeks for g in week), key=lambda g: g.start):
            if (st := start_time(g)) is not None and st.timestamp() <= now:
                continue  # started since the week was cached
            emoji = f"{g.league.emoji} " if len(keys) > 1 else ""  # several leagues: show whose game it is
            label = f"{emoji}{g.away.name} @ {g.home.name} · {_when(g)}"[:100]
            if not q or q in label.lower():
                out.append(app_commands.Choice(name=label, value=g.id))
        return out[:25]

    @tree.command(name="bankroll", description="Your bankroll, what to stake, and how your logged bets have done")
    @app_commands.describe(
        start="Start your bankroll at this amount: the money set aside for betting (starts the tracking over)",
        add="Add money to your bankroll (a negative amount takes some out)",
        style="How big a unit is: Careful 1%, Standard 2% or Aggressive 3% of your balance",
    )
    @app_commands.choices(style=[app_commands.Choice(name=f"{name} ({share:.0%} per unit)", value=key)
                                 for key, (name, share) in STYLES.items()])
    async def bankroll(interaction: discord.Interaction,
                       start: app_commands.Range[float, 1, 10_000_000] | None = None,
                       add: app_commands.Range[float, -10_000_000, 10_000_000] | None = None,
                       style: app_commands.Choice[str] | None = None):
        uid, notes = interaction.user.id, []
        if start is not None:
            bot.bets.set_bankroll(uid, start, style.value if style else None)
            notes.append(f"Bankroll set to {money(start)}. Results count from now.")
        elif style is not None and bot.bets.bankroll(uid) is None:
            notes.append("Set your bankroll first, with `start:`.")
        elif style is not None:
            bot.bets.set_style(uid, style.value)
        if add is not None:
            if bot.bets.add_money(uid, add) is None:
                notes.append("Set your bankroll first, with `start:`.")
            else:
                notes.append(f"{'Added' if add >= 0 else 'Took out'} {money(abs(add))}.")
        if style is not None and bot.bets.bankroll(uid):
            notes.append(f"Style: {STYLES[style.value][0]}.")
        embed = bankroll_embed(bot.bets, uid, interaction.user.display_name)
        await interaction.response.send_message(" ".join(dict.fromkeys(notes)) or None, embed=embed, ephemeral=True)

    @tree.command(name="record", description="How the research leans and parlays have done so far")
    async def record(interaction: discord.Interaction):
        embed = record_embed(bot.leans.summary(), bot.leans.pending())
        if field := record_field(bot.parlays.summary()):
            embed.add_field(name=field[0], value=field[1], inline=False)
        await interaction.response.send_message(embed=fit_embed(embed))

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
        await interaction.response.send_message(embed=fit_embed(embed), ephemeral=True)

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
        await interaction.response.send_message(clip("\n".join(lines), MESSAGE), ephemeral=True)

    @tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        # Without a reply, Discord just shows "The application did not respond".
        name = interaction.command.qualified_name if interaction.command else "?"
        log.error("/%s failed", name, exc_info=error)
        msg = "Something went wrong running that command. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


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
