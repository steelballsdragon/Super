"""Game threads and the live scoreboard, against fake Discord objects."""

import asyncio
import itertools
import tempfile
from types import SimpleNamespace

import discord

from sportsbot.bot import SportsBot, register_commands
from sportsbot.espn import GoalDetail, parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.settings import SettingsStore, StateStore
from sportsbot.storage import SubscriptionStore

ids = itertools.count(1000)


class FakeMessage:
    def __init__(self, where, embed=None, content=None):
        self.id, self.where, self.embed, self.content, self.pinned = next(ids), where, embed, content, False

    async def create_thread(self, name, auto_archive_duration):
        if self.where.server.no_threads:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        return self.where.server.add(FakeChannel(self.where.server, name=name))

    async def pin(self):
        self.pinned = True


class FakePartial:
    def __init__(self, channel, message_id):
        self.channel, self.message_id = channel, message_id

    async def edit(self, embed):
        msg = next((m for m in self.channel.messages if m.id == self.message_id), None)
        if msg is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        msg.embed = embed
        self.channel.edits += 1

    async def delete(self):
        self.channel.messages = [m for m in self.channel.messages if m.id != self.message_id]


class FakeChannel:
    def __init__(self, server, name=""):
        self.id, self.server, self.name, self.messages, self.edits = next(ids), server, name, [], 0

    async def send(self, content=None, embed=None):
        msg = FakeMessage(self, embed, content)
        self.messages.append(msg)
        return msg

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    def titles(self):
        return [(m.embed.title or m.embed.description.splitlines()[0]) if m.embed else m.content.splitlines()[0] for m in self.messages]


class FakeServer:
    def __init__(self):
        self.channels, self.no_threads = {}, False

    def add(self, channel):
        self.channels[channel.id] = channel
        return channel


def setup(tmp=None):
    tmp = tmp or tempfile.mkdtemp()
    store = SubscriptionStore(f"{tmp}/subs.json")
    bot = SportsBot(store, 10, None, SettingsStore(f"{tmp}/settings.json"), StateStore(f"{tmp}/state.json"))
    register_commands(bot)
    server = FakeServer()
    channel = server.add(FakeChannel(server, "epl"))
    bot.get_channel = lambda cid: server.channels.get(cid)

    async def fetch_channel(cid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")
    bot.fetch_channel = fetch_channel

    async def goal_details(event_id):
        return [GoalDetail("12'", "Bukayo Saka", None)]  # unassisted, so goals post straight away
    bot.play_resolvers["epl"]._fetch = goal_details
    return bot, server, channel, tmp


def match(state="in", home=0, away=0, minute="30'", goals=()):
    comp = {"status": {"type": {"state": state, "name": "STATUS_FIRST_HALF" if state == "in" else "STATUS_FULL_TIME", "shortDetail": minute}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "359", "displayName": "Arsenal", "abbreviation": "ARS"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "357", "displayName": "Leeds United", "abbreviation": "LEE"}}],
            "details": [{"scoringPlay": True, "team": {"id": "359"}, "clock": {"displayValue": m}, "athletesInvolved": [{"displayName": n}]} for m, n in goals]}
    return parse_scoreboard({"events": [{"id": "1", "date": "2026-10-10T11:30Z", "competitions": [comp]}]}, LEAGUES["epl"])


def run(bot, *snapshots):
    it = iter(snapshots)

    async def scoreboard(league):
        return next(it)
    bot.espn.scoreboard = scoreboard
    for _ in snapshots:
        asyncio.run(bot.poll())


SAKA = [("12'", "Bukayo Saka")]


def test_threads_keep_the_channel_to_start_and_result():
    bot, server, channel, _ = setup()
    bot.store.add(channel.id, "epl")
    bot.settings.update(channel.id, threads=True)
    run(bot, match("pre"), match("in"), match(home=1, goals=SAKA), match("post", home=1, goals=SAKA))
    assert channel.titles() == ["⚽ Kick-off", "⚽ Full-time"]
    [thread] = [c for c in server.channels.values() if c is not channel]
    assert thread.name == "⚽ ARS v LEE · Premier League"
    assert thread.titles() == ["⚽ GOAL!", "⚽ Full-time"]
    assert bot.state.items("threads") == []  # finished games are forgotten


def test_missed_kickoff_gets_a_header_for_the_thread():
    bot, server, channel, _ = setup()
    bot.store.add(channel.id, "epl")
    bot.settings.update(channel.id, threads=True)
    run(bot, match(), match(home=1, goals=SAKA))
    assert channel.titles() == ["🔴 **Arsenal 1 - 0 Leeds United**"]
    [thread] = [c for c in server.channels.values() if c is not channel]
    assert thread.titles() == ["⚽ GOAL!"]


def test_threads_survive_a_restart():
    bot, server, channel, tmp = setup()
    bot.store.add(channel.id, "epl")
    bot.settings.update(channel.id, threads=True)
    run(bot, match("pre"), match("in"))
    bot2, _, _, _ = setup(tmp)  # same files, as after an update
    bot2.get_channel = lambda cid: server.channels.get(cid)
    run(bot2, match(home=0), match(home=1, goals=SAKA))
    threads = [c for c in server.channels.values() if c is not channel]
    assert len(threads) == 1 and threads[0].titles() == ["⚽ GOAL!"]


def test_without_thread_permission_updates_post_in_the_channel():
    bot, server, channel, _ = setup()
    server.no_threads = True
    bot.store.add(channel.id, "epl")
    bot.settings.update(channel.id, threads=True)
    run(bot, match("pre"), match("in"), match(home=1, goals=SAKA))
    assert channel.titles() == ["⚽ Kick-off", "🔴 **Arsenal 1 - 0 Leeds United**", "⚽ GOAL!"]


def command(bot, name, channel, **kwargs):
    replies = []

    async def defer(**kw):
        pass

    async def followup(msg, ephemeral=False):
        replies.append(msg)

    async def send_message(msg, ephemeral=False):
        replies.append(msg)
    inter = SimpleNamespace(channel_id=channel.id, response=SimpleNamespace(defer=defer, send_message=send_message),
                            followup=SimpleNamespace(send=followup))
    asyncio.run(bot.tree.get_command(name).callback(inter, **kwargs))
    return replies


def test_scoreboard_is_pinned_and_edits_only_when_scores_change():
    bot, server, channel, _ = setup()
    bot.store.add(channel.id, "epl")
    run(bot, match())
    [reply] = command(bot, "scoreboard", channel)
    assert reply.startswith("📺 Live scoreboard posted.")
    [board] = channel.messages
    assert board.pinned and board.embed.title == "📺 Live scoreboard"
    assert "🔴 ARS **0 - 0** LEE · 30'" in board.embed.description
    run(bot, match())  # same score: no edit
    assert channel.edits == 0
    run(bot, match(home=1, minute="31'", goals=SAKA))
    assert channel.edits == 1 and "🔴 ARS **1 - 0** LEE · 31'" in board.embed.description
    assert [m.embed.title for m in channel.messages] == ["📺 Live scoreboard", "⚽ GOAL!"]


def test_scoreboard_off_and_deleted_board():
    bot, server, channel, _ = setup()
    bot.store.add(channel.id, "epl")
    run(bot, match())
    command(bot, "scoreboard", channel)
    assert command(bot, "scoreboard", channel, enabled=False) == ["🛑 Live scoreboard removed."]
    assert channel.messages == [] and bot.settings.get(channel.id).board_message_id is None
    command(bot, "scoreboard", channel)
    channel.messages.clear()  # someone deleted it
    run(bot, match(home=1, goals=SAKA))
    assert bot.settings.get(channel.id).board_message_id is None


def test_threads_command():
    bot, server, channel, _ = setup()
    [reply] = command(bot, "threads", channel, enabled=True)
    assert reply.startswith("🧵 Game threads on.") and bot.settings.get(channel.id).threads
    command(bot, "threads", channel, enabled=False)
    assert not bot.settings.get(channel.id).threads
