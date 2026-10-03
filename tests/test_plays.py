import asyncio

from sportsbot.espn import parse_scoreboard, parse_scoring_plays
from sportsbot.formatting import update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.plays import PLAY_WAIT_SECONDS, PlayResolver
from sportsbot.tracker import FINAL, SCORE, Tracker

NFL = LEAGUES["nfl"]


def board(state="in", away=0, home=0, leaders=None):
    comp = {
        "status": {"type": {"state": state, "name": "STATUS_IN_PROGRESS", "shortDetail": "Q1 2:59"}},
        "competitors": [
            {"homeAway": "home", "score": str(home),
             "team": {"id": "5", "displayName": "Cleveland Browns", "abbreviation": "CLE"}},
            {"homeAway": "away", "score": str(away),
             "team": {"id": "23", "displayName": "Pittsburgh Steelers", "abbreviation": "PIT"}},
        ],
        "leaders": leaders or [],
    }
    return parse_scoreboard({"events": [{"id": "9", "date": "2026-10-04T17:00Z", "competitions": [comp]}]}, NFL)


def play(pid, away, home, text="Roman Wilson 12 Yd pass from Aaron Rodgers (Chris Boswell Kick)",
         kind="Passing Touchdown", category="Touchdown", team="PIT", period=1, clock="2:59"):
    return {"id": pid, "type": {"text": kind}, "scoringType": {"displayName": category}, "text": text,
            "team": {"abbreviation": team}, "period": {"number": period}, "clock": {"displayValue": clock},
            "awayScore": away, "homeScore": home}


class Feed:
    """Fake ESPN summary feed whose scoring plays the test controls."""

    def __init__(self):
        self.plays = []

    async def __call__(self, event_id):
        return parse_scoring_plays({"scoringPlays": self.plays})


def step(tracker, resolver, games):
    return asyncio.run(resolver.resolve(games, tracker.update("nfl", games)))


def setup():
    feed = Feed()
    tracker, resolver = Tracker(), PlayResolver(feed)
    step(tracker, resolver, board())
    return feed, tracker, resolver


def test_touchdown_posts_the_scoring_play():
    feed, tracker, resolver = setup()
    feed.plays = [play("p1", 7, 0)]
    [u] = step(tracker, resolver, board(away=7))
    assert u.kind == SCORE and u.play.text.startswith("Roman Wilson 12 Yd pass")
    e = update_embed(u)
    assert e.title == "🏈 TOUCHDOWN — Pittsburgh Steelers"
    assert "Pittsburgh Steelers 7 - 0 Cleveland Browns" in e.description
    assert "*Passing Touchdown*" in e.description
    assert e.footer.text == "NFL · Q1 2:59"


def test_waits_for_a_late_play_then_posts_it():
    feed, tracker, resolver = setup()
    assert step(tracker, resolver, board(away=3)) == []  # ESPN hasn't published the play yet
    feed.plays = [play("fg", 3, 0, "Chris Boswell 48 Yd Field Goal", "Field Goal Good", "Field Goal")]
    [u] = step(tracker, resolver, board(away=3))
    assert update_embed(u).title == "🏈 FIELD GOAL — Pittsburgh Steelers"
    assert step(tracker, resolver, board(away=3)) == []  # not posted twice


def test_extra_point_after_touchdown_is_not_reposted():
    feed, tracker, resolver = setup()
    feed.plays = [play("p1", 6, 0, "Roman Wilson 12 Yd pass from Aaron Rodgers (kick pending)")]
    assert len(step(tracker, resolver, board(away=6))) == 1
    feed.plays = [play("p1", 7, 0)]  # same play, now with the kick
    assert step(tracker, resolver, board(away=7)) == []


def test_falls_back_to_plain_score_when_play_never_appears():
    now = [0.0]
    feed, tracker = Feed(), Tracker()
    resolver = PlayResolver(feed, clock=lambda: now[0])
    step(tracker, resolver, board())
    assert step(tracker, resolver, board(away=2)) == []
    now[0] = PLAY_WAIT_SECONDS - 1
    assert step(tracker, resolver, board(away=2)) == []  # still waiting
    now[0] = PLAY_WAIT_SECONDS
    [u] = step(tracker, resolver, board(away=2))
    assert u.kind == SCORE and u.play is None


def test_old_plays_are_not_reposted_after_restart():
    feed = Feed()
    tracker, resolver = Tracker(), PlayResolver(feed)
    step(tracker, resolver, board(away=7, home=7))  # bot starts mid-game
    feed.plays = [play("a", 7, 0), play("b", 7, 7), play("c", 10, 7, "Chris Boswell 30 Yd Field Goal",
                                                         "Field Goal Good", "Field Goal")]
    [u] = step(tracker, resolver, board(away=10, home=7))
    assert u.play.id == "c"


def test_two_scores_between_polls_post_both_in_order():
    feed, tracker, resolver = setup()
    feed.plays = [play("a", 7, 0), play("b", 7, 3, "Andre Szmyt 41 Yd Field Goal", "Field Goal Good",
                                         "Field Goal", team="CLE")]
    ups = step(tracker, resolver, board(away=7, home=3))
    assert [u.play.id for u in ups] == ["a", "b"]


def test_final_lists_game_leaders():
    leaders = [{"name": "passingYards", "leaders": [{"displayValue": "22/40, 299 YDS, 3 TD, 2 INT",
                                                    "athlete": {"shortName": "A. Rodgers"}}]},
               {"name": "rushingYards", "leaders": [{"displayValue": "18 CAR, 104 YDS, 1 TD",
                                                    "athlete": {"shortName": "J. McLaughlin"}}]}]
    feed, tracker, resolver = setup()
    [u] = step(tracker, resolver, board("post", leaders=leaders))
    assert u.kind == FINAL
    [field] = update_embed(u).fields
    assert field.name == "Game leaders"
    assert "**PASS** A. Rodgers — 22/40, 299 YDS, 3 TD, 2 INT" in field.value
    assert "**RUSH** J. McLaughlin — 18 CAR, 104 YDS, 1 TD" in field.value
