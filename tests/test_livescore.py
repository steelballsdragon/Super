"""Faster soccer goals from LiveScore: parsing its real answers (tests/data), matching its teams to ESPN's, using
its goal while ESPN lags (and never "taking it back" when LiveScore can't be read), and goals ESPN scores before
naming the scorer being posted at once and edited when it does."""

import asyncio
import json
from pathlib import Path

from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.livescore import FastGoals, LiveGoal, find, parse_goals, parse_live, same_team
from sportsbot.plays import AssistResolver
from sportsbot.tracker import SCORE, Tracker

DATA = Path(__file__).parent / "data"


def load(name):
    return json.loads((DATA / name).read_text())


def test_parse_live_and_goals():
    live = parse_live(load("livescore_live.json"))
    rma = next(m for m in live if m.home == "Real Madrid")
    assert (rma.eid, rma.away, rma.home_score, rma.away_score, rma.minute) == ("1810714", "Villarreal", 1, 0, "79'")
    assert parse_goals(load("livescore_event_rma.json")) == [
        LiveGoal(True, "62'", "Yan Diomande", "Federico Valverde", False, False)]  # the VAR check isn't a goal
    alaves = parse_goals(load("livescore_event_alaves.json"))
    assert [(g.home_side, g.minute, g.scorer, g.assist, g.penalty) for g in alaves] == [
        (False, "33'", "Alejandro Grimaldo", "Jonathan David", False),
        (True, "45'+6'", "Lucas Boye", "", True),
        (False, "87'", "Cristian Romero", "Alejandro Baena", False)]
    assert parse_live({"Stages": [{"Events": [{"T1": [], "Tr1": "x"}]}]}) == [] and parse_goals({}) == []


def test_team_names_match_across_feeds():
    assert same_team("Alavés", "Deportivo Alaves") and same_team("Atlético Madrid", "Atletico Madrid")
    assert same_team("Chicago Fire FC", "Chicago Fire") and same_team("New York City FC", "New York City FC")
    assert not same_team("Real Madrid", "Real Sociedad") and not same_team("Manchester City", "Manchester United")


def espn(home, away, hs, as_, goals=(), state="in", gid="401882848"):
    details = [{"scoringPlay": True, "team": {"id": "1" if side == "home" else "2"}, "clock": {"displayValue": m},
                "athletesInvolved": [{"displayName": who}]} for m, who, side in goals]
    comp = {"status": {"type": {"state": state, "name": "STATUS_IN_PROGRESS", "shortDetail": "79'"}},
            "details": details,
            "competitors": [{"homeAway": "home", "score": str(hs), "team": {"id": "1", "displayName": home,
                                                                            "abbreviation": home[:3].upper()}},
                            {"homeAway": "away", "score": str(as_), "team": {"id": "2", "displayName": away,
                                                                             "abbreviation": away[:3].upper()}}]}
    [g] = parse_scoreboard({"events": [{"id": gid, "date": "2026-10-10T19:00Z", "competitions": [comp]}]},
                           LEAGUES["laliga"])
    return g


def fast_with(answers):
    fast = FastGoals(session_getter=None)

    async def get(url):
        return answers.get("event" if "scoreboard" in url else "live")
    fast._get = get
    return fast


def test_the_faster_goal_is_used_until_espn_catches_up():
    answers = {"live": load("livescore_live.json"), "event": load("livescore_event_rma.json")}
    fast = fast_with(answers)
    tracker = Tracker()
    before = espn("Real Madrid", "Villarreal", 0, 0)
    tracker.update("laliga", [before])
    assert find(before, parse_live(answers["live"])).eid == "1810714"

    [g] = asyncio.run(fast.overlay([before]))  # LiveScore has the goal, ESPN doesn't yet
    assert (g.home.score, g.away.score) == (1, 0)
    [u] = tracker.update("laliga", [g])
    assert u.kind == SCORE and [(x.minute, x.scorer, x.assist) for x in u.new_goals] == [
        ("62'", "Yan Diomande", "Federico Valverde")]
    assert fast.used == 1

    answers["live"] = None  # LiveScore can't be read: its goal stays, nothing is "taken back"
    fast._live = (-1e9, None)
    [g] = asyncio.run(fast.overlay([before]))
    assert (g.home.score, g.away.score) == (1, 0) and tracker.update("laliga", [g]) == []

    caught_up = espn("Real Madrid", "Villarreal", 1, 0, [("62'", "Yan Diomandé", "home")])
    answers["live"] = load("livescore_live.json")
    fast._live = (-1e9, None)
    [g] = asyncio.run(fast.overlay([caught_up]))
    assert g is caught_up and tracker.update("laliga", [g]) == []  # no second post
    assert fast._ahead == {}


def test_a_goal_livescore_takes_back_is_taken_back():
    live = load("livescore_live.json")
    answers = {"live": live, "event": load("livescore_event_rma.json")}
    fast = fast_with(answers)
    before = espn("Real Madrid", "Villarreal", 0, 0)
    asyncio.run(fast.overlay([before]))
    for stage in live["Stages"]:
        for e in stage["Events"]:
            if e["T1"][0]["Nm"] == "Real Madrid":
                e["Tr1"] = "0"  # VAR: no goal
    fast._live = (-1e9, None)
    [g] = asyncio.run(fast.overlay([before]))
    assert g is before and fast._ahead == {}


def test_espn_score_before_the_scorer_is_posted_then_edited():
    goals = []

    async def details(game_id):
        return goals
    resolver, tracker = AssistResolver(details), Tracker()

    def step(game):
        return asyncio.run(resolver.resolve([game], tracker.update("laliga", [game])))
    step(espn("Real Madrid", "Villarreal", 0, 0))
    [u] = step(espn("Real Madrid", "Villarreal", 1, 0))  # the score moved; the goal isn't listed yet
    assert u.kind == SCORE and not u.new_goals and u.provisional and not u.edit
    assert step(espn("Real Madrid", "Villarreal", 1, 0)) == []
    [e] = step(espn("Real Madrid", "Villarreal", 1, 0, [("62'", "Yan Diomandé", "home")]))
    assert e.edit and e.provisional == u.provisional and [g.scorer for g in e.new_goals] == ["Yan Diomandé"]


def test_bot_edits_a_nameless_goal_post_when_espn_names_the_scorer(tmp_path):
    from sportsbot.bot import SportsBot
    from sportsbot.storage import SubscriptionStore
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 5, None)
    bot.store.add(5, "laliga")
    boards = [espn("Real Madrid", "Villarreal", 0, 0)]

    async def scoreboard(league, date=None):
        return [boards[0]]
    bot.espn.scoreboard = scoreboard

    async def no_details(league, event_id):
        return []
    bot.espn.goal_details = no_details

    class Message:
        def __init__(self, embed):
            self.embed = embed

        async def edit(self, embed=None):
            self.embed = embed
    posts = []

    async def send(channel_id, embed=None, content=None, view=None):
        posts.append(Message(embed))
        return posts[-1]
    bot._send = send
    asyncio.run(bot._poll_league("laliga"))
    boards[0] = espn("Real Madrid", "Villarreal", 1, 0)
    asyncio.run(bot._poll_league("laliga"))
    [post] = posts
    assert "Yan" not in (post.embed.description or "")
    boards[0] = espn("Real Madrid", "Villarreal", 1, 0, [("62'", "Yan Diomandé", "home")])
    asyncio.run(bot._poll_league("laliga"))
    assert len(posts) == 1 and "Yan Diomandé" in post.embed.description  # edited, not posted again
    asyncio.run(bot.espn.close())
