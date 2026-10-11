"""🚨 Red alerts: DraftKings' total-shots prices (a real sample via ESPN), a player's record against them, the
matchup (starts only, home or away, the opponent), lineup-confirmed looks, the morning post's singles, parlay and
lotto recorded for grading, and the weekly report and edge tuning built from the graded posts. No network."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sportsbot import redalerts as R
from sportsbot import shotmodel as S
from sportsbot.bot import SportsBot, register_commands
from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.limits import DESCRIPTION, TOTAL
from sportsbot.props import Availability, Lineup, PlayerGame
from sportsbot.storage import SubscriptionStore

DATA = Path(__file__).parent / "data"


def test_draftkings_total_shots_prices():
    odds = R.parse_shot_odds(json.loads((DATA / "dk_propbets_shots.json").read_text()))
    assert set(odds) == {"21990", "92800"}  # shots on target is another market: left out
    assert {line: p.american for line, p in odds["21990"].items()} == {1: "+105", 2: "+550", 3: "+2500"}
    p = odds["92800"][3]
    assert (p.line, p.american, p.decimal) == (3, "+120", 2.2) and round(p.implied, 3) == 0.455
    assert R.parse_shot_odds({}) == {} and R.parse_shot_odds({"items": [{"type": {"name": "Shots Milestones"}}]}) == {}


def game(gid="1", home="Roma", away="Como", start=None, league="seriea"):
    start = start or datetime.now(timezone.utc) + timedelta(hours=2)
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": "10", "abbreviation": home[:3].upper(),
                                                                         "displayName": home}},
                            {"homeAway": "away", "score": "0", "team": {"id": "20", "abbreviation": away[:3].upper(),
                                                                         "displayName": away}}]}
    [g] = parse_scoreboard({"events": [{"id": gid, "date": f"{start:%Y-%m-%dT%H:%MZ}", "competitions": [comp]}]},
                           LEAGUES[league])
    return g


def log_of(shots, season="2026-27", opponent="COM", home=None, events=None):
    """A game log, newest first. home: one value for every game, or one per game; events: their ids."""
    homes = home if isinstance(home, (list, tuple)) else [home] * len(shots)
    return [PlayerGame(f"2026-10-{30 - i:02d}", season, opponent, {"totalShots": float(s)},
                       events[i] if events else "", homes[i]) for i, s in enumerate(shots)]


def test_alerts_need_a_record_dk_underprices():
    g = game()
    prices = {2: R.Price(2, "+175", 2.75), 3: R.Price(3, "+400", 5.0), 1: R.Price(1, "-300", 1.33)}
    games = log_of([3, 2, 4, 2, 0, 3, 2, 1, 2, 3])
    [a] = R.alerts_for("Bryan Cristante", "169213", "ROM", "COM", g, games, prices)
    # 2+ shots in 8 of 10 vs DK's 36%: the best line; 1+ is too short a price, 3+ hit only 4 of 10.
    assert (a.line, a.price.american) == (2, "+175") and a.chance > 0.7 and a.edge > 0.9
    assert a.pick == "Bryan Cristante 2+ Shots" and a.evidence.startswith("L10 8/10 · 2026-27 8/10 · vs COM 8/10")
    leg = a.leg()
    assert (leg.stat, leg.line, leg.player_id, leg.league) == ("totalShots", 2, "169213", "seriea")
    assert R.alerts_for("X", "1", "ROM", "COM", g, log_of([1, 0, 1, 0, 0, 2, 0, 1]), prices) == []  # cold
    assert R.alerts_for("X", "1", "ROM", "COM", g, log_of([3, 3]), prices) == []  # too few games
    fair = {2: R.Price(2, "-300", 1.33)}
    assert R.alerts_for("X", "1", "ROM", "COM", g, games, fair) == []  # priced right: no edge


def test_spread_and_prices():
    g1, g2 = game("1"), game("2", "Lazio", "Monza")
    mk = lambda gm, edge: SimpleNamespace(game=gm, edge=edge)  # noqa: E731
    alerts = [mk(g1, 0.9), mk(g1, 0.8), mk(g1, 0.7), mk(g2, 0.5)]
    assert [(a.game.id, a.edge) for a in R.spread(alerts, 3, per_game=2)] == [("1", 0.9), ("1", 0.8), ("2", 0.5)]
    assert [a.game.id for a in R.spread(alerts, 3)] == ["1", "2"]
    assert R.american_of(2.75) == "+175" and R.american_of(1.5) == "-200"


def test_morning_red_alerts_post_singles_parlay_and_lotto(tmp_path, monkeypatch):
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 10, None)
    register_commands(bot)
    sent = []

    async def send(channel_id, embed=None, content=None, view=None):
        sent.append((channel_id, embed, content, view))
    bot._send = send
    now = datetime.now(timezone.utc)
    tz = timezone(timedelta(hours=1 - now.hour, minutes=-now.minute))
    games = [game(str(i), f"Home{i}", f"Away{i}", now + timedelta(hours=2)) for i in range(5)]

    async def week_games(key):
        return games if key == "seriea" else []
    bot.week_games = week_games

    async def game_alerts(self, g, confirmed_only=False):
        price = R.Price(2, "+150", 2.5)
        return [R.Alert(f"Player {g.id}", f"p{g.id}", g.home.abbrev, g.away.abbrev, g, price, 0.6 + int(g.id) / 100,
                        R.Rate(7, 10), R.Rate(5, 7), R.Rate(10, 20), R.Rate(0, 0), "2026-27", "2025-26")]
    monkeypatch.setattr(R.RedAlerts, "game_alerts", game_alerts)

    async def no_lineups(league, game_id, path=""):  # the slip's lineup note asks ESPN: not in a test
        raise RuntimeError("offline")
    bot._availability = no_lineups
    posted = asyncio.run(bot.auto_post(9, "redalerts", tz))
    embeds = [e for _, e, _, _ in sent if e is not None]
    assert posted == 3 and [e.title.split(":")[0] for e in embeds] == [
        f"🚨 Red alerts · {datetime.now(tz).date():%A %B} {datetime.now(tz).date().day}", "🚨 Red alert parlay",
        "🚨 Red alert lotto"]
    assert "Player 4 2+ Shots** · DK **+150**" in embeds[0].description  # best edge first
    slips = [(c, v) for _, e, c, v in sent if e is None and not c.startswith("💪")]
    assert slips and all(c.startswith("📋 Copy for DraftKings") and v for c, v in slips)
    [ping] = [c for _, e, c, v in sent if e is None and c.startswith("💪")]  # strong alerts: one ping line
    assert ping.startswith("💪 Strong red alert")
    styles = {p["style"]: p for p in bot.parlays.pending()}
    assert styles["Red alert singles"]["round_robin"] == 1 and len(styles["Red alert singles"]["legs"]) == 5
    assert len(styles["Red alert parlay"]["legs"]) == 3 and len(styles["Red alert lotto"]["legs"]) == 5
    assert all(leg["stat"] == "totalShots" for p in styles.values() for leg in p["legs"])
    assert all(cid == 9 for cid, *_ in sent)

    sent.clear()  # the afternoon look only posts what's new
    assert asyncio.run(bot.auto_post(9, "redalerts-late", tz)) == 0 and sent == []
    bot.week_games = lambda key: asyncio.sleep(0, result=[])
    assert asyncio.run(bot.auto_post(9, "redalerts", tz)) == 0 and sent == []  # nothing today: quiet
    asyncio.run(bot.espn.close())


# ---------- stakes and 💪 ----------

def alert(chance, decimal, l10=R.Rate(10, 10), line=2, **kw):
    return R.Alert("Paulo Dybala", "1", "ROM", "COM", game(), R.Price(line, R.american_of(decimal), decimal), chance,
                   l10, R.Rate(8, 10), R.Rate(0, 0), R.Rate(0, 0), "2026-27", "", **kw)


def test_stake_is_a_quarter_kelly_in_units_of_1_percent():
    # Kelly (p*d - 1) / (d - 1), a quarter of it, as % of the bankroll, to the nearest 0.25u, from 0.25u to 2u.
    assert alert(0.50, 2.1).stake == 1.25  # Kelly 4.5%: 1.14u
    assert alert(0.30, 4.0).stake == 1.75  # Kelly 6.7%: 1.67u
    assert alert(0.21, 5.5).stake == 0.75  # Kelly 3.4%: 0.86u
    assert alert(0.20, 5.2).stake == 0.25  # Kelly 1%: 0.24u, the least
    assert alert(0.30, 2.0).stake == 0.25  # no edge at all: still the least, never 0 or less
    assert alert(0.75, 2.75).stake == 2.0  # Kelly 61%: capped at 2u


def test_strong_needs_a_big_edge_a_full_record_and_a_likely_bet():
    assert alert(0.62, 2.5).strong  # edge +55%, 10 games, 62%
    assert not alert(0.62, 2.5, l10=R.Rate(9, 9)).strong  # 9 games
    assert not alert(0.58, 2.5).strong  # edge +45%
    assert not alert(0.50, 3.2).strong  # edge +60%, but a coin flip


# ---------- the matchup ----------

PRICES = {2: R.Price(2, "+175", 2.75)}


def test_the_opponent_moves_the_chance_its_way():
    g, games = game(), log_of([3, 2, 4, 2, 0, 3, 2, 1, 2, 3])
    [base] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES)
    [up] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES, factor=1.2)
    [down] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES, factor=0.9)
    assert down.chance < base.chance < up.chance and (down.factor, base.factor, up.factor) == (0.9, 1.0, 1.2)
    assert up.chance == pytest.approx(S.adjust(base.chance, games[:10], 2, 1.2))
    assert base.matchup == "" and up.matchup == "opponent allows +20% shots" and "-10%" in down.matchup
    assert R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES, factor=0.6) == []  # a wall: no longer worth it
    [few] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES, factor=1.02, starts=9)
    assert few.matchup == "9 games, bench left out"  # under 3% isn't worth a mention


def test_his_home_or_away_record_counts_a_fifth():
    g = game()
    shots = [3, 2, 3, 2, 2, 1, 3, 2, 2, 0, 3, 2]  # at home 6/6 at 2+, away 4/6
    games = log_of(shots, home=[i % 2 == 0 for i in range(12)])
    [neutral] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES)
    [home] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES, home=True)
    [away] = R.alerts_for("X", "1", "COM", "ROM", g, games, PRICES, home=False)
    assert away.chance < neutral.chance < home.chance and neutral.venue is None
    assert home.chance == pytest.approx(0.8 * neutral.chance + 0.2 * 7 / 8)
    assert (home.venue, away.venue) == (R.Rate(6, 6), R.Rate(4, 6))
    assert home.evidence.endswith("home 6/6") and away.evidence.endswith("away 4/6")
    # Only 4 home games known: shown, not counted.
    games = log_of(shots, home=[True] * 4 + [None] * 8)
    [few] = R.alerts_for("X", "1", "ROM", "COM", g, games, PRICES, home=True)
    assert few.chance == neutral.chance and few.venue == R.Rate(4, 4)


def test_a_league_can_ask_for_a_bigger_or_smaller_edge():
    g, games = game(), log_of([2, 2, 0, 2, 1, 2, 0, 2, 1, 2])  # 2+ in 6 of 10: ~58%
    price = {2: R.Price(2, "-109", 1.92)}  # edge ~ +12%
    assert R.alerts_for("X", "1", "ROM", "COM", g, games, price) == []  # under the usual 15%
    [a] = R.alerts_for("X", "1", "ROM", "COM", g, games, price, min_edge=0.10)
    assert a.edge == pytest.approx(0.12)
    hot = log_of([3, 2, 4, 2, 0, 3, 2, 1, 2, 3])
    [b] = R.alerts_for("X", "1", "ROM", "COM", g, hot, PRICES, min_edge=0.25)
    assert R.alerts_for("X", "1", "ROM", "COM", g, hot, PRICES, min_edge=b.edge + 0.01) == []


def past(eid, starters=(), shots_for=12, shots_against=12, home=True):
    return S.PastGame(eid, "2026-10-01T19:00Z", home, shots_for, shots_against, frozenset(starters), frozenset())


def test_bench_games_are_left_out_when_the_lineups_cover_them():
    games = log_of([3] * 12, events=[f"e{i}" for i in range(12)])
    history = [past(f"e{i}", ["9"] if i in (1, 2) else ["7", "9"]) for i in range(10)]
    kept, starts = R.without_bench(games, history, "7")
    assert [g.event_id for g in kept] == ["e0"] + [f"e{i}" for i in range(3, 12)] and starts == 10
    assert R.without_bench(games, [], "7") == (games, 0)  # no lineups
    assert R.without_bench(games, [past("x", ["7"])], "7") == (games, 0)  # they don't cover his games
    assert R.without_bench(games, history, "9") == (games, 12)  # started them all
    benched = [past(f"e{i}", ["9"] if i > 1 else ["7"]) for i in range(12)]
    assert R.without_bench(games, benched, "7") == (games, 0)  # 2 starts: too few to go by
    assert R.shots_average([[past("a", shots_for=10, shots_against=20)], [past("b", shots_for=14,
                                                                                 shots_against=4)]]) == 12


# ---------- a game's alerts ----------

def propbets(prices):
    """DraftKings' total-shots market in ESPN's shape: {athlete id: {line: (american, decimal)}}."""
    return {"items": [{"athlete": {"$ref": f"http://sports.core.api.espn.com/v2/athletes/{aid}?lang=en"},
                       "type": {"name": "Shots Milestones"},
                       "current": {"over": {"american": am, "decimal": d}, "target": {"value": float(line)}}}
                      for aid, lines in prices.items() for line, (am, d) in lines.items()]}


H1, H2, A1 = "101", "102", "201"  # two Roma players (the second a sub) and one of Como's


class FakeESPN:
    def __init__(self, prices):
        self.prices, self.calls = prices, []

    async def _get_json(self, url, params=None):
        self.calls.append(url)
        return propbets(self.prices)


class FakeProps:
    def __init__(self, rosters, logs):
        self.rosters, self.logs, self.keys = rosters, logs, []

    async def cached(self, key, ttl, make, keep=None):
        self.keys.append((key, ttl))
        return await make()

    async def _roster(self, path, team_id):
        return self.rosters.get(str(team_id), {})

    async def player_games(self, path, aid, fresh=False):
        return self.logs[aid], False


class FakeHistory:
    def __init__(self, by_team):
        self.by_team, self.calls = by_team, []

    async def recent(self, league_path, team_id, n=10):
        self.calls.append((league_path, str(team_id), n))
        return self.by_team.get(str(team_id), [])


HOT = [3, 2, 4, 2, 2, 3, 2, 1, 2, 3, 3, 2]
ROSTERS = {"10": {H1: ("Home Starter", "F"), H2: ("Home Sub", "F")}, "20": {A1: ("Away Starter", "F")}}
DK = {aid: {1: ("-300", 1.33), 2: ("+175", 2.75)} for aid in (H1, H2, A1)}
DK[H2][1] = ("-500", 1.2)  # DraftKings' likeliest shooter of all


def lineups(*teams):
    out = {"10": Lineup(frozenset({H1}), frozenset({H1, H2}), "ROM"),
           "20": Lineup(frozenset({A1}), frozenset({A1}), "COM")}
    return Availability(lineups={tid: out[tid] for tid in teams})


def red(available, history=None, edges=None, logs=None):
    async def availability(league, gid, path):
        if isinstance(available, Exception):
            raise available
        return available
    props = FakeProps(ROSTERS, logs or {aid: log_of(HOT) for aid in (H1, H2, A1)})
    return R.RedAlerts(FakeESPN(DK), props, availability, history, edges), props


def test_lineup_confirmed_alerts_wait_for_both_xis_and_keep_only_starters(monkeypatch):
    g = game()

    def run(available, confirmed_only):
        alerts, props = red(available)
        return asyncio.run(alerts.game_alerts(g, confirmed_only=confirmed_only)), alerts, props
    for available in (lineups("10"), Availability(), RuntimeError("ESPN down")):  # one XI, none, or no word
        found, alerts, _ = run(available, True)
        assert found == [] and alerts.espn.calls == []  # DraftKings isn't even asked
    found, _, props = run(lineups("10", "20"), True)
    assert sorted(a.player_id for a in found) == sorted([A1, H1]) and all(a.confirmed for a in found)
    renamed = lineups("10", "20")
    renamed.lineups["20"] = Lineup(frozenset({A1}), frozenset({A1}), "CFC")  # the summary's abbreviation differs
    assert sorted(a.player_id for a in run(renamed, True)[0]) == sorted([A1, H1])
    assert props.keys == [("dk-shots-fresh:1", R.FRESH_PROPS_TTL)]  # DraftKings' newest prices
    assert "✅ starting" in R.singles_embed(found, "Sunday")[0].description
    # The usual look: everyone until a lineup is out, then that team's starters; nothing marked confirmed.
    found, _, props = run(Availability(), False)
    assert sorted(a.player_id for a in found) == sorted([A1, H1, H2]) and not any(a.confirmed for a in found)
    assert props.keys == [("dk-shots:1", R.PROPS_TTL)]
    found, _, _ = run(lineups("10"), False)
    assert sorted(a.player_id for a in found) == sorted([A1, H1]) and not any(a.confirmed for a in found)
    # A sub doesn't take one of the places, even when DraftKings has him as the likeliest shooter.
    monkeypatch.setattr(R, "PLAYERS_PER_GAME", 2)
    found, _, _ = run(lineups("10", "20"), True)
    assert sorted(a.player_id for a in found) == sorted([A1, H1])


def test_game_alerts_use_the_lineups_and_the_opponent():
    g = game()
    # Roma (10) concede 9 shots a game, Como (20) 18. H1 came off the bench in e1 and e2 (0 shots each).
    h10 = [past(f"e{i}", [H2] if i in (1, 2) else [H1, H2], 12, 9, i % 2 == 0) for i in range(10)]
    h20 = [past(f"c{i}", [A1], 10, 18, i % 2 == 1) for i in range(10)]
    shots = [0 if i in (1, 2) else 3 for i in range(10)] + [2] * 10
    logs = {H1: log_of(shots, events=[f"e{i}" for i in range(10)] + [f"o{i}" for i in range(10)]),
            H2: log_of(HOT), A1: log_of(HOT)}
    history = FakeHistory({"10": h10, "20": h20})
    alerts, _ = red(Availability(), history, logs=logs)
    found = {a.player_id: a for a in asyncio.run(alerts.game_alerts(g))}
    assert sorted(history.calls) == [("soccer/ita.1", "10", 10), ("soccer/ita.1", "20", 10)]
    average = R.shots_average([h10, h20])
    assert average == pytest.approx((12 + 9 + 10 + 18) / 4)
    assert found[H1].factor == pytest.approx(S.opponent_factor(h20, average)) and found[H1].factor > 1
    assert found[A1].factor == pytest.approx(S.opponent_factor(h10, average)) and found[A1].factor < 1
    assert found[H1].starts == 18 and found[H1].l10 == R.Rate(10, 10)  # the two cameos left out
    assert found[H2].starts == 0  # his games aren't in the lineups
    plain, _ = red(Availability(), logs=logs)
    before = {a.player_id: a for a in asyncio.run(plain.game_alerts(g))}
    assert before[H1].l10 == R.Rate(8, 10) and before[H1].factor == 1.0 and before[H1].starts == 0
    assert found[H1].chance > before[H1].chance
    # A tuned edge too high for anything here, and one that fails (the usual 15%).
    tough, _ = red(Availability(), edges=lambda key: 5.0)
    assert asyncio.run(tough.game_alerts(g)) == []
    for edges in (lambda key: {}[key], lambda key: None):  # fails, or nothing tuned for this league
        usual, _ = red(Availability(), edges=edges)
        assert usual.min_edge("seriea") == R.MIN_EDGE and len(asyncio.run(usual.game_alerts(g))) == 3


# ---------- the posts ----------

def test_singles_show_stake_starting_opponent_venue_and_starts():
    a = alert(0.50, 2.1, factor=1.12, starts=38, venue=R.Rate(7, 9), confirmed=True)
    b = alert(0.70, 2.75, factor=0.98)
    embed, slip = R.singles_embed([a, b], "Sunday October 11", confirmed=True)
    assert embed.title == "🚨✅ Lineup-confirmed red alerts · Sunday October 11: 2 shot singles"
    first, second = embed.description.split("\n💪 ")
    assert "**1. Paulo Dybala 2+ Shots** · DK **+110**" in first and "✅ starting" in first
    assert "stake 1.25u" in first and "opponent allows +12% shots" in first and "home 7/9" in first
    assert "38 games, bench left out" in first and not first.startswith("💪")
    assert "stake 2u" in second and "opponent" not in second  # 2% isn't worth a mention
    assert second.startswith("**2. Paulo Dybala")  # strong: 💪
    assert R.singles_embed([b], "Sunday")[0].title == "🚨 Red alerts · Sunday: 1 shot single"
    parlay, _ = R.combo_embed([a, replace(a, game=game("2"))], lotto=False)
    assert parlay.title.startswith("🚨✅ Lineup-confirmed red alert parlay: 2 legs")
    assert "✅ starting" in parlay.description and "stake" not in parlay.description  # stakes are for singles
    mixed, _ = R.combo_embed([a, b], lotto=True)
    assert mixed.title.startswith("🚨 Red alert lotto: 2 legs")
    assert R.strong_line([a]) == "" and R.strong_line([a, b]) == (
        "💪 Strong red alert: **Paulo Dybala 2+ Shots** (DK +175, stake 2u)")


def test_a_big_slate_stays_within_discords_limits():
    many = [alert(0.70, 2.75, factor=1.2, starts=40, venue=R.Rate(9, 12)) for _ in range(60)]
    for embed in (R.singles_embed(many, "Sunday")[0], R.combo_embed(many, lotto=True)[0]):
        assert len(embed.description) <= DESCRIPTION and len(embed) <= TOTAL


# ---------- how they've done ----------

NOW = 1_800_000_000.0
DAY = 86400


def leg(pick, status, american, league="epl", line=2, chance=0.6, game_id=None):
    return {"pick": pick, "probability": chance, "evidence": f"DK {american} · L10 7/10", "league": league,
            "status": status, "stat": "totalShots", "line": line, "game_id": game_id or f"g-{pick}",
            "player_id": f"p-{pick}", "kind": "prop"}


def post(style, legs, days_ago, status="pending", channel=1):
    return {"id": f"{style}{days_ago}{channel}", "channel": channel, "league": legs[0]["league"], "style": style,
            "created": NOW - days_ago * DAY, "legs": legs, "status": status,
            **({"round_robin": 1} if "singles" in style else {})}


def week():
    morning = [leg("A", "hit", "+150", chance=0.6), leg("B", "miss", "+200", line=3, chance=0.4),
               leg("C", "void", "+120"), leg("D", "pending", "+130")]
    confirmed = [leg("E", "hit", "-110", "seriea", line=1, chance=0.7),
                 leg("F", "hit", "+400", "seriea", line=3, chance=0.3),
                 leg("G", "miss", "+250", "seriea", chance=0.5)]
    parlay = [leg("A", "hit", "+150"), leg("E", "hit", "-110", "seriea")]
    return [post("Red alert singles", morning, 2),
            post("Red alert singles", morning, 2, channel=2),  # the same post in another channel: counted once
            post(R.post_style("singles", confirmed=True), confirmed, 1),
            post("Red alert singles", [leg("H", "hit", "+300")], 10),  # last week
            post("Lotto", [leg("I", "hit", "+500")], 1, "won"),  # not a red alert
            post("Red alert parlay", parlay, 2, "won"), post("Red alert parlay", parlay, 2, "won", channel=2),
            post("Red alert lotto", [leg("J", "miss", "+300"), leg("K", "hit", "+200")], 3, "lost"),
            post("Red alert lotto", [leg("L", "miss", "+300")], 4, "lost"),
            post("Red alert parlay", [leg("M", "pending", "+300")], 1)]


def field(embed, name):
    return next(f.value for f in embed.fields if f.name == name)


def test_the_weekly_report():
    embed = R.report_embed(week(), NOW - 7 * DAY, NOW)
    assert embed.title == "🚨 Red alerts this week"
    # A +1.50, B -1, E +0.91, F +4, G -1: 3-2, +4.41u over 5 singles; predicted (.6+.4+.7+.3+.5)/5.
    assert embed.description.split("\n") == [
        "**Singles 3-2** · won 60%, predicted 50% · 1 void",
        "**+4.41u** at DraftKings' prices, 1 unit on each · ROI **+88%**",
        "✅ Lineup-confirmed singles: 2-1 · +3.91u (+130%)"]
    assert field(embed, "By league") == "Serie A: 2-1 · +3.91u (+130%)\nPremier League: 1-1 · +0.50u (+25%)"
    assert field(embed, "By line") == ("1+ shots: 1-0 · +0.91u (+91%)\n2+ shots: 1-1 · +0.50u (+25%)\n"
                                       "3+ shots: 1-1 · +3.00u (+150%)")
    assert field(embed, "By price") == ("-125 to +150: 2-0 · +2.41u (+120%)\n+151 to +300: 0-2 · -2.00u (-100%)\n"
                                        "+301 and longer: 1-0 · +4.00u (+400%)")
    assert field(embed, "Parlays and lottos") == "Parlays 1 won, 0 lost · lottos 0 won, 2 lost"
    assert field(embed, "Auto-tuning").startswith("No changes: every league still needs a 15% edge")
    assert embed.footer.text.endswith("Fri Jan 8 to Fri Jan 15")
    since = datetime.fromtimestamp(NOW - 7 * DAY, timezone.utc)  # datetimes work too
    assert R.report_embed(week(), since, datetime.fromtimestamp(NOW, timezone.utc)).to_dict() == embed.to_dict()
    empty = R.report_embed(week(), NOW - 30 * DAY, NOW - 20 * DAY)
    assert empty.title == "🚨 Red alerts this week" and empty.description.startswith("Nothing graded this week")
    assert R.report_embed([], NOW - 7 * DAY, NOW).description.startswith("Nothing graded this week")


def test_dk_prices_are_read_back_from_the_evidence():
    assert R.dk_decimal("DK +150 · L10 7/10") == 2.5 and R.dk_decimal("DK -125 · L10 7/10") == 1.8
    assert R.dk_decimal("DK EVEN · L10") == 2.0 and R.dk_decimal("L10 7/10") is None
    assert R.dk_decimal("DK  · L10") is None and R.dk_decimal("") is None
    # A price ESPN sends without its American form still gets one, so it can be read back.
    [price] = R.parse_shot_odds(propbets({"5": {2: ("", 2.6)}}))["5"].values()
    assert price.american == "+160" and R.dk_decimal(f"DK {price.american} · L10") == 2.6


def graded(league, hits, misses, days_ago=5, american="+100"):
    legs = [leg(f"{league}{i}", "hit" if i < hits else "miss", american, league) for i in range(hits + misses)]
    return post("Red alert singles", legs, days_ago)


def test_each_leagues_edge_is_tuned_from_its_last_60_days():
    parlays = [graded("epl", 12, 8),  # +20%: a smaller edge will do
               graded("seriea", 8, 12),  # -20%: needs a bigger one
               graded("laliga", 10, 10),  # break-even: the usual
               graded("ligue1", 11, 9),  # exactly +10%
               graded("mls", 9, 11),  # exactly -10%
               graded("bundesliga", 19, 0),  # 19 singles: not enough to go by
               post("Red alert singles", [leg(f"v{i}", "void", "+100", "bundesliga") for i in range(5)], 5),
               graded("brasileirao", 30, 0, days_ago=70),  # too long ago
               post("Red alert parlay", [leg(f"x{i}", "hit", "+100", "uel") for i in range(25)], 5, "won")]
    assert R.tuned_edges(parlays, NOW) == {"epl": 0.10, "seriea": 0.25, "laliga": R.MIN_EDGE, "ligue1": 0.10,
                                           "mls": 0.25}
    assert R.tuned_edges(parlays, NOW, days=3) == {}
    assert R.tuned_edges(parlays, NOW + 100 * DAY) == {}
    assert "brasileirao" in R.tuned_edges(parlays, NOW - 50 * DAY)
    note = R.tuning_note(parlays, NOW - 7 * DAY, NOW)
    assert note.startswith("Edge needed for an alert: ") and "La Liga" not in note  # break-even: unchanged
    assert "Premier League 15% → 10%" in note and "Serie A 15% → 25%" in note
    assert R.tuning_note(parlays, NOW, NOW + DAY).startswith("No changes this week. Edge needed: Ligue 1 10%")
