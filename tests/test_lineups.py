"""Picks come from confirmed lineups, and open slips are checked when the lineups drop."""

import asyncio
import time
from datetime import datetime, timezone

from sportsbot.props import Availability, Leg, Lineup, availability, lineups_from
from tests.test_commands_safety import make_bot


def roster(team_id, abbrev, starters, bench):
    return {"team": {"id": team_id, "abbreviation": abbrev},
            "roster": [{"starter": True, "athlete": {"id": pid}} for pid in starters]
                      + [{"starter": False, "athlete": {"id": pid}} for pid in bench]}


XI_HOME = [str(i) for i in range(100, 111)]
XI_AWAY = [str(i) for i in range(200, 211)]


def summary(home_out=True, away_out=True, injured=()):
    return {"rosters": [roster("1", "ARS", XI_HOME if home_out else [], ["150", "151"] if home_out else []),
                        roster("2", "CHE", XI_AWAY if away_out else [], ["250"] if away_out else [])],
            "injuries": [{"injuries": [{"status": "Out", "athlete": {"displayName": n}} for n in injured]}]}


def test_lineups_only_count_once_announced():
    assert lineups_from(summary(False, False)) == {}
    lineups = lineups_from(summary(True, False))
    assert list(lineups) == ["1"] and lineups["1"].team == "ARS" and "150" in lineups["1"].squad
    assert len(lineups["1"].starters) == 11


def test_only_starters_can_be_picked_once_the_lineup_is_out():
    a = availability(summary(True, False, injured=["Hurt Guy"]))
    assert a.allows("1", "100", "Starter")
    assert not a.allows("1", "150", "Sub")  # on the bench
    assert not a.allows("1", "999", "Left out")  # not in the squad
    assert a.allows("2", "999", "Anyone")  # Chelsea's lineup isn't out yet
    assert not a.allows("2", "998", "Hurt Guy")  # but the injured are still out
    assert a.status("100", "x", "ARS") == "starts"
    assert a.status("150", "x", "ARS") == "bench"
    assert a.status("999", "x", "ARS") == "out"
    assert a.status("999", "Hurt Guy", "CHE") == "injured"
    assert a.status("998", "x", "CHE") == "unknown"
    assert Availability().allows("1", "1", "x")


def leg(pid, pick, team="ARS", start=None, league="epl", game_id="g1"):
    return Leg(pick, 0.3, "", "Chelsea @ Arsenal", game_id, pid, "prop", "goalAssists", 1, None, league, "",
               pick.split(" Anytime")[0], team, start or "")


def iso_in(minutes):
    return datetime.fromtimestamp(time.time() + minutes * 60, timezone.utc).isoformat().replace("+00:00", "Z")


def lineup_bot(tmp_path, avail):
    bot = make_bot(tmp_path)
    sent = []

    async def send(channel_id, embed=None, content=None):
        sent.append((channel_id, content))

    async def get(league, gid, path=""):
        return avail[0]
    bot._send, bot._availability = send, get
    return bot, sent


def test_open_slips_are_checked_when_the_lineups_drop(tmp_path):
    avail = [availability(summary(False, False))]
    bot, sent = lineup_bot(tmp_path, avail)
    start = iso_in(50)
    pid = bot.parlays.record(9, "epl", "Lotto", [leg("100", "Saka Anytime Assist", start=start),
                                                 leg("150", "Trossard Anytime Goalscorer", start=start),
                                                 leg("200", "Palmer Anytime Assist", "CHE", start)])
    bot.bets.place(bot.records.get("parlays", pid), 42, 2.0, price=30.0)
    asyncio.run(bot._check_lineups())
    assert sent == []  # nothing out yet

    avail[0] = availability(summary(True, False))  # Arsenal's lineup comes out first
    asyncio.run(bot._check_lineups())
    [(channel, text)] = sent
    assert channel == 9
    lines = text.splitlines()
    assert lines[0].startswith("📋 **Lineups are out** for Chelsea @ Arsenal (kickoff in 5")
    assert "**1 of your picks is not starting**" in lines[0] and lines[0].endswith("your Lotto slip:")
    assert lines[1:] == ["✅ Saka Anytime Assist: starts", "🪑 Trossard Anytime Goalscorer: **on the bench**", "<@42>"]

    avail[0] = availability(summary(True, True))  # then Chelsea's
    asyncio.run(bot._check_lineups())
    assert sent[1][1].splitlines()[1:] == ["✅ Palmer Anytime Assist: starts", "<@42>"]
    assert "they all start ✅" in sent[1][1]
    asyncio.run(bot._check_lineups())
    assert len(sent) == 2  # each team's news is said once
    asyncio.run(bot.espn.close())


def test_all_starting_and_far_off_games(tmp_path):
    avail = [availability(summary(True, True))]
    bot, sent = lineup_bot(tmp_path, avail)
    bot.parlays.record(9, "epl", "Safe", [leg("100", "Saka Anytime Assist", start=iso_in(300))])
    asyncio.run(bot._check_lineups())
    assert sent == []  # too early: lineups aren't checked until 75 minutes before
    bot.parlays.record(9, "epl", "Safe", [leg("100", "Saka Anytime Assist", start=iso_in(40)),
                                         leg("201", "Palmer Anytime Assist", "CHE", iso_in(40))])
    asyncio.run(bot._check_lineups())
    [(_, text)] = sent
    assert "they all start ✅" in text and "<@" not in text  # nobody logged a bet on it
    asyncio.run(bot.espn.close())


def test_players_ruled_out_in_sports_without_lineups(tmp_path):
    avail = [Availability(frozenset())]
    bot, sent = lineup_bot(tmp_path, avail)
    start = iso_in(60)
    nba = Leg("Jalen Brunson 25+ Points", 0.6, "", "Celtics @ Knicks", "n1", "77", "prop", "points", 25, None, "nba",
              "", "Jalen Brunson", "NY", start)
    bot.parlays.record(5, "nba", "Safe", [nba])
    asyncio.run(bot._check_lineups())
    assert sent == []
    avail[0] = Availability(frozenset({"Jalen Brunson"}))
    asyncio.run(bot._check_lineups())
    asyncio.run(bot._check_lineups())
    [(channel, text)] = sent
    assert channel == 5 and text.startswith("🩹 **Pick ruled out** for Celtics @ Knicks")
    assert "❌ Jalen Brunson 25+ Points: **ruled out** (injury report)" in text
    asyncio.run(bot.espn.close())


def test_slip_note_says_whether_lineups_are_confirmed(tmp_path):
    from sportsbot.bot import register_commands
    bot = make_bot(tmp_path)
    register_commands(bot)
    avail = {"g1": availability(summary(True, True)), "g2": availability(summary(False, False))}

    async def get(league, gid, path=""):
        return avail[gid]
    bot._availability = get
    note = bot._lineup_note_for_tests
    assert "confirmed lineups" in asyncio.run(note([leg("100", "Saka Anytime Assist")]))
    both = asyncio.run(note([leg("100", "Saka Anytime Assist"), leg("300", "X Anytime Assist", game_id="g2")]))
    assert "1 of 2 games' lineups are out" in both and "isn't starting" in both
    assert "Lineups aren't out yet" in asyncio.run(note([leg("300", "X Anytime Assist", game_id="g2")]))
    nba = Leg("Brunson 25+ Points", 0.6, "", "x", "n1", "77", league="nba")
    assert "ruled out" in asyncio.run(note([nba]))
    ml = Leg("Knicks Moneyline", 0.6, "", "x", "n1", kind="moneyline", league="nba")
    assert asyncio.run(note([ml])) == ""
    asyncio.run(bot.espn.close())
