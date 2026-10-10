"""Edge cases found by checking each sport against real ESPN data."""

import asyncio

from sportsbot.espn import parse_scoreboard, parse_scoring_plays
from sportsbot.formatting import update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.plays import FINAL_HOLD_SECONDS, PlayResolver
from sportsbot.tracker import CALLED_OFF, FINAL, PERIOD, SCORE, Tracker


def game(key, state, name, detail, home, away, period=0):
    def team(side, tid, abbrev, display, c):
        score, extra = (c, {}) if isinstance(c, int) else (c[0], c[1])
        return {"homeAway": side, "score": str(score), "team": {"id": tid, "abbreviation": abbrev, "displayName": display}, **extra}
    comp = {"status": {"period": period, "type": {"state": state, "name": name, "shortDetail": detail}},
            "competitors": [team("home", "1", home[0], home[1], home[2]), team("away", "2", away[0], away[1], away[2])]}
    return parse_scoreboard({"events": [{"id": "g", "date": "", "competitions": [comp]}]}, LEAGUES[key])


def test_postponed_game_is_not_reported_as_a_tie():
    t = Tracker()
    t.update("mlb", game("mlb", "pre", "STATUS_SCHEDULED", "7:10 PM", ("KC", "Kansas City Royals", 0), ("MIL", "Milwaukee Brewers", 0)))
    [u] = t.update("mlb", game("mlb", "post", "STATUS_POSTPONED", "Postponed", ("KC", "Kansas City Royals", 0), ("MIL", "Milwaukee Brewers", 0)))
    e = update_embed(u)
    assert u.kind == CALLED_OFF and e.title == "⚾ Postponed" and "Tie" not in e.description
    assert "Milwaukee Brewers vs Kansas City Royals" in e.description


def test_penalty_shootout_names_the_winner():
    t = Tracker()
    t.update("worldcup", game("worldcup", "in", "STATUS_SHOOTOUT", "Pens", ("GER", "Germany", 1), ("PAR", "Paraguay", 1)))
    [u] = t.update("worldcup", game("worldcup", "post", "STATUS_FINAL_PEN", "FT-Pens",
                                    ("GER", "Germany", (1, {"winner": False, "shootoutScore": 3})),
                                    ("PAR", "Paraguay", (1, {"winner": True, "shootoutScore": 4}))))
    assert u.kind == FINAL
    assert "Paraguay win 4-3 on penalties" in update_embed(u).description


def test_extra_time_winner():
    t = Tracker()
    t.update("worldcup", game("worldcup", "in", "STATUS_SECOND_HALF_EXTRA_TIME", "118'", ("BEL", "Belgium", 3), ("SEN", "Senegal", 2)))
    [u] = t.update("worldcup", game("worldcup", "post", "STATUS_FINAL_AET", "AET",
                                    ("BEL", "Belgium", (3, {"winner": True})), ("SEN", "Senegal", (2, {"winner": False}))))
    assert "Belgium win after extra time" in update_embed(u).description


def test_nhl_shootout_win_is_not_posted_as_a_goal():
    t = Tracker()
    t.update("nhl", game("nhl", "in", "STATUS_IN_PROGRESS", "SO", ("ANA", "Anaheim Ducks", 2), ("CGY", "Calgary Flames", 2), 5))
    [u] = t.update("nhl", game("nhl", "post", "STATUS_FINAL", "Final/SO",
                               ("ANA", "Anaheim Ducks", (3, {"winner": True})), ("CGY", "Calgary Flames", (2, {"winner": False})), 5))
    assert u.kind == FINAL and "Anaheim Ducks win in a shootout" in update_embed(u).description


def mlb_play(away, home):
    return {"id": "walkoff", "scoringPlay": True, "text": "Judge homered to left (412 feet).", "awayScore": away,
            "homeScore": home, "scoreValue": 1, "period": {"type": "Bottom", "displayValue": "9th Inning"}, "team": {"id": "1"}}


def test_walk_off_play_is_posted_before_the_final():
    now, plays = [0.0], []

    async def feed(_):
        return parse_scoring_plays({"plays": plays}, "baseball")

    t, r = Tracker(), PlayResolver(feed, clock=lambda: now[0])
    step = lambda g: asyncio.run(r.resolve(g, t.update("mlb", g)))
    nyy = lambda s, x=None: ("NYY", "New York Yankees", s if x is None else (s, x))
    step(game("mlb", "in", "STATUS_IN_PROGRESS", "Bot 9th", nyy(2), ("BOS", "Boston Red Sox", 2)))
    # The game ends on the home run, but ESPN hasn't published the play yet: the score goes out first, then the final.
    first = step(game("mlb", "post", "STATUS_FINAL", "Final", nyy(3, {"winner": True}), ("BOS", "Boston Red Sox", 2)))
    assert [(u.kind, u.play) for u in first] == [(SCORE, None), (FINAL, None)] and first[0].provisional
    plays.append(mlb_play(2, 3))
    [u] = step(game("mlb", "post", "STATUS_FINAL", "Final", nyy(3, {"winner": True}), ("BOS", "Boston Red Sox", 2)))
    assert u.edit and u.provisional == first[0].provisional  # the score post becomes the home run
    assert update_embed(u).title == "⚾ HOME RUN — New York Yankees"


def test_final_is_not_held_forever_if_the_play_never_appears():
    now = [0.0]

    async def feed(_):
        return []

    t, r = Tracker(), PlayResolver(feed, clock=lambda: now[0])
    step = lambda g: asyncio.run(r.resolve(g, t.update("nfl", g)))
    step(game("nfl", "in", "STATUS_IN_PROGRESS", "Q4 0:03", ("KC", "Kansas City Chiefs", 20), ("BUF", "Buffalo Bills", 21)))
    final = game("nfl", "post", "STATUS_FINAL", "Final", ("KC", "Kansas City Chiefs", (23, {"winner": True})), ("BUF", "Buffalo Bills", 21))
    score, u = step(final)  # nothing is held: the field goal's score, then the final
    assert score.kind == SCORE and u.kind == FINAL and "Kansas City Chiefs win" in update_embed(u).description
    now[0] = FINAL_HOLD_SECONDS
    assert step(final) == []


def test_nfl_end_of_quarter():
    t = Tracker()
    t.update("nfl", game("nfl", "in", "STATUS_IN_PROGRESS", "Q1 0:12", ("KC", "Kansas City Chiefs", 7), ("BUF", "Buffalo Bills", 3), 1))
    [u] = t.update("nfl", game("nfl", "in", "STATUS_END_PERIOD", "End of 1st", ("KC", "Kansas City Chiefs", 7), ("BUF", "Buffalo Bills", 3), 1))
    assert u.kind == PERIOD and update_embed(u).title == "🏈 End of Q1"
