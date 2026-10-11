"""Who started, and how many shots the opponent gives up: real ESPN match summaries, schedules and game logs
(trimmed), the on-disk match history, and the opponent adjustment for total-shots props. No network."""

import asyncio
import copy
import json
import random
from datetime import datetime, timedelta, timezone
from math import exp
from pathlib import Path

import pytest

from sportsbot import shotmodel as S
from sportsbot.props import PlayerGame, Rate, _slim, parse_gamelog

DATA = Path(__file__).parent / "data"
VIT, FLA = "3457", "819"  # Vitória 1-? Flamengo, Brasileirão, 11 Feb 2026 (event 401840993)
KAYZER, MARINHO, UNUSED = "227314", "198392", "203887"  # a starter, a sub who came on, a sub who didn't


def load(name):
    return json.loads((DATA / name).read_text())


# ---------- parsing ----------

def test_a_real_summary_gives_each_side_its_shots_venue_and_lineup():
    summary = load("espn_summary_bra1_401840993.json")
    vit = S.parse_team_game(summary, VIT)
    assert (vit.event_id, vit.date, vit.home, vit.shots_for, vit.shots_against) == (
        "401840993", "2026-02-11T00:30Z", True, 10, 6)
    assert len(vit.starters) == 11 and KAYZER in vit.starters and MARINHO not in vit.starters
    assert len(vit.subs) == 5 and MARINHO in vit.subs and UNUSED not in vit.subs | vit.starters
    fla = S.parse_team_game(summary, int(FLA))  # ids as numbers work too
    assert (fla.home, fla.shots_for, fla.shots_against, len(fla.starters)) == (False, 6, 10, 11)
    assert not fla.starters & vit.starters
    assert S.parse_team_game(summary, "999") is None  # not in this match


def test_a_summary_missing_parts_keeps_what_it_has():
    summary = load("espn_summary_bra1_401840993.json")
    no_box = {k: v for k, v in summary.items() if k != "boxscore"}
    g = S.parse_team_game(no_box, VIT)
    assert (g.shots_for, g.shots_against, len(g.starters)) == (None, None, 11)
    no_lineup = {k: v for k, v in summary.items() if k != "rosters"}
    g = S.parse_team_game(no_lineup, VIT)
    assert (g.shots_for, g.shots_against, g.starters, g.subs) == (10, 6, frozenset(), frozenset())
    assert S.parse_team_game({"header": summary["header"]}, VIT) is None  # nothing to learn from it
    assert S.parse_team_game({}, VIT) is None


def test_schedule_is_finished_matches_newest_first():
    data = load("espn_schedule_bra1_3457.json")
    expected = [(e["id"], e["date"]) for e in data["events"]]
    shuffled = copy.deepcopy(data)
    random.Random(4).shuffle(shuffled["events"])
    upcoming, postponed = copy.deepcopy(data["events"][0]), copy.deepcopy(data["events"][1])
    upcoming.update(id="1", date="2026-10-20T19:00Z")
    upcoming["competitions"][0]["status"]["type"].update(state="pre", completed=False, name="STATUS_SCHEDULED")
    postponed.update(id="2", date="2026-10-01T19:00Z")
    postponed["competitions"][0]["status"]["type"].update(state="post", completed=False, name="STATUS_POSTPONED")
    shuffled["events"] += [upcoming, postponed]
    assert S.parse_schedule(shuffled) == expected
    assert expected[0] == ("401841250", "2026-10-07T23:00Z") and len(expected) == 8
    assert S.parse_schedule({}) == [] and S.parse_schedule({"events": [{"id": "3"}]}) == []


def test_game_logs_say_home_or_away_and_slimming_keeps_it():
    data = load("espn_gamelog_bra1_227314.json")
    games, _ = parse_gamelog(data)
    assert [(g.event_id, g.home) for g in games] == [
        ("401841250", True), ("401841242", True), ("401841235", False), ("401841222", True), ("401841216", False),
        ("401841200", True)]  # "vs" is home, "@" is away
    slim, _ = _slim((games, False), "https://site.web.api.espn.com/apis/common/v3/sports/soccer/bra.1/athletes/1")
    assert [g.home for g in slim] == [g.home for g in games] and "foulsSuffered" not in slim[0].stats
    assert slim[1].stats["totalShots"] == 1 and slim[1].event_id == "401841242"
    data["events"]["401841250"]["atVs"] = "at"  # anything else: unknown
    del data["events"]["401841242"]["atVs"]
    games, _ = parse_gamelog(data)
    assert [g.home for g in games[:3]] == [None, None, False]
    assert PlayerGame("2026-01-01", "2026", "FLA", {}).home is None  # older callers are unaffected


# ---------- the match history ----------

def iso(days_ago: float) -> str:
    return f"{datetime.now(timezone.utc) - timedelta(days=days_ago):%Y-%m-%dT%H:%MZ}"


def match(eid, date, home, away, home_shots=12, away_shots=9, box=True):
    """A summary in ESPN's shape: two sides, total shots, 11 starters and 3 subs each (ids from the team id)."""
    def roster(tid):
        return {"team": {"id": tid}, "roster": [{"athlete": {"id": f"{tid}{i:02d}"}, "starter": i < 11,
                                                 "subbedIn": 11 <= i < 14} for i in range(16)]}
    summary = {"header": {"id": eid, "competitions": [{"id": eid, "date": date, "competitors": [
        {"id": home, "homeAway": "home"}, {"id": away, "homeAway": "away"}]}]},
        "rosters": [roster(home), roster(away)]}
    if box:
        summary["boxscore"] = {"teams": [
            {"team": {"id": home}, "statistics": [{"name": "totalShots", "displayValue": str(home_shots)}]},
            {"team": {"id": away}, "statistics": [{"name": "totalShots", "displayValue": str(away_shots)}]}]}
    return summary


def schedule(matches, team, year=2026, extra=()):
    events = [{"id": m["header"]["id"], "date": m["header"]["competitions"][0]["date"],
               "competitions": [{"status": {"type": {"state": "post", "completed": True}}}]}
              for m in matches if team in [c["id"] for c in m["header"]["competitions"][0]["competitors"]]]
    return {"requestedSeason": {"year": year}, "events": events + list(extra)}


class FakeESPN:
    """Answers schedule and summary requests from dicts, counting them and how many summaries run at once."""

    def __init__(self, matches, schedules=None, fail=()):
        self.matches = {m["header"]["id"]: m for m in matches}
        self.schedules = schedules or {}  # (team id, season) -> schedule
        self.fail = set(fail)
        self.calls = []
        self.active = self.peak = 0

    async def _get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/schedule"):
            team = url.split("/teams/")[1].split("/")[0]
            return self.schedules.get((team, (params or {}).get("season")), {"events": []})
        eid = params["event"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if eid in self.fail:
                raise RuntimeError("ESPN hiccup")
            return self.matches[eid]
        finally:
            self.active -= 1

    def summaries(self):
        return [p["event"] for url, p in self.calls if url.endswith("/summary")]


def league_of_matches():
    """Team 10 plays 12 matches (newest first ids m0..m11); m0-m2 are against team 20."""
    out = []
    for i in range(12):
        opp = "20" if i < 3 else f"3{i}"
        home, away = ("10", opp) if i % 2 == 0 else (opp, "10")
        out.append(match(f"m{i}", iso(3 + 4 * i), home, away, 10 + i, 8))
    return out


def test_history_is_read_once_kept_on_disk_and_reused_after_a_restart(tmp_path):
    matches = league_of_matches()
    schedules = {("10", None): schedule(matches, "10"), ("20", None): schedule(matches, "20")}
    espn = FakeESPN(matches, schedules, fail={"m4"})
    path = tmp_path / "history.json"
    history = S.MatchHistory(espn, path)

    async def first():
        return await asyncio.gather(history.recent("soccer/bra.1", "10", n=10),
                                    history.recent("soccer/bra.1", "20", n=10))
    ten, twenty = asyncio.run(first())
    # The newest 10 matches less the one ESPN failed on, newest first; at most 4 summaries at once.
    assert [g.event_id for g in ten] == ["m0", "m1", "m2", "m3", "m5", "m6", "m7", "m8", "m9"]
    assert 1 < espn.peak <= S.SUMMARIES_AT_ONCE
    assert [g.home for g in ten[:3]] == [True, False, True] and (ten[0].shots_for, ten[0].shots_against) == (10, 8)
    assert (ten[1].shots_for, ten[1].shots_against) == (8, 11) and "1000" in ten[0].starters
    # Team 20's side of the shared matches came from the same summaries: each match was read once.
    assert [g.event_id for g in twenty] == ["m0", "m1", "m2"] and twenty[0].shots_for == 8
    assert sorted(espn.summaries()) == sorted(f"m{i}" for i in range(10))
    saved = json.loads(path.read_text())
    assert "m0:10" in saved and "m0:20" in saved and "m4:10" not in saved
    assert saved["m0:10"]["starters"][0] == "1000" and saved["m0:10"]["for"] == 10

    # Within 6 hours the schedule isn't read again; the failed match is tried again only after a while.
    before = len(espn.calls)
    assert len(asyncio.run(history.recent("soccer/bra.1", "10", n=10))) == 9 and len(espn.calls) == before

    # After a restart only the schedule and the match that failed are read.
    espn2 = FakeESPN(matches, schedules)
    again = asyncio.run(S.MatchHistory(espn2, path).recent("soccer/bra.1", "10", n=10))
    assert [g for g in again if g.event_id != "m4"] == ten and espn2.summaries() == ["m4"]
    assert "m4:10" in json.loads(path.read_text())


def test_a_short_season_is_topped_up_with_the_last_one(tmp_path):
    new = [match(f"n{i}", iso(2 + i), "10", f"4{i}") for i in range(3)]
    old = [match(f"o{i}", iso(150 + i), f"5{i}", "10") for i in range(9)]
    schedules = {("10", None): schedule(new, "10", year=2026), ("10", 2025): schedule(old, "10", year=2025)}
    espn = FakeESPN(new + old, schedules)
    games = asyncio.run(S.MatchHistory(espn, tmp_path / "h.json").recent("soccer/eng.1", "10", n=5))
    assert [g.event_id for g in games] == ["n0", "n1", "n2", "o0", "o1"]
    assert {(u.split("/teams/")[1], p.get("season")) for u, p in espn.calls if u.endswith("/schedule")} == {
        ("10/schedule", None), ("10/schedule", 2025)}
    # Enough this season: last season isn't asked for.
    espn = FakeESPN(new + old, schedules)
    asyncio.run(S.MatchHistory(espn, tmp_path / "h2.json").recent("soccer/eng.1", "10", n=3))
    assert [p for u, p in espn.calls if u.endswith("/schedule")] == [{}]


def test_old_matches_are_dropped_and_a_bad_file_or_schedule_does_no_harm(tmp_path):
    path = tmp_path / "history.json"
    stale = {"date": iso(500), "home": True, "for": 9, "against": 9, "starters": ["1"], "subs": []}
    path.write_text(json.dumps({"old:10": stale, "bad:10": {"home": True}, "m0:30": {**stale, "date": iso(30)}}))
    history = S.MatchHistory(FakeESPN([]), path)
    assert set(history._games) == {"old:10", "m0:30"}  # the damaged entry is skipped
    matches = [match("m1", iso(2), "10", "30")]
    history.espn = FakeESPN(matches, {("10", None): schedule(matches, "10")})
    asyncio.run(history.recent("soccer/bra.1", "10"))
    assert set(json.loads(path.read_text())) == {"m0:30", "m1:10", "m1:30"}  # older than 400 days: pruned

    path.write_text("[1, 2")  # a damaged file is set aside and the history starts empty
    assert S.MatchHistory(FakeESPN([]), path)._games == {}
    assert list(tmp_path.glob("history.json.damaged-*"))

    class Down(FakeESPN):
        async def _get_json(self, url, params=None):
            raise RuntimeError("ESPN is down")
    assert asyncio.run(S.MatchHistory(Down([]), tmp_path / "x.json").recent("soccer/bra.1", "10")) == []


def test_a_match_that_just_ended_without_a_box_score_is_not_saved(tmp_path):
    fresh, settled = match("m1", iso(0.05), "10", "20", box=False), match("m2", iso(2), "10", "30", box=False)
    espn = FakeESPN([fresh, settled], {("10", None): schedule([fresh, settled], "10")})
    path = tmp_path / "history.json"
    games = asyncio.run(S.MatchHistory(espn, path).recent("soccer/bra.1", "10"))
    assert [(g.event_id, g.shots_for, len(g.starters)) for g in games] == [("m1", None, 11), ("m2", None, 11)]
    assert set(json.loads(path.read_text())) == {"m2:10", "m2:30"}  # ESPN may still fill m1's in


# ---------- the model ----------

def pg(i, shots=2.0, home=None, eid=None):
    return PlayerGame(f"2026-09-{28 - i:02d}", "2026", "OPP", {"totalShots": float(shots)}, eid or f"e{i}", home)


def past(eid, starters=(), shots_for=12, shots_against=12, home=True):
    return S.PastGame(eid, "2026-09-01T19:00Z", home, shots_for, shots_against, frozenset(starters), frozenset())


def test_starts_only_drops_games_off_the_bench():
    games = [pg(i) for i in range(10)]
    history = [past(f"e{i}", ["7"] if i % 3 else ["8"]) for i in range(8)]  # benched in e0, e3, e6; e8-e9 unknown
    assert [g.event_id for g in S.starts_only(games, history, "7")] == ["e1", "e2", "e4", "e5", "e7", "e8", "e9"]
    assert [g.event_id for g in S.starts_only(games, history, 7)] == ["e1", "e2", "e4", "e5", "e7", "e8", "e9"]
    # Games for another team (or older than the history) are kept; so are matches with no lineup on ESPN.
    assert S.starts_only(games, [past("x1", ["8"]), past("e0", [])], "7") == games
    # Fewer than 5 starts left: too few to judge by, so the full record is used.
    benched = [past(f"e{i}", ["8"]) for i in range(6)]
    assert S.starts_only(games, benched, "7") == games
    assert len(S.starts_only(games, benched[:5], "7")) == 5
    # Both teams' histories together: either side's lineup counts.
    both = [past("e0", ["8"]), past("e0", ["7"], home=False)]
    assert S.starts_only(games, both, "7") == games


def test_league_average():
    a = [past("1", shots_for=10), past("2", shots_for=14), past("3", shots_for=None)]
    b = [past("1", shots_for=18, home=False)]
    assert S.league_average([]) == 12.0 == S.league_average([[], [past("9", shots_for=None)]])
    assert S.league_average([a, b]) == 14.0
    assert S.league_average(a + b) == 14.0  # a flat list works too
    assert S.league_average([a, a, b]) == 14.0  # the same team's match counts once


def test_opponent_factor_is_shrunk_and_clamped():
    def conceding(*shots):
        return [past(f"g{i}", shots_against=s) for i, s in enumerate(shots)]
    assert S.opponent_factor(conceding(18, 18, 18), 12.0) == 1.0  # fewer than 4 games
    assert S.opponent_factor(conceding(15, 15, 15, 15), 12.0) == pytest.approx(1 + 0.25 * 4 / 10)
    assert S.opponent_factor(conceding(15, 15, 15, 15, None, None, None), 12.0) == pytest.approx(1.1)
    assert S.opponent_factor(conceding(*[15] * 12), 12.0) == pytest.approx(1 + 0.25 * 12 / 18)
    assert S.opponent_factor(conceding(*[24] * 10), 12.0) == 1.25  # 2x shrinks to 1.625: capped
    assert S.opponent_factor(conceding(*[6] * 30), 12.0) == 0.8  # half shrinks to 0.58: floored
    assert S.opponent_factor(conceding(9, 9, 9, 9), 12.0) == pytest.approx(0.9)
    assert S.opponent_factor(conceding(15, 15, 15, 15), 0) == 1.0


def test_poisson_tail():
    assert S.poisson_tail(2.0, 1) == pytest.approx(1 - exp(-2))
    assert S.poisson_tail(2.0, 3) == pytest.approx(1 - exp(-2) * (1 + 2 + 2))
    assert S.poisson_tail(2.0, 2.5) == S.poisson_tail(2.0, 3)
    assert S.poisson_tail(2.0, 0) == 1.0 and S.poisson_tail(0, 1) == 0.0
    tails = [S.poisson_tail(lam / 2, 2) for lam in range(1, 12)]
    assert tails == sorted(tails) and all(0 < t < 1 for t in tails)


def test_adjust_follows_the_factor_and_stays_in_range():
    games = [pg(i, s) for i, s in enumerate([3, 2, 1, 4, 2, 0, 3, 2, 1, 2])]  # 2.0 shots a game
    base = 0.6
    up, down = S.adjust(base, games, 2, 1.2), S.adjust(base, games, 2, 0.85)
    assert down < base < up
    expected = base * S.poisson_tail(2.4, 2) / S.poisson_tail(2.0, 2)
    assert up == pytest.approx(expected)
    # Higher lines move more: 4+ shots needs the extra chances more than 1+ does.
    assert S.adjust(0.2, games, 4, 1.2) / 0.2 > S.adjust(0.85, games, 1, 1.2) / 0.85
    rising = [S.adjust(base, games, 2, f) for f in (0.8, 0.9, 1.0, 1.1, 1.25)]
    assert rising == sorted(rising) and rising[2] == base
    assert S.adjust(0.95, games, 2, 1.25) == 0.97 and S.adjust(0.03, games, 5, 0.8) == 0.02
    assert S.adjust(base, [], 2, 1.2) == base and S.adjust(base, [pg(0, 0)], 2, 1.2) == base
    assert S.adjust(base, games, 2, 1.0) == base


def test_venue_rate():
    games = [pg(0, 3, True), pg(1, 0, True), pg(2, 2, False), pg(3, 4, True), pg(4, 1, False), pg(5, 5, None)]
    assert S.venue_rate(games, 2, True) == Rate(2, 3)
    assert S.venue_rate(games, 2, False) == Rate(1, 2)  # the game with no venue counts for neither
    assert S.venue_rate(games, 1, False, stat="shotsOnTarget") == Rate(0, 2)
    assert S.venue_rate([], 1, True) == Rate(0, 0)
