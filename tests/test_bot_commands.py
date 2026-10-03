import asyncio
import tempfile
from types import SimpleNamespace

from sportsbot.bot import SportsBot, poll_seconds, register_commands
from sportsbot.espn import parse_scorepanel
from sportsbot.formatting import update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.storage import SubscriptionStore
from sportsbot.tracker import OVERS, WICKET, Tracker

CRICKET = LEAGUES["cricket"]


def make_bot():
    bot = SportsBot(SubscriptionStore(tempfile.mktemp()), 10, None)
    register_commands(bot)
    return bot


def autocomplete(bot, command, league, current, channel_id=1):
    cmd = bot.tree.get_command(command)
    param = next(p for p in cmd._params.values() if p.name == "team")
    interaction = SimpleNamespace(namespace=SimpleNamespace(league=league), channel_id=channel_id)
    return asyncio.run(param.autocomplete(interaction, current))


def test_team_suggestions_filter_as_you_type():
    bot = make_bot()

    async def teams(league):
        return [("India", "IND"), ("Ireland", "IRE"), ("West Indies", "WI"), ("Pakistan", "PAK")]
    bot.espn.teams = teams
    assert [c.name for c in autocomplete(bot, "follow", "cricket", "ind")] == ["India", "West Indies"]
    assert [c.value for c in autocomplete(bot, "follow", "cricket", "pak")] == ["Pakistan"]
    assert len(autocomplete(bot, "scores", "cricket", "")) == 4
    assert autocomplete(bot, "follow", None, "ind") == []  # league not picked yet


def test_unfollow_suggests_only_followed_teams():
    bot = make_bot()
    bot.store.add(1, "cricket", "India")
    bot.store.add(1, "nfl", "Eagles")
    assert [c.value for c in autocomplete(bot, "unfollow", "cricket", "")] == ["india"]


def test_poll_interval_setting():
    assert poll_seconds(None) == 10
    assert poll_seconds("30") == 10  # written by older installers
    assert poll_seconds("20") == 20
    assert poll_seconds("1") == 5  # don't hammer ESPN


def cricket(ind=None, wi=None, summary="India won toss & batted", state="in"):
    def comp(side, name, abbrev, inn, max_overs):
        r, w, o, b = inn
        return {"homeAway": side, "score": f"{r}/{w} ({o}/{max_overs} ov)", "team": {"id": abbrev, "displayName": name, "abbreviation": abbrev},
                "linescores": [{"runs": r, "wickets": w, "overs": o, "isBatting": b}]}
    c = {"class": {"internationalClassId": "2"}, "status": {"summary": summary, "type": {"state": state, "shortDetail": "Live"}},
         "competitors": [comp("home", "India", "IND", ind, 50), comp("away", "West Indies", "WI", wi or (0, 0, 0.0, False), 50)]}
    return parse_scorepanel({"scores": [{"events": [{"id": "9", "date": "", "competitions": [c]}]}]}, CRICKET)


def test_cricket_score_every_ten_overs_in_odis():
    t = Tracker()
    t.update("cricket", cricket((40, 2, 8.3, True)))
    assert t.update("cricket", cricket((48, 2, 9.5, True))) == []
    [u] = t.update("cricket", cricket((52, 2, 10.1, True)))
    e = update_embed(u)
    assert u.kind == OVERS and e.title == "🏏 After 10 overs"
    assert "India 52/2 (10.1/50 ov)" in e.description and "India won toss & batted" in e.description
    assert t.update("cricket", cricket((60, 2, 11.0, True))) == []
    [u] = t.update("cricket", cricket((95, 3, 20.0, True)))
    assert u.kind == WICKET  # the wicket post shows the score, so no separate over update


def test_t20_posts_every_five_overs():
    from sportsbot.tracker import over_interval
    [g] = cricket((30, 0, 3.0, True))
    assert over_interval(g) == 10
    t20 = parse_scorepanel({"scores": [{"events": [{"id": "1", "date": "", "competitions": [{
        "class": {"internationalClassId": "3"}, "status": {"type": {"state": "in"}},
        "competitors": [{"homeAway": "home", "score": "30/0 (3/20 ov)", "team": {"id": "a"}, "linescores": [{"runs": 30, "overs": 3, "isBatting": True}]},
                        {"homeAway": "away", "score": "", "team": {"id": "b"}, "linescores": []}]}]}]}]}, CRICKET)
    assert over_interval(t20[0]) == 5
