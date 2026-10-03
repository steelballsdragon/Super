import asyncio
import tempfile

from sportsbot.balls import BallFeed
from sportsbot.espn import parse_balls, parse_scorepanel
from sportsbot.formatting import ball_messages
from sportsbot.leagues import LEAGUES
from sportsbot.storage import SubscriptionStore

CRICKET = LEAGUES["cricket"]


def item(seq, over, short, kind, runs, wkts, ball=1, complete=False, over_runs=0, text="", dismissal=""):
    return {"sequence": seq, "shortText": short, "text": text, "playType": {"description": kind},
            "team": {"abbreviation": "IND"}, "innings": {"runs": runs, "wickets": wkts},
            "over": {"actual": over, "number": int(float(over)) + 1, "ball": ball, "complete": complete, "runs": over_runs},
            "dismissal": {"dismissal": bool(dismissal), "text": dismissal}}


def page(items, pages):
    return {"commentary": {"pageCount": pages, "items": items}}


def test_parse_balls_cleans_dismissal_text():
    balls, pages = parse_balls(page([
        item(100203, "2.3", "Seales to Shubman Gill, OUT", "out", 3, 1,
             dismissal="Shubman Gill c &dagger;Hope b Seales 1 (6b 0x4 0x6) SR: 16.66"),
        {"sequence": 0, "shortText": "", "over": {}, "innings": {}},  # empty placeholder ESPN appends
    ], 2))
    assert pages == 2 and len(balls) == 1
    assert balls[0].dismissal == "Shubman Gill c †Hope b Seales 1 (6b 0x4 0x6)"


def game():
    c = {"class": {"internationalClassId": "2"}, "status": {"type": {"state": "in"}},
         "competitors": [{"homeAway": "home", "score": "4/2", "team": {"id": "6", "abbreviation": "IND", "displayName": "India"}, "linescores": []},
                         {"homeAway": "away", "score": "", "team": {"id": "4", "abbreviation": "WI", "displayName": "West Indies"}, "linescores": []}]}
    [g] = parse_scorepanel({"scores": [{"leagues": [{"id": "24289"}], "events": [{"id": "1529229", "competitions": [c]}]}]}, CRICKET)
    return g


def test_game_knows_its_series_path():
    assert game().path == "cricket/24289"


def test_feed_starts_at_the_current_ball_then_posts_new_ones():
    pages = {1: [item(1, "0.1", "Seales to Sharma, no run", "no run", 0, 0)],
             2: [item(2, "2.2", "Seales to Gill, no run", "no run", 3, 0)]}
    calls = []

    async def fetch(path, event_id, p):
        calls.append((path, p))
        n = max(pages)
        return parse_balls(page(pages[p or 1], n))

    feed = BallFeed(fetch)
    g = game()
    assert asyncio.run(feed.new_balls(g)) == []  # following mid-match doesn't replay history
    assert calls == [("cricket/24289", None), ("cricket/24289", 2)]
    pages[2].append(item(3, "2.3", "Seales to Shubman Gill, OUT", "out", 3, 1,
                         dismissal="Shubman Gill c &dagger;Hope b Seales 1 (6b 0x4 0x6) SR: 16.66"))
    pages[3] = [item(4, "2.4", "Seales to Kohli, FOUR", "four", 7, 1, text="Driven through cover")]
    new = asyncio.run(feed.new_balls(g))
    assert [b.over for b in new] == ["2.3", "2.4"]  # reads on into the next page
    assert asyncio.run(feed.new_balls(g)) == []  # nothing new, nothing posted


def test_ball_message_layout():
    balls, _ = parse_balls(page([
        item(3, "2.3", "Seales to Shubman Gill, OUT", "out", 3, 1, dismissal="Shubman Gill c &dagger;Hope b Seales 1 (6b 0x4 0x6) SR: 16.66"),
        item(4, "2.4", "Seales to Kohli, FOUR", "four", 7, 1, text="Driven through cover"),
        item(5, "2.6", "Seales to Kohli, 1 run", "run", 8, 1, complete=True, over_runs=6),
    ], 1))
    [msg] = ball_messages(game(), balls)
    assert msg.splitlines() == [
        "🏏 **IND v WI**",
        "`2.3` Seales to Shubman Gill, 🔴 **OUT!** · **IND 3/1**",
        "> Shubman Gill c †Hope b Seales 1 (6b 0x4 0x6)",
        "`2.4` Seales to Kohli, 4️⃣ **FOUR!** · **IND 7/1**",
        "> Driven through cover",
        "`2.6` Seales to Kohli, 1 run · **IND 8/1**",
        "*End of over 3: 6 runs*",
    ]


def test_long_bursts_are_split_under_discords_limit():
    balls, _ = parse_balls(page([item(i, f"{i // 6}.{i % 6 + 1}", "Bowler to Batter, " + "x" * 80, "run", i, 0) for i in range(60)], 1))
    msgs = ball_messages(game(), balls)
    assert len(msgs) > 1 and all(len(m) <= 2000 for m in msgs)


def test_switching_a_follow_to_ball_by_ball_and_back():
    store = SubscriptionStore(tempfile.mktemp())
    store.add(1, "cricket", "India")
    assert store.add(1, "cricket", "India", ball_by_ball=True)
    [s] = store.for_channel(1)
    assert s.ball_by_ball
    assert SubscriptionStore(store.path).for_channel(1)[0].ball_by_ball  # saved
    assert store.remove(1, "cricket", "India") and store.for_channel(1) == []


def test_sixth_legal_ball_ends_the_over_even_before_espn_flags_it():
    balls, _ = parse_balls(page([item(1, "7.6", "Forde to Gaikwad, no run", "no run", 23, 2, ball=6, over_runs=2),
                                 item(2, "8.6", "Joseph to Sharma, 1 wide", "wide", 24, 2, ball=6)], 1))
    assert [b.over_complete for b in balls] == [True, False]
    assert "*End of over 8: 2 runs*" in ball_messages(game(), balls[:1])[0]
