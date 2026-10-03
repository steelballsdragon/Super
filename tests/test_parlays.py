import asyncio
import time
from types import SimpleNamespace

from sportsbot.cricket_props import key_cricketers, parse_scorecard, player_histories
from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.parlays import ParlayBook, record_field, result_embed, settle
from sportsbot.props import Leg, PlayerGame
from sportsbot.settings import StateStore


def nfl_game(state="post", home=24, away=20):
    comp = {"status": {"type": {"state": state, "name": "STATUS_FINAL", "shortDetail": "Final"}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "28", "abbreviation": "WSH", "displayName": "Washington Commanders"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "11", "abbreviation": "IND", "displayName": "Indianapolis Colts"}}]}
    [g] = parse_scoreboard({"events": [{"id": "77", "date": "", "competitions": [comp]}]}, LEAGUES["nfl"])
    return g


def legs():
    return [
        Leg("Josh Downs Over 3.5 Receptions", 0.85, "", "IND @ WSH", "77", "p1", "prop", "receptions", 4, None, "nfl", "football/nfl"),
        Leg("Terry McLaurin Over 39.5 Receiving Yards", 0.80, "", "IND @ WSH", "77", "p2", "prop", "receivingYards", 40, None, "nfl", "football/nfl"),
        Leg("Washington Commanders Moneyline", 0.60, "", "IND @ WSH", "77", None, "moneyline", None, None, "28", "nfl", "football/nfl"),
    ]


class FakeBot:
    def __init__(self, tmp_path, games, box):
        self.state = StateStore(tmp_path / "state.json")
        self.latest = {"nfl": games}
        self.sent = []

        async def player_games(path, aid, fresh=False):
            assert fresh  # grading must not use cached logs
            return ([PlayerGame("2026-10-04", "2026", "WSH", box[aid], "77")] if aid in box else []), False
        self.props = SimpleNamespace(player_games=player_games)
        self.espn = None

    async def _send(self, channel_id, embed=None, content=None):
        self.sent.append((channel_id, embed))


def test_parlay_statuses(tmp_path):
    book = ParlayBook(StateStore(tmp_path / "s.json"))
    pid = book.record(9, "nfl", "Safest", legs())
    p = book.pending()[0]
    for status, expected in ((["hit", "pending", "pending"], "pending"), (["hit", "miss", "pending"], "pending"),
                             (["hit", "miss", "hit"], "lost"), (["hit", "void", "hit"], "won"), (["void"] * 3, "void")):
        for leg, st in zip(p["legs"], status):
            leg["status"] = st
        book.save(p)
        assert p["status"] == expected, status
    assert p["id"] == pid


def test_settle_grades_props_and_moneyline_and_posts_once(tmp_path):
    bot = FakeBot(tmp_path, [nfl_game()], {"p1": {"receptions": 6}, "p2": {"receivingYards": 31}})
    book = ParlayBook(bot.state)
    book.record(9, "nfl", "Safest", legs())
    asyncio.run(settle(bot, book))
    asyncio.run(settle(bot, book))  # nothing new: no second post
    [(channel, embed)] = bot.sent
    assert channel == 9 and embed.title == "🎟️ ❌ Parlay lost: 2/3 legs hit"
    assert embed.description.splitlines() == [
        "✅ **Josh Downs Over 3.5 Receptions** · 6 · predicted ~85%",
        "❌ **Terry McLaurin Over 39.5 Receiving Yards** · 31 · predicted ~80%",
        "✅ **Washington Commanders Moneyline** · IND 20 - 24 WSH · predicted ~60%",
    ]
    s = book.summary()["all"]
    assert (s["won"], s["lost"], s["hit"], s["miss"]) == (0, 1, 2, 1) and round(s["predicted"], 2) == 2.25
    name, text = record_field(book.summary())
    assert name == "🎟️ Parlays" and "legs 2-1 (hit 67% vs predicted 75%)" in text


def test_unfinished_game_waits_and_missing_player_is_voided_later(tmp_path):
    bot = FakeBot(tmp_path, [nfl_game(state="in")], {})
    book = ParlayBook(bot.state)
    book.record(9, "nfl", "Safest", legs()[:1])
    asyncio.run(settle(bot, book))
    assert book.pending()[0]["legs"][0]["status"] == "pending"  # game not over
    bot.latest["nfl"] = [nfl_game()]
    asyncio.run(settle(bot, book))
    assert book.pending()[0]["legs"][0]["status"] == "pending"  # box score may still be updating
    bot.state.set("finals", "nfl:77", time.time() - 13 * 3600)
    asyncio.run(settle(bot, book))
    assert book.pending() == [] and bot.sent[0][1].title == "🎟️ ➖ Parlay void: 0/0 legs hit"


def ball(seq, period, team, batter, bat_stats, other, bowler, bowl_stats):
    side = lambda tid: {"id": tid, "abbreviation": {"6": "IND", "4": "WI"}[tid], "displayName": {"6": "India", "4": "West Indies"}[tid]}
    bat_team, bowl_team = (side("6"), side("4")) if team == "6" else (side("4"), side("6"))
    runs, faced, fours, sixes = bat_stats
    return {"sequence": seq, "period": period, "team": bat_team,
            "batsman": {"athlete": {"id": batter[0], "displayName": batter[1]}, "team": bat_team,
                        "totalRuns": runs, "faced": faced, "fours": fours, "sixes": sixes},
            "otherBatsman": {"athlete": {"id": other[0], "displayName": other[1]}, "team": bat_team,
                             "totalRuns": 0, "faced": 0, "fours": 0, "sixes": 0},
            "bowler": {"athlete": {"id": bowler[0], "displayName": bowler[1]}, "team": bowl_team,
                       "overs": bowl_stats[0], "wickets": bowl_stats[1]}}


KOHLI, GILL, SEALES, JADEJA, HOPE, KING = ("1", "Virat Kohli"), ("2", "Shubman Gill"), ("3", "Jayden Seales"), ("4", "Ravindra Jadeja"), ("5", "Shai Hope"), ("6", "Brandon King")


def commentary():
    return {"commentary": {"items": [
        ball(100001, 1, "6", KOHLI, (4, 1, 1, 0), GILL, SEALES, (0.1, 0)),
        ball(100602, 1, "6", KOHLI, (75, 42, 9, 3), GILL, SEALES, (9.6, 2)),
        ball(200101, 2, "4", HOPE, (12, 10, 2, 0), KING, JADEJA, (1.1, 0)),
        ball(200901, 2, "4", HOPE, (40, 33, 4, 1), KING, JADEJA, (8.4, 3)),
    ]}}


def test_scorecard_rebuilt_from_both_innings_of_commentary():
    card = parse_scorecard(commentary(), "m1", "2026-10-04")
    assert card.teams == {"6": {"name": "India", "abbrev": "IND"}, "4": {"name": "West Indies", "abbrev": "WI"}}
    assert ("1", "Virat Kohli", "6", 75, 42, 9, 3) in card.batting  # the last ball's running total
    assert ("5", "Shai Hope", "4", 40, 33, 4, 1) in card.batting
    assert not any(r[0] in ("2", "6") for r in card.batting)  # non-strikers who never faced a ball
    assert ("4", "Ravindra Jadeja", "6", "8.4", 3) in card.bowling and ("3", "Jayden Seales", "4", "9.6", 2) in card.bowling


def test_cricket_histories_and_key_players():
    cards = [parse_scorecard(commentary(), f"m{i}", f"2026-0{i}-01") for i in range(1, 5)]
    bat, bowl = player_histories(cards, "6", lambda c: c.when[:4])
    assert len(bat["1"][1]) == 4 and bat["1"][1][0].stats == {"runs": 75, "fours": 9, "sixes": 3}
    assert bat["1"][1][0].opponent == "WI"
    batters, bowlers = key_cricketers(bat, bowl)
    assert [p[0] for p in batters] == ["1"] and [p[0] for p in bowlers] == ["4"]


def test_cricket_leg_graded_from_the_scorecard(tmp_path):
    async def get_json(url, params):
        return commentary()
    bot = FakeBot(tmp_path, [], {})
    bot.latest = {"cricket": []}
    bot.espn = SimpleNamespace(_get_json=get_json,
                               summary=lambda path, eid: _final_summary())
    book = ParlayBook(bot.state)
    book.record(9, "cricket", "Safest", [
        Leg("Virat Kohli Over 49.5 Runs", 0.6, "", "WI @ IND", "m1", "1", "prop", "runs", 50, None, "cricket", "cricket/24289"),
        Leg("Ravindra Jadeja Over 3.5 Wickets", 0.5, "", "WI @ IND", "m1", "4", "prop", "wickets", 4, None, "cricket", "cricket/24289"),
    ])
    asyncio.run(settle(bot, book))
    [(_, embed)] = bot.sent
    assert embed.description.splitlines()[0].startswith("✅ **Virat Kohli Over 49.5 Runs** · 75")
    assert embed.description.splitlines()[1].startswith("❌ **Ravindra Jadeja Over 3.5 Wickets** · 3")


async def _final_summary():
    return {"header": {"competitions": [{"status": {"type": {"state": "post"}},
                                         "competitors": [{"homeAway": "home", "score": "290/7", "team": {"id": "6"}},
                                                         {"homeAway": "away", "score": "250", "team": {"id": "4"}}]}]}}
