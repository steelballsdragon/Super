import asyncio

from sportsbot.espn import parse_scoreboard, parse_scorepanel, parse_scoring_plays
from sportsbot.formatting import game_line, update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.plays import PlayResolver
from sportsbot.tracker import FINAL, HALFTIME, INNINGS, KICKOFF, PERIOD, WICKET, Tracker

NBA, MLB, IPL, CRICKET = (LEAGUES[k] for k in ("nba", "mlb", "ipl", "cricket"))


def us_game(league, state="in", name="STATUS_IN_PROGRESS", away=0, home=0, period=1, detail="Q1 6:00", leaders=False):
    def team(side, tid, abbrev, display, score, star, line):
        c = {"homeAway": side, "score": str(score), "team": {"id": tid, "abbreviation": abbrev, "displayName": display}}
        if leaders:
            c["leaders"] = [{"name": "points", "leaders": [{"displayValue": "30", "athlete": {"shortName": star}}]},
                            {"name": "rating", "leaders": [{"displayValue": line, "athlete": {"shortName": star}}]}]
        return c
    comp = {"status": {"period": period, "type": {"state": state, "name": name, "shortDetail": detail}},
            "competitors": [team("home", "18", "NY", "New York Knicks", home, "J. Brunson", "36 PTS, 7 AST, 3 STL"),
                            team("away", "24", "SA", "San Antonio Spurs", away, "V. Wembanyama", "24 PTS, 13 REB, 3 BLK")]}
    return parse_scoreboard({"events": [{"id": "1", "date": "2026-06-10T00:30Z", "competitions": [comp]}]}, league)


def test_nba_posts_quarters_not_every_basket():
    t = Tracker()
    t.update("nba", us_game(NBA, "pre", "STATUS_SCHEDULED"))
    assert [u.kind for u in t.update("nba", us_game(NBA))] == [KICKOFF]
    assert t.update("nba", us_game(NBA, away=2)) == []  # baskets aren't posted
    assert t.update("nba", us_game(NBA, away=9, home=4)) == []
    [u] = t.update("nba", us_game(NBA, name="STATUS_END_PERIOD", away=28, home=25, detail="End of 1st"))
    assert u.kind == PERIOD and update_embed(u).title == "🏀 End of Q1"
    assert "San Antonio Spurs 28 - 25 New York Knicks" in update_embed(u).description
    [u] = t.update("nba", us_game(NBA, name="STATUS_HALFTIME", away=55, home=50, period=2, leaders=True))
    assert u.kind == HALFTIME
    [u] = t.update("nba", us_game(NBA, "post", "STATUS_FINAL", away=106, home=107, period=4, leaders=True))
    e = update_embed(u)
    assert u.kind == FINAL and "New York Knicks win" in e.description
    [field] = e.fields
    assert field.name == "Top performers"
    assert "**NY** J. Brunson — 36 PTS, 7 AST, 3 STL" in field.value
    assert "**SA** V. Wembanyama — 24 PTS, 13 REB, 3 BLK" in field.value


def mlb_play(pid, away, home, text, runs, half="Bottom", inning="1st Inning", team="15"):
    return {"id": pid, "scoringPlay": True, "text": text, "awayScore": away, "homeScore": home, "scoreValue": runs,
            "period": {"type": half, "displayValue": inning}, "team": {"id": team}}


def test_mlb_runs_post_the_play():
    plays = {"plays": [{"id": "x", "scoringPlay": False, "text": "Olson struck out swinging."},
                       mlb_play("hr", 0, 3, "Harris II homered to right center (387 feet), Acuña Jr. scored and Olson scored.", 3)]}
    feed_plays = []

    async def feed(_):
        return parse_scoring_plays({"plays": feed_plays}, "baseball")

    def game(away, home):
        comp = {"status": {"type": {"state": "in", "name": "STATUS_IN_PROGRESS", "shortDetail": "Bot 1st"}},
                "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "15", "abbreviation": "ATL", "displayName": "Atlanta Braves"}},
                                {"homeAway": "away", "score": str(away), "team": {"id": "22", "abbreviation": "PHI", "displayName": "Philadelphia Phillies"}}]}
        return parse_scoreboard({"events": [{"id": "7", "date": "", "competitions": [comp]}]}, MLB)

    t, r = Tracker(), PlayResolver(feed)
    t.update("mlb", game(0, 0))
    feed_plays[:] = plays["plays"]
    [u] = asyncio.run(r.resolve(game(0, 3), t.update("mlb", game(0, 3))))
    e = update_embed(u)
    assert e.title == "⚾ HOME RUN — ATL"
    assert "Philadelphia Phillies 0 - 3 Atlanta Braves" in e.description
    assert "homered to right center (387 feet)" in e.description
    assert e.footer.text == "MLB · Bottom 1st"

    feed_plays.append(mlb_play("rbi", 1, 3, "Bohm grounded out to shortstop, Schwarber scored.", 1, "Top", "6th Inning", "22"))
    [u] = asyncio.run(r.resolve(game(1, 3), t.update("mlb", game(1, 3))))
    assert update_embed(u).title == "⚾ RUN SCORED — PHI"


def cricket_event(state="in", summary="India won toss & batted", ind=(51, 1, 3.4, True), pak=None, intl="3", eid="1552779"):
    def comp(side, name, abbrev, innings, score):
        return {"homeAway": side, "score": score, "team": {"id": abbrev, "displayName": name, "abbreviation": abbrev},
                "linescores": [{"runs": r, "wickets": w, "overs": o, "isBatting": b} for r, w, o, b in innings]}
    pak = pak or []
    ind_text = f"{ind[0]}/{ind[1]} ({ind[2]}/20 ov)"
    pak_text = f"{pak[-1][0]}/{pak[-1][1]}" if pak else ""
    c = {"class": {"internationalClassId": intl}, "status": {"summary": summary, "type": {"state": state, "shortDetail": "Live"}},
         "competitors": [comp("home", "India", "IND", [ind], ind_text), comp("away", "Pakistan", "PAK", pak, pak_text)]}
    return {"id": eid, "date": "2026-10-03T04:30Z", "competitions": [c]}


def panel(*events):
    return parse_scorepanel({"scores": [{"leagues": [{"id": "22547"}], "events": list(events)}]}, CRICKET)


def test_scorepanel_keeps_only_internationals():
    games = panel(cricket_event(), cricket_event(intl="0", eid="2"))
    assert [g.id for g in games] == ["1552779"]
    assert "🔴 IND **51/1 (3.4/20 ov)** · PAK — India won toss & batted" == game_line(games[0])


def test_cricket_wickets_innings_break_and_result():
    t = Tracker()
    t.update("cricket", panel(cricket_event()))
    assert t.update("cricket", panel(cricket_event(ind=(58, 1, 4.2, True)))) == []  # runs aren't posted
    [u] = t.update("cricket", panel(cricket_event(ind=(87, 3, 9.2, True))))
    assert u.kind == WICKET and u.count == 2
    e = update_embed(u)
    assert e.title == "🏏 2 WICKETS!" and "India 87/3 (9.2/20 ov) · Pakistan" in e.description
    ups = t.update("cricket", panel(cricket_event(ind=(165, 9, 20.0, False), pak=[(0, 0, 0.0, True)],
                                                  summary="Pakistan need 166 runs")))
    assert [(u.kind, u.count) for u in ups] == [(WICKET, 6), (INNINGS, 1)]
    ups = t.update("cricket", panel(cricket_event(ind=(165, 9, 20.0, False), pak=[(20, 1, 2.0, True)],
                                                  summary="Pakistan need 146 runs from 108 balls")))
    assert [u.kind for u in ups] == [WICKET] and "Pakistan need 146 runs" in update_embed(ups[0]).description
    [u] = t.update("cricket", panel(cricket_event("post", "India won by 63 runs", ind=(165, 9, 20.0, False),
                                                  pak=[(102, 10, 17.3, False)])))
    e = update_embed(u)
    assert u.kind == FINAL and e.title == "🏏 Result" and "India won by 63 runs" in e.description


def test_cricket_innings_break_is_posted_once():
    t = Tracker()
    t.update("cricket", panel(cricket_event(ind=(160, 7, 19.5, True))))
    ups = t.update("cricket", panel(cricket_event(ind=(165, 7, 20.0, False), pak=[(0, 0, 0.0, True)],
                                                  summary="Pakistan need 166 runs")))
    assert [u.kind for u in ups] == [INNINGS]
    assert "Pakistan need 166 runs" in update_embed(ups[0]).description
    assert t.update("cricket", panel(cricket_event(ind=(165, 7, 20.0, False), pak=[(4, 0, 0.3, True)]))) == []


def test_cricket_match_start_shows_toss():
    t = Tracker()
    t.update("ipl", parse_scoreboard({"events": [cricket_event("pre", "Starts at 19:30")]}, IPL))
    [u] = t.update("ipl", parse_scoreboard({"events": [cricket_event()]}, IPL))
    e = update_embed(u)
    assert e.title == "🏏 Match started" and "India won toss & batted" in e.description


def test_link_shaped_leaders_and_bad_events_are_tolerated():
    ok = cricket_event()
    ok["competitions"][0]["competitors"][0]["leaders"] = {"$ref": "http://example/leaders"}  # as IPL sends it
    bad = {"id": "broken", "competitions": [{"competitors": "not a list"}]}
    games = parse_scoreboard({"events": [bad, ok]}, IPL)
    assert [g.id for g in games] == ["1552779"]


NHL = LEAGUES["nhl"]


def nhl_game(state="in", name="STATUS_IN_PROGRESS", away=0, home=0, period=1, leaders=False):
    def team(side, tid, abbrev, display, score, star, pts):
        c = {"homeAway": side, "score": str(score), "team": {"id": tid, "abbreviation": abbrev, "displayName": display}}
        if leaders:
            c["leaders"] = [{"name": "goals", "leaders": [{"displayValue": "2", "athlete": {"shortName": star}}]},
                            {"name": "points", "leaders": [{"displayValue": pts, "athlete": {"shortName": star}}]}]
        return c
    comp = {"status": {"period": period, "type": {"state": state, "name": name, "shortDetail": "1st 10:00"}},
            "competitors": [team("home", "7", "CAR", "Carolina Hurricanes", home, "S. Aho", "1"),
                            team("away", "23", "WSH", "Washington Capitals", away, "A. Tuch", "2")]}
    return parse_scoreboard({"events": [{"id": "401892432", "date": "", "competitions": [comp]}]}, NHL)


def nhl_play(pid, away, home, text, strength="even-strength", period="1st", clock="6:33", team="23"):
    return {"id": pid, "scoringPlay": True, "text": text, "awayScore": away, "homeScore": home, "scoreValue": 1,
            "period": {"displayValue": period}, "clock": {"displayValue": clock}, "team": {"id": team},
            "strength": {"abbreviation": strength, "text": strength.replace("-", " ").title()}}


def test_nhl_goals_periods_and_final():
    feed_plays = []

    async def feed(_):
        return parse_scoring_plays({"plays": feed_plays}, "hockey")

    t, r = Tracker(), PlayResolver(feed)
    step = lambda games: asyncio.run(r.resolve(games, t.update("nhl", games)))
    step(nhl_game("pre", "STATUS_SCHEDULED"))
    [u] = step(nhl_game())
    assert update_embed(u).title == "🏒 Puck drop"

    feed_plays.append(nhl_play("g1", 1, 0, "Alex Tuch Goal (1) Wrist Shot, assists: Pierre-Luc Dubois (1), Alex Ovechkin (1)"))
    [u] = step(nhl_game(away=1))
    e = update_embed(u)
    assert e.title == "🏒 GOAL — WSH"
    assert "Washington Capitals 1 - 0 Carolina Hurricanes" in e.description
    assert "Alex Tuch Goal (1) Wrist Shot\n🅰️ Assists: Pierre-Luc Dubois (1), Alex Ovechkin (1)" in e.description
    assert e.footer.text == "NHL · 1st 6:33"

    [u] = step(nhl_game(name="STATUS_END_PERIOD", away=1))
    assert update_embed(u).title == "🏒 End of 1st"
    assert step(nhl_game(name="STATUS_INTERMISSION", away=1)) == []  # not posted twice

    feed_plays.append(nhl_play("g2", 1, 1, "Sebastian Aho Goal (1) Snap Shot, assists: Andrei Svechnikov (1)",
                               "power-play", "2nd", "16:50", "7"))
    [u] = step(nhl_game(away=1, home=1, period=2))
    assert update_embed(u).title == "🏒 POWER-PLAY GOAL — CAR"

    feed_plays.append(nhl_play("g3", 1, 2, "Seth Jarvis Goal (4) Wrist Shot, Empty Net", "short-handed", "3rd", "19:02", "7"))
    [u] = step(nhl_game(away=1, home=2, period=3))
    assert update_embed(u).title == "🏒 EMPTY-NET GOAL — CAR"

    [u] = step(nhl_game("post", "STATUS_FINAL", away=1, home=2, period=3, leaders=True))
    e = update_embed(u)
    assert e.title == "🏒 Final" and "Carolina Hurricanes win" in e.description
    assert "**WSH** A. Tuch — 2 PTS" in e.fields[0].value


def test_hockey_overtime_and_shorthanded_labels():
    from sportsbot.espn import period_label
    assert [period_label(n, "hockey") for n in (1, 3, 4, 5)] == ["1st", "3rd", "OT", "2OT"]
    assert [period_label(n) for n in (4, 5)] == ["Q4", "OT"]
    [p] = parse_scoring_plays({"plays": [nhl_play("x", 0, 1, "Goal", "short-handed")]}, "hockey")
    assert p.category == "Shorthanded Goal"
