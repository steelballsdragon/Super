"""Commands always answer, and setup survives Discord hiccups."""

import asyncio
import logging
from types import SimpleNamespace

import discord

from sportsbot.bot import SportsBot
from sportsbot.storage import SubscriptionStore


class Response:
    def __init__(self, done=False):
        self.done, self.sent = done, []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.sent.append(content)


def make_bot(tmp_path):
    return SportsBot(SubscriptionStore(tmp_path / "subscriptions.json"), 10, None)


def test_a_failing_command_still_gets_a_reply(tmp_path, caplog):
    bot = make_bot(tmp_path)
    from sportsbot.bot import register_commands
    register_commands(bot)
    followups = []

    async def followup(content=None, **kw):
        followups.append(content)
    for done in (False, True):  # before and after the command deferred
        inter = SimpleNamespace(command=SimpleNamespace(qualified_name="schedule"), response=Response(done),
                                followup=SimpleNamespace(send=followup))
        with caplog.at_level(logging.ERROR):
            asyncio.run(bot.tree.on_error(inter, discord.app_commands.AppCommandError("boom")))
        replies = inter.response.sent if not done else followups
        assert replies and "Something went wrong" in replies[-1]
    assert "/schedule failed" in caplog.text
    asyncio.run(bot.espn.close())


def test_bot_starts_updating_even_if_command_registration_fails(tmp_path, caplog):
    bot = make_bot(tmp_path)

    async def sync(**kw):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="err"), "Discord is down")
    bot.tree.sync = sync

    async def run():
        await bot.setup_hook()
        running = bot.poll.is_running()
        bot.poll.cancel()
        await bot.espn.close()
        return running
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(run()) is True
    assert "Couldn't register the slash commands" in caplog.text


def test_status_and_following_stay_within_discord_limits(tmp_path):
    from sportsbot.bot import LeagueHealth, register_commands
    from sportsbot.leagues import LEAGUES
    bot = make_bot(tmp_path)
    register_commands(bot)
    for key in LEAGUES:
        bot.store.add(5, key)
        bot.health[key] = LeagueHealth(checked_at=1.0, error="ClientConnectorError: " + "x" * 190, error_at=2.0)
    for n in range(80):
        bot.store.add(5, "epl", f"Some Very Long Football Club Name Number {n}")
    sent = {}

    class Resp:
        async def send_message(self, content=None, embed=None, **kw):
            sent["content"], sent["embed"] = content, embed
    inter = SimpleNamespace(channel_id=5, response=Resp())
    asyncio.run(bot.tree.get_command("status").callback(inter))
    assert len(sent["embed"]) <= 6000 and len(sent["embed"].description) <= 4096
    asyncio.run(bot.tree.get_command("following").callback(inter))
    assert len(sent["content"]) <= 2000
    asyncio.run(bot.espn.close())


def test_outdated_server_commands_are_removed_once(tmp_path):
    bot = make_bot(tmp_path)
    bot.dev_guild = 3
    guilds = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    stale = {1: ["research game", "research picks"], 2: [], 3: ["research"]}
    synced, cleared = [], []

    async def fetch_commands(guild=None):
        return stale[guild.id]

    async def sync(guild=None):
        synced.append(guild.id)
        stale[guild.id] = []
    bot.tree.fetch_commands = fetch_commands
    bot.tree.sync = sync
    bot.tree.clear_commands = lambda guild=None: cleared.append(guild.id)
    type(bot).guilds = property(lambda self: guilds)
    try:
        asyncio.run(bot.on_ready())
        asyncio.run(bot.on_ready())  # reconnects don't repeat it
    finally:
        del type(bot).guilds
    assert cleared == [1] and synced == [1]  # only the server with leftovers; the dev server is left alone
    asyncio.run(bot.espn.close())
