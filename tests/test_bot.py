from dataclasses import replace

from sportsbot.espn import parse_scoreboard
from sportsbot.formatting import scoreboard_embed, update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.storage import SubscriptionStore
from sportsbot.tracker import FINAL, HALFTIME, KICKOFF, SCORE, Tracker


def event(state="pre", name="STATUS_SCHEDULED", home=0, away=0, details=None, last_play=None):
    comp = {
        "status": {"type": {"state": state, "name": name, "shortDetail": "67'"}},
        "competitors": [
            {"homeAway": "home", "score": str(home),
             "team": {"id": "359", "displayName": "Arsenal", "abbreviation": "ARS"}},
            {"homeAway": "away", "score": str(away),
             "team": {"id": "357", "displayName": "Leeds United", "abbreviation": "LEE"}},
        ],
        "details": details or [],
    }
    if last_play:
        comp["situation"] = {"lastPlay": {"text": last_play}}
    return {"events": [{"id": "1", "date": "2026-10-10T11:30Z", "competitions": [comp]}]}


def goal(team_id="359", minute="12'", name="Bukayo Saka", **flags):
    return {"scoringPlay": True, "team": {"id": team_id}, "clock": {"displayValue": minute},
            "athletesInvolved": [{"displayName": name}], **flags}


EPL = LEAGUES["epl"]
NFL = LEAGUES["nfl"]


def test_parse_scoreboard():
    [g] = parse_scoreboard(event("in", "STATUS_FIRST_HALF", 1, 0, [goal(), {"scoringPlay": False}]), EPL)
    assert g.home.name == "Arsenal" and g.home.score == 1
    assert g.away.abbrev == "LEE"
    assert g.state == "in"
    assert [x.scorer for x in g.goals] == ["Bukayo Saka"]
    assert g.involves("ars") and g.involves("leeds") and not g.involves("chelsea")


def test_first_snapshot_is_silent():
    t = Tracker()
    assert t.update("epl", parse_scoreboard(event("in", home=3), EPL)) == []


def test_full_match_lifecycle():
    t = Tracker()
    t.update("epl", parse_scoreboard(event(), EPL))

    ups = t.update("epl", parse_scoreboard(event("in", "STATUS_FIRST_HALF"), EPL))
    assert [u.kind for u in ups] == [KICKOFF]

    ups = t.update("epl", parse_scoreboard(event("in", "STATUS_FIRST_HALF", 1, 0, [goal()]), EPL))
    assert [u.kind for u in ups] == [SCORE]
    assert ups[0].new_goals[0].scorer == "Bukayo Saka"

    ups = t.update("epl", parse_scoreboard(event("in", "STATUS_HALFTIME", 1, 0, [goal()]), EPL))
    assert [u.kind for u in ups] == [HALFTIME]

    # No change -> no updates
    assert t.update("epl", parse_scoreboard(event("in", "STATUS_HALFTIME", 1, 0, [goal()]), EPL)) == []

    ups = t.update("epl", parse_scoreboard(event("post", "STATUS_FULL_TIME", 1, 0, [goal()]), EPL))
    assert [u.kind for u in ups] == [FINAL]


def test_overturned_goal_is_a_correction():
    t = Tracker()
    t.update("epl", parse_scoreboard(event("in", home=1, details=[goal()]), EPL))
    [u] = t.update("epl", parse_scoreboard(event("in"), EPL))
    assert u.kind == SCORE and u.score_decreased and u.new_goals == ()
    assert update_embed(u).title.endswith("Score correction")


def test_embeds_render():
    t = Tracker()
    t.update("epl", parse_scoreboard(event("in"), EPL))
    [u] = t.update("epl", parse_scoreboard(event("in", away=1, details=[goal("357", "80'", "Joe Rodon", penaltyKick=True)]), EPL))
    e = update_embed(u)
    assert "GOAL" in e.title
    assert "Leeds United 1 - 0 Arsenal" in e.description
    assert "80' Joe Rodon (pen) (LEE)" in e.description

    t.update("nfl", parse_scoreboard(event("in"), NFL))
    [u] = t.update("nfl", parse_scoreboard(event("in", home=7, last_play="J. Allen 5 yd pass to K. Shakir"), NFL))
    e = update_embed(u)
    assert e.title == "🏈 Score update" and "J. Allen" in e.description

    board = scoreboard_embed("epl", parse_scoreboard(event("in", home=2), EPL))
    assert "LEE **0 - 2** ARS" in board.description


def test_store_roundtrip(tmp_path):
    path = tmp_path / "subs.json"
    s = SubscriptionStore(path)
    assert s.add(1, "nfl")
    assert s.add(1, "epl", " Arsenal ")
    assert not s.add(1, "epl", "arsenal")
    s2 = SubscriptionStore(path)
    assert {(x.league, x.team) for x in s2.for_channel(1)} == {("nfl", None), ("epl", "arsenal")}
    assert s2.remove(1, "epl", "ARSENAL")
    assert s2.leagues() == {"nfl"}
    s2.remove_channel(1)
    assert SubscriptionStore(path).leagues() == set()
