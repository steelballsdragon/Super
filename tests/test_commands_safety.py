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
    asyncio.run(research.callback(i, team="zzz"))  # NFL is used; there's just no game for that team
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
    asyncio.run(research(i, team="Dodgers"))
    assert i.sent == ["No MLB game for **Dodgers** in the next week on ESPN."]
    i = Inter()
    asyncio.run(research(i))  # no team: it can't tell which league
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


def soccer_game(gid, home, away, day, league="mls"):
    from sportsbot.espn import parse_scoreboard
    from sportsbot.leagues import LEAGUES
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": home, "abbreviation": home, "displayName": home}},
                            {"homeAway": "away", "score": "0", "team": {"id": away, "abbreviation": away, "displayName": away}}]}
    [g] = parse_scoreboard({"events": [{"id": gid, "date": f"{day}T19:00Z", "competitions": [comp]}]}, LEAGUES[league])
    return g


def test_parlays_look_past_a_lone_midweek_game_and_never_fake_a_lotto(tmp_path):
    from datetime import datetime, timedelta

    from sportsbot.props import Leg
    bot = bot_with_teams(tmp_path)
    from sportsbot.espn import EASTERN
    today = datetime.now(EASTERN).date()
    lone = soccer_game("1", "CHI", "VAN", f"{today + timedelta(days=2):%Y-%m-%d}")
    weekend = [soccer_game(str(10 + i), f"H{i}", f"A{i}", f"{today + timedelta(days=4):%Y-%m-%d}") for i in range(8)]
    in_four_days = f"{today + timedelta(days=4):%Y%m%d}"

    async def scoreboard(league, day=None):
        return weekend if day == in_four_days else [lone] if day is None else []
    bot.espn.scoreboard = scoreboard

    async def game_props(game, bigger=False, underdog=False, scorers=False):
        if underdog or scorers:
            return [], None
        legs = [Leg(f"{game.home.name} P{i} Over 0.5 Shots", 0.62, "", "", game.id, f"{game.id}-{i}") for i in range(2)]
        return [SimpleNamespace(**{"pick": l.pick, "probability": l.probability, "evidence": "", "player_id": l.player_id,
                                   "prop": SimpleNamespace(stat="shots"), "line": 1, "player": "P",
                                   "team": game.home.abbrev}) for l in legs], None
    bot.game_props = game_props
    bot.store.add(7, "mls")
    research = bot.tree.get_command("research").callback
    i = Inter()
    asyncio.run(research(i, parlay=SimpleNamespace(value="lotto")))
    assert i.sent[0].startswith("🎟️ ⚽ MLS · Lotto") and "Closest" not in i.sent[0]

    # Only the lone game exists (and it has just 2 legs): no 2-leg "lotto", it says why instead.
    async def only_lone(league, day=None):
        return [lone] if day is None else []
    bot.espn.scoreboard = only_lone
    bot._week.clear()  # the week's games are cached; this is a different week
    i = Inter()
    asyncio.run(research(i, parlay=SimpleNamespace(value="lotto")))
    assert i.sent == ["Not enough strong legs in VAN @ CHI for a Lotto (4-10 legs, +3000 to +20000) parlay. "
                      "Try a smaller payout, or another game."]
    asyncio.run(bot.espn.close())


def test_pick_a_game_for_a_same_game_lotto_of_goalscorers(tmp_path):
    bot = bot_with_teams(tmp_path)
    bot.store.add(7, "epl")
    from datetime import datetime, timedelta, timezone
    saturday = f"{datetime.now(timezone.utc) + timedelta(days=5):%Y-%m-%d}"
    game = soccer_game("55", "Chelsea", "Bournemouth", saturday)
    other = soccer_game("56", "Arsenal", "Leeds United", saturday)

    async def week_games(key):
        return [game, other]
    bot.week_games = week_games
    asked = []

    async def game_props(g, bigger=False, underdog=False, scorers=False):
        asked.append((g.id, scorers))
        if not scorers:
            return [], None
        # Three scorers and three assisters for each side; only one of each per team can be used.
        names = {"CHE": ("Cole Palmer", "João Pedro", "Enzo Fernández"), "BOU": ("Antoine Semenyo", "Justin Kluivert", "Evanilson")}
        legs = [(f"{n} {wording}", p, f"{g.id}-{n}-{stat}", stat, n, team)
                for team, players in names.items() for n, p in zip(players, (0.45, 0.4, 0.3))
                for stat, wording in (("totalGoals", "Anytime Goalscorer"), ("goalAssists", "Anytime Assist"))]
        return [SimpleNamespace(pick=pick, probability=p, evidence="", player_id=pid, prop=SimpleNamespace(stat=stat),
                                line=1, player=n, team=team) for pick, p, pid, stat, n, team in legs], None
    bot.game_props = game_props
    research = bot.tree.get_command("research")

    # The game suggestions list the week's games; the value is the game's id.
    complete = research._params["game"].autocomplete
    found = asyncio.run(complete(Inter(), "chel"))
    assert [(c.name.split(" · ")[0], c.value) for c in found] == [("Bournemouth @ Chelsea", "55")]

    i = Inter()
    asyncio.run(research.callback(i, game="55", parlay=SimpleNamespace(value="lotto"),
                                  bets=SimpleNamespace(value="scorers")))
    assert i.sent[0].startswith("🎟️ ⚽ Premier League · Lotto") and "Closest" not in i.sent[0]
    assert {gid for gid, _ in asked} == {"55"} and all(s for _, s in asked)  # only that game, only scorer bets
    [parlay] = bot.parlays.pending()
    assert {leg["game_id"] for leg in parlay["legs"]} == {"55"} and len(parlay["legs"]) == 4
    # One goalscorer and one assister per team, never two assists from the same side.
    assert sorted((leg["team"], leg["stat"]) for leg in parlay["legs"]) == [
        ("BOU", "goalAssists"), ("BOU", "totalGoals"), ("CHE", "goalAssists"), ("CHE", "totalGoals")]

    # Typing the game instead of picking it works too.
    i = Inter()
    asyncio.run(research.callback(i, game="Bournemouth @ Chelsea", parlay=SimpleNamespace(value="safe"),
                                  bets=SimpleNamespace(value="scorers")))
    assert i.sent[0].startswith("🎟️ ⚽ Premier League · Safe")

    # Goalscorer bets are for soccer and hockey only.
    bot.store.add(8, "nfl")
    i = Inter(channel_id=8)
    asyncio.run(research.callback(i, bets=SimpleNamespace(value="scorers")))
    assert i.sent == ["Goalscorer and assist bets are for soccer and the NHL."]
    asyncio.run(bot.espn.close())


def test_game_picker_works_in_a_channel_following_several_leagues(tmp_path):
    from datetime import datetime, timedelta, timezone

    bot = bot_with_teams(tmp_path)
    for key in ("epl", "nfl"):
        bot.store.add(9, key)
    soon = f"{datetime.now(timezone.utc) + timedelta(days=2):%Y-%m-%d}"
    weeks = {"epl": [soccer_game("55", "Chelsea", "Bournemouth", soon, "epl")], "nfl": []}

    async def week_games(key):
        return weeks[key]
    bot.week_games = week_games
    research = bot.tree.get_command("research")
    found = asyncio.run(research._params["game"].autocomplete(Inter(channel_id=9), ""))
    assert [c.name.split(" · ")[0] for c in found] == ["⚽ Bournemouth @ Chelsea"]
    seen = []

    async def game_props(g, *args, **kw):
        seen.append(g.league_key)
        return [], None
    bot.game_props = game_props
    i = Inter(channel_id=9)
    asyncio.run(research.callback(i, game="55", parlay=SimpleNamespace(value="safe")))
    assert seen == ["epl"]  # the picked game decided the league
    asyncio.run(bot.espn.close())


def test_slow_suggestions_keep_loading_for_next_time():
    from sportsbot.bot import gather_within
    finished = []

    async def quick():
        return "quick"

    async def slow():
        await asyncio.sleep(0.2)
        finished.append("slow")
        return "slow"

    async def broken():
        raise RuntimeError("ESPN down")

    async def run():
        first = await gather_within(0.05, quick(), slow(), broken())
        await asyncio.sleep(0.3)  # the slow one wasn't cancelled: it finished (and would have been cached)
        return first
    assert asyncio.run(run()) == ["quick"] and finished == ["slow"]


def test_slate_and_round_robin_options_check_the_league_and_picks(tmp_path):
    bot = bot_with_teams(tmp_path)
    research = bot.tree.get_command("research")
    choice = lambda v: SimpleNamespace(value=v, name=v)
    i = Inter()
    asyncio.run(research.callback(i, league=choice("nba"), bets=choice("nfl-tds")))
    assert i.sent == ["The Anytime TD scorers is for the NFL."]
    i = Inter()
    asyncio.run(research.callback(i, league=choice("nfl"), bets=choice("slate-goals")))
    assert i.sent == ["The Goalscorer slate lotto is for soccer."]
    i = Inter()
    asyncio.run(research.callback(i, league=choice("nba"), bets=choice("rr-threes"), picks=10))
    assert i.sent == ["A round robin takes 3 to 6 picks."]
    i = Inter()
    asyncio.run(research.callback(i, league=choice("nfl"), bets=choice("nfl-tds")))
    assert i.sent == ["No NFL games in the next week on ESPN."]  # the fake scoreboard is empty
    asyncio.run(bot.espn.close())
