import asyncio

from sportsbot.espn import parse_scoreboard, parse_scoring_plays
from sportsbot.formatting import update_embed
from sportsbot.leagues import LEAGUES
from sportsbot.plays import PLAY_WAIT_SECONDS, WATCH_CHECK_SECONDS, WATCH_SECONDS, PlayResolver
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


def test_a_late_play_posts_the_score_at_once_then_edits_it_in():
    feed, tracker, resolver = setup()
    [now] = step(tracker, resolver, board(away=3))  # ESPN hasn't published the play yet: the score goes out now
    assert now.kind == SCORE and now.play is None and now.provisional and not now.edit
    feed.plays = [play("fg", 3, 0, "Chris Boswell 48 Yd Field Goal", "Field Goal Good", "Field Goal")]
    [u] = step(tracker, resolver, board(away=3))
    assert u.edit and u.provisional == now.provisional  # that post becomes the play
    assert update_embed(u).title == "🏈 FIELD GOAL — Pittsburgh Steelers"
    assert step(tracker, resolver, board(away=3)) == []  # not posted twice


def test_scoring_again_before_the_play_updates_the_score_post():
    feed, tracker, resolver = setup()
    [first] = step(tracker, resolver, board(away=3))
    [again] = step(tracker, resolver, board(away=6))
    assert again.edit and again.provisional == first.provisional and again.play is None
    feed.plays = [play("fg1", 3, 0, "Chris Boswell 48 Yd Field Goal", "Field Goal Good", "Field Goal"),
                  play("fg2", 6, 0, "Chris Boswell 51 Yd Field Goal", "Field Goal Good", "Field Goal")]
    a, b = step(tracker, resolver, board(away=6))
    assert (a.play.id, a.edit, a.provisional) == ("fg1", True, first.provisional) and (b.play.id, b.edit) == ("fg2", False)


def test_extra_point_after_touchdown_is_not_reposted():
    feed, tracker, resolver = setup()
    feed.plays = [play("p1", 6, 0, "Roman Wilson 12 Yd pass from Aaron Rodgers (kick pending)")]
    assert len(step(tracker, resolver, board(away=6))) == 1
    feed.plays = [play("p1", 7, 0)]  # same play, now with the kick
    assert step(tracker, resolver, board(away=7)) == []  # the kick waits for its play rather than posting


def test_a_score_posted_at_once_that_was_part_of_a_posted_play_is_removed():
    now = [0.0]
    feed, tracker = Feed(), Tracker()
    resolver = PlayResolver(feed, clock=lambda: now[0])
    step(tracker, resolver, board())
    feed.plays = [play("p1", 6, 0, "Roman Wilson 12 Yd pass from Aaron Rodgers (kick pending)")]
    step(tracker, resolver, board(away=6))
    now[0] = 400  # long after the touchdown, so a 1-point change posts at once
    [extra] = step(tracker, resolver, board(away=7))
    assert extra.provisional and extra.play is None
    feed.plays = [play("p1", 7, 0)]  # ...but it was the same play's kick
    out = step(tracker, resolver, board(away=7))
    assert [(u.drop, u.provisional) for u in out if u.provisional] == [(True, extra.provisional)]


def test_falls_back_to_plain_score_when_play_never_appears():
    now = [0.0]
    feed, tracker = Feed(), Tracker()
    resolver = PlayResolver(feed, clock=lambda: now[0])
    step(tracker, resolver, board())
    [u] = step(tracker, resolver, board(away=2))  # posted at once
    assert u.kind == SCORE and u.play is None
    now[0] = PLAY_WAIT_SECONDS - 1
    assert step(tracker, resolver, board(away=2)) == []  # still looking for the play
    now[0] = PLAY_WAIT_SECONDS
    assert step(tracker, resolver, board(away=2)) == []  # never found: the score post stays as it is


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


NHL = LEAGUES["nhl"]


def nhl_board(away=0, home=0):
    comp = {"status": {"type": {"state": "in", "name": "STATUS_IN_PROGRESS", "shortDetail": "2nd 5:00"}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "12", "displayName": "New York Islanders", "abbreviation": "NYI"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "11", "displayName": "New Jersey Devils", "abbreviation": "NJ"}}]}
    return parse_scoreboard({"events": [{"id": "7", "date": "2026-10-04T23:00Z", "competitions": [comp]}]}, NHL)


def nhl_goal(text, participants=()):
    return {"id": "g1", "scoringPlay": True, "text": text, "team": {"id": "12"}, "period": {"displayValue": "2nd"},
            "clock": {"displayValue": "5:00"}, "awayScore": 0, "homeScore": 1, "strength": {"text": "Even Strength"},
            "participants": list(participants)}


def test_nhl_goal_posts_at_once_and_is_edited_when_espn_names_the_scorer():
    now = [0.0]
    plays = []

    async def feed(event_id):
        return parse_scoring_plays({"plays": plays}, "hockey")
    tracker, resolver = Tracker(), PlayResolver(feed, clock=lambda: now[0])

    def nhl_step(games):
        return asyncio.run(resolver.resolve(games, tracker.update("nhl", games)))
    nhl_step(nhl_board())
    plays.append(nhl_goal("Goal, assists: none"))  # ESPN's first version of the goal: a placeholder
    [u] = nhl_step(nhl_board(home=1))
    assert not u.edit and "⏳ Scorer and assists coming…" in update_embed(u).description
    plays[0] = nhl_goal("Brayden Schenn Goal (1) Snap Shot, assists: Victor Eklund (1)")
    now[0] = WATCH_CHECK_SECONDS - 1
    assert nhl_step(nhl_board(home=1)) == []  # re-checked every WATCH_CHECK_SECONDS, not every update
    now[0] = WATCH_CHECK_SECONDS
    [e] = nhl_step(nhl_board(home=1))
    desc = update_embed(e).description
    assert e.edit and "Brayden Schenn Goal (1) Snap Shot" in desc and "🅰️ Assists: Victor Eklund (1)" in desc
    # A correction minutes later (a second assist added) edits the post again; no change, no edit.
    plays[0] = nhl_goal("Brayden Schenn Goal (1) Snap Shot, assists: Victor Eklund (1), Matthew Schaefer (1)")
    now[0] = 5 * 60
    [e] = nhl_step(nhl_board(home=1))
    assert e.edit and "Matthew Schaefer (1)" in update_embed(e).description
    now[0] = 6 * 60
    assert nhl_step(nhl_board(home=1)) == []
    # After WATCH_SECONDS the post is left alone.
    plays[0] = nhl_goal("Someone Else Goal (1) Snap Shot, Unassisted")
    now[0] = WATCH_SECONDS + 1
    assert nhl_step(nhl_board(home=1)) == []


def test_nhl_scorer_comes_from_the_participants():
    [p] = parse_scoring_plays({"plays": [nhl_goal(", assists: Victor Eklund (1)",
                                                  [{"type": "scorer", "athlete": {"displayName": "Brayden Schenn"}}])]},
                              "hockey")
    assert p.ready and p.text == "Brayden Schenn Goal, assists: Victor Eklund (1)"


def test_bot_edits_the_posts_of_a_corrected_play(tmp_path):
    from sportsbot.bot import SportsBot
    from sportsbot.storage import SubscriptionStore
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 10, None)
    bot.store.add(5, "nhl")
    plays = [nhl_goal("Goal, assists: none")]
    now = [0.0]

    async def feed(event_id):
        return parse_scoring_plays({"plays": plays}, "hockey")
    bot.play_resolvers["nhl"] = PlayResolver(feed, clock=lambda: now[0])
    boards = [nhl_board()]

    async def scoreboard(league, date=None):
        return boards[0]
    bot.espn.scoreboard = scoreboard

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
    asyncio.run(bot._poll_league("nhl"))
    boards[0] = nhl_board(home=1)
    asyncio.run(bot._poll_league("nhl"))
    [post] = posts
    assert "⏳ Scorer and assists coming…" in post.embed.description
    plays[0] = nhl_goal("Brayden Schenn Goal (1) Snap Shot, assists: Victor Eklund (1)")
    now[0] = WATCH_CHECK_SECONDS
    asyncio.run(bot._poll_league("nhl"))
    assert len(posts) == 1  # edited, not posted again
    assert "Brayden Schenn Goal (1) Snap Shot\n🅰️ Assists: Victor Eklund (1)" in post.embed.description
    asyncio.run(bot.espn.close())


def test_bot_posts_the_score_at_once_then_edits_it_into_the_play(tmp_path):
    from sportsbot.bot import SportsBot
    from sportsbot.storage import SubscriptionStore
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 10, None)
    bot.store.add(5, "nhl")
    plays = []

    async def feed(event_id):
        return parse_scoring_plays({"plays": plays}, "hockey")
    bot.play_resolvers["nhl"] = PlayResolver(feed)
    boards = [nhl_board()]

    async def scoreboard(league, date=None):
        return boards[0]
    bot.espn.scoreboard = scoreboard

    class Message:
        def __init__(self, embed):
            self.embed, self.deleted = embed, False

        async def edit(self, embed=None):
            self.embed = embed

        async def delete(self):
            self.deleted = True
    posts = []

    async def send(channel_id, embed=None, content=None, view=None):
        posts.append(Message(embed))
        return posts[-1]
    bot._send = send
    asyncio.run(bot._poll_league("nhl"))
    boards[0] = nhl_board(home=1)
    asyncio.run(bot._poll_league("nhl"))  # ESPN has no play yet: the score goes out now
    [post] = posts
    assert "New Jersey Devils 0 - 1 New York Islanders" in post.embed.description or "1" in post.embed.description
    plays.append(nhl_goal("Brayden Schenn Goal (1) Snap Shot, assists: Victor Eklund (1)"))
    asyncio.run(bot._poll_league("nhl"))
    assert len(posts) == 1 and "Brayden Schenn Goal (1) Snap Shot" in post.embed.description  # edited, not reposted
    assert [k for _, k in bot.play_posts] == ["play:g1"]  # later corrections find it under the play
    asyncio.run(bot.espn.close())


def test_busy_leagues_are_checked_every_cycle_and_idle_ones_every_30_seconds(tmp_path):
    from sportsbot.bot import IDLE_POLL_SECONDS, SportsBot
    from sportsbot.storage import SubscriptionStore
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 10, None)
    bot.store.add(5, "nhl")
    bot.store.add(5, "nfl")
    polled = []

    async def poll(key):
        polled.append(key)
    bot._poll_league = poll
    bot.latest = {"nhl": nhl_board(), "nfl": board(state="post")}
    asyncio.run(bot._poll_cycle())
    asyncio.run(bot._poll_cycle())
    assert polled.count("nhl") == 2 and polled.count("nfl") == 1
    bot._polled["nfl"] -= IDLE_POLL_SECONDS
    asyncio.run(bot._poll_cycle())
    assert polled.count("nfl") == 2
    asyncio.run(bot.espn.close())
