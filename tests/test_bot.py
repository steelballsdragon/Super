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
    assert "Arsenal 0 - 1 Leeds United" in e.description
    assert "80' Joe Rodon (pen) (LEE)" in e.description

    t.update("nfl", parse_scoreboard(event("in"), NFL))
    [u] = t.update("nfl", parse_scoreboard(event("in", home=7, last_play="J. Allen 5 yd pass to K. Shakir"), NFL))
    e = update_embed(u)
    assert e.title == "🏈 Score update" and "J. Allen" in e.description
    assert "Leeds United 0 - 7 Arsenal" in e.description  # NFL keeps away team first

    board = scoreboard_embed("epl", parse_scoreboard(event("in", home=2), EPL))
    assert "ARS **2 - 0** LEE" in board.description
    assert "ARS vs LEE" in scoreboard_embed("epl", parse_scoreboard(event(), EPL)).description


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


def test_espn_requests_retry_when_throttled(monkeypatch):
    import asyncio
    import sportsbot.espn as espn

    calls = []

    class Resp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            if self.status >= 400:
                raise RuntimeError(self.status)

        async def json(self, content_type=None):
            return {"ok": True}

    class Session:
        closed = False

        def get(self, url, params=None):
            calls.append(url)
            return Resp(429 if len(calls) < 3 else 200)

    monkeypatch.setattr(espn, "RETRY_BASE_SECONDS", 0)
    client = espn.ESPNClient(Session())
    assert asyncio.run(client._get_json("u")) == {"ok": True} and len(calls) == 3


def test_scoreboard_adds_todays_games_while_espn_still_shows_an_earlier_day():
    import asyncio
    from datetime import datetime

    from sportsbot import espn

    today = datetime.now(espn.EASTERN).date()
    [old] = event(state="post", name="STATUS_FINAL")["events"]
    new = {**event()["events"][0], "id": "999"}
    asked = []

    async def get_json(url, params=None):
        asked.append(params)
        if params is None:  # ESPN's default: still a past day
            return {"day": {"date": "2000-01-01"}, "events": [old]}
        return {"events": [new, old]}  # today's games (the overlap is only listed once)

    client = espn.ESPNClient()
    client._get_json = get_json
    games = asyncio.run(client.scoreboard(LEAGUES["mlb"]))
    assert [g.id for g in games] == [old["id"], "999"]
    assert asked == [None, {"dates": f"{today:%Y%m%d}"}]

    # Once ESPN shows today (or a later matchday), there's no extra request.
    async def current(url, params=None):
        asked.append(params)
        return {"day": {"date": today.isoformat()}, "events": [new]}
    asked.clear()
    client._get_json = current
    assert [g.id for g in asyncio.run(client.scoreboard(LEAGUES["mlb"]))] == ["999"]
    assert asked == [None]


def test_a_day_with_no_games_is_not_rechecked_every_cycle(monkeypatch):
    import asyncio

    from sportsbot import espn

    asked = []

    async def get_json(url, params=None):
        asked.append((url.split("/")[-2], params))
        if params is None:
            return {"day": {"date": "2000-01-01"}, "events": []}
        return {"events": []}  # nothing on today (off-season)

    client = espn.ESPNClient()
    client._get_json = get_json
    clock = [1000.0]
    monkeypatch.setattr(espn.time, "monotonic", lambda: clock[0])
    for _ in range(5):
        asyncio.run(client.scoreboard(LEAGUES["mlb"]))
        asyncio.run(client.scoreboard(LEAGUES["nhl"]))
        clock[0] += 10
    assert [a for a in asked if a[1]] == [("mlb", asked[1][1]), ("nhl", asked[1][1])]  # once each
    clock[0] += espn.QUIET_DAY_SECONDS
    asyncio.run(client.scoreboard(LEAGUES["mlb"]))
    assert len([a for a in asked if a[1]]) == 3  # and again after a while
