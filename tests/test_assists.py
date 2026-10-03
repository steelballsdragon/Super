import asyncio

from sportsbot.espn import parse_goal_details, parse_scoreboard
from sportsbot.formatting import _hockey_text, update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.plays import ASSIST_WAIT_SECONDS, AssistResolver
from sportsbot.tracker import FINAL, SCORE, Tracker

EPL = LEAGUES["epl"]


def match(state="in", home=0, away=0, goals=()):
    comp = {
        "status": {"type": {"state": state, "name": "STATUS_FIRST_HALF", "shortDetail": "30'"}},
        "competitors": [
            {"homeAway": "home", "score": str(home), "team": {"id": "382", "displayName": "Manchester City", "abbreviation": "MNC"}},
            {"homeAway": "away", "score": str(away), "team": {"id": "366", "displayName": "Sunderland", "abbreviation": "SUN"}},
        ],
        "details": [{"scoringPlay": True, "team": {"id": t}, "clock": {"displayValue": m}, "athletesInvolved": [{"displayName": n}]}
                    for t, m, n in goals],
    }
    return parse_scoreboard({"events": [{"id": "401879272", "date": "", "competitions": [comp]}]}, EPL)


def key_event(minute, *names, kind="Goal"):
    return {"scoringPlay": True, "clock": {"displayValue": minute}, "type": {"text": kind},
            "participants": [{"athlete": {"displayName": n}} for n in names]}


class Feed:
    def __init__(self):
        self.events = []
        self.commentary = None  # None: this match has no commentary

    async def __call__(self, event_id):
        summary = {"keyEvents": self.events}
        if self.commentary is not None:
            summary["commentary"] = [{"time": {"displayValue": m}, "text": t} for m, t in self.commentary]
        return parse_goal_details(summary)


def setup():
    feed = Feed()
    t, r = Tracker(), AssistResolver(feed)
    step = lambda games: asyncio.run(r.resolve(games, t.update("epl", games)))
    step(match())
    return feed, step


CHERKI = ("382", "29'", "Rayan Cherki")


def test_goal_shows_assist():
    feed, step = setup()
    feed.events = [key_event("29'", "Rayan Cherki", "Antoine Semenyo")]
    [u] = step(match(home=1, goals=[CHERKI]))
    e = update_embed(u)
    assert e.title == "⚽ GOAL!"
    assert "⚽ 29' Rayan Cherki (Manchester City)\n🅰️ Assist: Antoine Semenyo" in e.description


def test_unassisted_goal_posts_without_waiting():
    feed, step = setup()
    feed.events = [key_event("9'", "Enzo Fernández", kind="Goal - Free-kick")]
    [u] = step(match(home=1, goals=[("382", "9'", "Enzo Fernández")]))
    assert "Assist" not in update_embed(u).description


def test_own_goal_has_no_assist():
    [d] = parse_goal_details({"keyEvents": [key_event("63'", "Lisandro Martínez", "Someone Else", kind="Own Goal")]})
    assert d.assist is None


def test_waits_briefly_for_assist_then_posts_anyway():
    feed, step = setup()
    assert step(match(home=1, goals=[CHERKI])) == []  # details not published yet
    feed.events = [key_event("29'", "Rayan Cherki", "Antoine Semenyo")]
    [u] = step(match(home=1, goals=[CHERKI]))
    assert "Assist: Antoine Semenyo" in update_embed(u).description

    now = [0.0]
    t, r = Tracker(), AssistResolver(Feed(), clock=lambda: now[0])
    step2 = lambda games: asyncio.run(r.resolve(games, t.update("epl", games)))
    step2(match())
    assert step2(match(home=1, goals=[CHERKI])) == []
    now[0] = ASSIST_WAIT_SECONDS
    [u] = step2(match(home=1, goals=[CHERKI]))
    assert "29' Rayan Cherki" in update_embed(u).description and "Assist" not in update_embed(u).description


def test_goal_is_posted_before_the_final_whistle():
    feed, step = setup()
    ups = step(match("post", home=1, goals=[CHERKI]))
    assert [u.kind for u in ups] == [SCORE, FINAL]


def test_two_goals_while_waiting_are_posted_together():
    feed, step = setup()
    semenyo = ("382", "43'", "Antoine Semenyo")
    assert step(match(home=1, goals=[CHERKI])) == []
    feed.events = [key_event("29'", "Rayan Cherki", "Antoine Semenyo"), key_event("43'", "Antoine Semenyo", "Marc Guéhi")]
    [u] = step(match(home=2, goals=[CHERKI, semenyo]))
    desc = update_embed(u).description
    assert "Assist: Antoine Semenyo" in desc and "Assist: Marc Guéhi" in desc


def test_hockey_assists_on_their_own_line():
    assert _hockey_text("A Goal (1) Wrist Shot, assists: B (1), C (2)") == "A Goal (1) Wrist Shot\n🅰️ Assists: B (1), C (2)"
    assert _hockey_text("A Goal (1) Wrist Shot, Unassisted") == "A Goal (1) Wrist Shot\n🅰️ Unassisted"
    assert _hockey_text("A Goal (1) Wrist Shot") == "A Goal (1) Wrist Shot"


FREE_KICK_GOAL = [("8'", "Iliman Ndiaye (Manchester City) wins a free kick on the right wing."),
                  ("8'", "Foul by Dayann Méthalie (Sunderland)."),
                  ("9'", "Goal! Manchester City 1, Sunderland 0. Enzo Fernández (Manchester City) from a free kick with a "
                         "right footed shot to the top right corner.")]


def test_fanduel_assist_for_winning_the_free_kick_shows_with_the_goal():
    feed, step = setup()
    feed.events = [key_event("9'", "Enzo Fernández", kind="Goal - Free-kick")]
    feed.commentary = FREE_KICK_GOAL[:2]  # ESPN's commentary hasn't written the goal up yet: wait for it
    assert step(match(home=1, goals=[("382", "9'", "Enzo Fernández")])) == []
    feed.commentary = FREE_KICK_GOAL
    [u] = step(match(home=1, goals=[("382", "9'", "Enzo Fernández")]))
    assert ("⚽ 9' Enzo Fernández (Manchester City)\n🅰️ FanDuel assist: Iliman Ndiaye (won the free kick)"
            in update_embed(u).description)


def summary_with(*lines):
    return {"commentary": [{"time": {"displayValue": m}, "text": t} for m, t in lines]}


def test_fanduel_assist_rules():
    from sportsbot.espn import fanduel_assists
    cases = {
        "penalty won": summary_with(
            ("68'", "Penalty conceded by Bobby Thomas (Coventry City) after a foul in the penalty area."),
            ("68'", "Penalty Brighton and Hove Albion. Charalampos Kostoulas draws a foul in the penalty area."),
            ("70'", "Goal! Coventry City 0, Brighton and Hove Albion 3. Pascal Groß (Brighton and Hove Albion) converts "
                    "the penalty with a right footed shot to the bottom right corner.")),
        "rebound": summary_with(
            ("57'", "Attempt blocked. Florian Wirtz (Liverpool) right footed shot from the centre of the box is blocked. "
                    "Assisted by Cody Gakpo with a cross."),
            ("57'", "Goal! Bournemouth 0, Liverpool 1. Alexander Isak (Liverpool) right footed shot from the centre of "
                    "the box to the bottom left corner.")),
        "own goal off the post": summary_with(
            ("25'", "Arda Güler (Real Madrid) hits the left post with a left footed shot from outside the box from a "
                    "direct free kick."),
            ("25'", "Own Goal by Matías Dituro, Elche. Elche 0, Real Madrid 1.")),
        "own rebound: no assist": summary_with(
            ("57'", "Attempt saved. Alexander Isak (Liverpool) right footed shot is saved by Đorđe Petrović (Bournemouth)."),
            ("57'", "Goal! Bournemouth 0, Liverpool 1. Alexander Isak (Liverpool) right footed shot to the bottom left corner.")),
        "took his own penalty: no assist": summary_with(
            ("68'", "Penalty Manchester United. Bruno Fernandes draws a foul in the penalty area."),
            ("70'", "Goal! Manchester United 1, Ipswich Town 0. Bruno Fernandes (Manchester United) converts the penalty "
                    "with a right footed shot to the bottom left corner.")),
        "shot a minute earlier is not a rebound": summary_with(
            ("82'", "Attempt saved. Kevin Schade (Brentford) right footed shot is saved by Emiliano Martínez (Chelsea)."),
            ("83'", "Goal! Brentford 2, Chelsea 0. Igor Thiago (Brentford) right footed shot following a fast break.")),
    }
    got = {k: [(f.assist, f.how) for f in fanduel_assists(v)] for k, v in cases.items()}
    assert got == {
        "penalty won": [("Charalampos Kostoulas", "won the penalty")],
        "rebound": [("Florian Wirtz", "rebound")],
        "own goal off the post": [("Arda Güler", "forced the own goal")],
        "own rebound: no assist": [],
        "took his own penalty: no assist": [],
        "shot a minute earlier is not a rebound": [],
    }
