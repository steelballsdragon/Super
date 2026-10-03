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


class Inter:
    """Just enough of a Discord interaction to run a command."""

    def __init__(self, channel_id=7, **namespace):
        self.channel_id = channel_id
        self.namespace = SimpleNamespace(**namespace)
        self.sent = []
        outer = self

        class Response:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **kw):
                self.done = True

            async def send_message(self, content=None, embed=None, **kw):
                outer.sent.append(content or embed.title)
        self.response = Response()

        async def followup(content=None, embed=None, **kw):
            outer.sent.append(content or embed.title)
        self.followup = SimpleNamespace(send=followup)


def bot_with_teams(tmp_path):
    from sportsbot.bot import register_commands
    bot = make_bot(tmp_path)
    register_commands(bot)
    teams = {"nfl": [("Kansas City Chiefs", "KC")], "mlb": [("Los Angeles Dodgers", "LAD")],
             "nba": [("Los Angeles Lakers", "LAL")]}

    async def team_list(key):
        return teams.get(key, [])
    bot.team_list = team_list

    async def scoreboard(league, date=None):
        return []
    bot.espn.scoreboard = scoreboard
    return bot


def test_commands_use_the_channels_league_when_it_is_left_out(tmp_path):
    bot = bot_with_teams(tmp_path)
    bot.store.add(7, "nfl")
    scores = bot.tree.get_command("scores").callback
    i = Inter()
    asyncio.run(scores(i))
    assert i.sent == ["🏈 NFL scores"]

    research = bot.tree.get_command("research")
    i = Inter()
    asyncio.run(research.callback(i, None, "zzz", None))  # NFL is used; there's just no game for that team
    assert i.sent == ["No NFL game for **zzz** in the next week on ESPN."]

    # Team suggestions come from the channel's league too.
    complete = bot.tree.get_command("scores")._params["team"].autocomplete
    assert [c.name for c in asyncio.run(complete(Inter(), "chi"))] == ["Kansas City Chiefs"]
    asyncio.run(bot.espn.close())


def test_with_several_leagues_the_team_picks_the_league(tmp_path):
    bot = bot_with_teams(tmp_path)
    for key in ("nfl", "mlb"):
        bot.store.add(7, key)
    research = bot.tree.get_command("research").callback
    i = Inter()
    asyncio.run(research(i, None, "Dodgers", None))
    assert i.sent == ["No MLB game for **Dodgers** in the next week on ESPN."]
    i = Inter()
    asyncio.run(research(i, None, None, None))  # no team: it can't tell which league
    assert i.sent == ["This channel follows NFL, MLB. Pick the league too."]

    # /scores with no league shows each followed league.
    i = Inter()
    asyncio.run(bot.tree.get_command("scores").callback(i))
    assert i.sent == ["🏈 NFL scores", "⚾ MLB scores"]
    asyncio.run(bot.espn.close())


def test_a_channel_following_nothing_is_asked_for_a_league(tmp_path):
    bot = bot_with_teams(tmp_path)
    i = Inter()
    asyncio.run(bot.tree.get_command("research").callback(i, None, None, None))
    assert i.sent[0].startswith("Pick a league")
    asyncio.run(bot.espn.close())


def test_unfollow_without_a_league(tmp_path):
    bot = bot_with_teams(tmp_path)
    bot.store.add(7, "nfl", "Chiefs")
    bot.store.add(7, "mlb")
    unfollow = bot.tree.get_command("unfollow").callback
    i = Inter()
    asyncio.run(unfollow(i, None, "Chiefs"))  # the followed team decides the league
    assert i.sent == ["🛑 Stopped updates for **Chiefs** in NFL."]
    i = Inter()
    asyncio.run(unfollow(i, None, None))  # now only MLB is left
    assert i.sent == ["🛑 Stopped updates for all **MLB** games."] and bot.store.for_channel(7) == []
    asyncio.run(bot.espn.close())
