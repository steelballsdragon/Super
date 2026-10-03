import asyncio

from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.odds import Odds
from sportsbot.props import (Leg, PlayerGame, PropsClient, Rate, best_trends, build_parlay, combined, injured_names,
                             moneyline_leg, parlay_embed, parse_gamelog, seasons_from, trend_legs, trends_embed)


def gamelog(names, rows, season="2026 Regular Season", extra_types=()):
    events = {f"e{i}": {"gameDate": f"2026-0{1 + i // 28}-{1 + i % 28:02d}T00:00:00.000+00:00",
                        "opponent": {"abbreviation": opp}} for i, (opp, _) in enumerate(rows)}
    types = [{"displayName": season, "categories": [{"events": [{"eventId": f"e{i}", "stats": stats} for i, (_, stats) in enumerate(rows)]}]}]
    return {"names": names, "events": events, "seasonTypes": list(extra_types) + types,
            "filters": [{"name": "season", "options": [{"value": "2026"}, {"value": "2025"}]}]}


NBA_NAMES = ["minutes", "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "totalRebounds", "assists", "points"]


def test_parse_gamelog_reads_stats_and_skips_preseason():
    pre = {"displayName": "2026-27 Preseason", "categories": [{"events": [{"eventId": "e0", "stats": ["30", "9-9", "1", "1", "50"]}]}]}
    games, pitcher = parse_gamelog(gamelog(NBA_NAMES, [("UTAH", ["36", "3-7", "12", "10", "28"])], "2025-26 Regular Season", [pre]))
    assert not pitcher and len(games) == 1
    g = games[0]
    assert (g.season, g.opponent) == ("2025-26", "UTAH")
    assert g.stats["threesMade"] == 3 and g.stats["pra"] == 50
    assert seasons_from(gamelog(NBA_NAMES, [])) == ["2026", "2025"]
    _, pitcher = parse_gamelog(gamelog(["innings", "strikeouts"], [("SD", ["6.0", "8"])]))
    assert pitcher


def test_rates_are_adjusted_for_sample_size():
    assert Rate(10, 10).estimate == 11 / 12 and Rate(3, 3).estimate == 0.8 and Rate(0, 0).estimate == 0.5


def nfl_games(rush_yards, season="2026", opp="WSH"):
    names = {"rushingYards": 0, "receptions": 0}
    return [PlayerGame(f"2026-09-{28 - i:02d}", season, opp, {**names, "rushingYards": y, "receptions": 2,
                                                              "anytimeTouchdowns": 0}) for i, y in enumerate(rush_yards)]


def test_best_trend_takes_the_highest_consistent_line():
    games = nfl_games([80, 72, 95, 64, 70, 88, 61, 77, 90, 66]) + nfl_games([70] * 12, season="2025", opp="HOU")
    [rush, rec] = best_trends("Jonathan Taylor", "1", "IND", "WSH", games, "football")
    assert rush.pick == "Jonathan Taylor Over 59.5 Rushing Yards"  # 60+ every game; 75+ only 4/10
    assert rush.evidence == "L10 10/10 · 2026 10/10 · 2025 12/12 · vs WSH 10/10"
    assert 0.9 < rush.probability < 0.95
    assert rec.pick == "Jonathan Taylor Over 1.5 Receptions"


def test_bigger_payout_skips_the_gimme_line():
    games = nfl_games([80, 72, 95, 64, 70, 88, 61, 77, 90, 66]) + nfl_games([70] * 12, season="2025")
    picks = [t.pick for t in best_trends("JT", "1", "IND", "WSH", games, "football", bigger=True)]
    assert picks == ["JT Over 59.5 Rushing Yards"]  # the 2+ receptions line is skipped as a gimme


def test_inconsistent_player_gets_no_trend():
    games = nfl_games([10, 90, 15, 80, 5, 70, 20, 95, 0, 60])
    assert [t for t in best_trends("X", "2", "IND", "WSH", games, "football") if t.prop.label == "Rushing Yards"] == []


def game(home_ml="-205", away_ml="+170", draw=None):
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": "28", "abbreviation": "WSH", "displayName": "Washington Commanders"}},
                            {"homeAway": "away", "score": "0", "team": {"id": "11", "abbreviation": "IND", "displayName": "Indianapolis Colts"}}]}
    [g] = parse_scoreboard({"events": [{"id": "9", "date": "2026-10-04T17:00Z", "competitions": [comp]}]}, LEAGUES["nfl"])
    return g


def test_moneyline_leg_only_for_clear_favorites_and_not_soccer():
    g = game()
    odds = Odds("DraftKings", home_ml="-205", away_ml="+170")
    leg = moneyline_leg(g, {"28": 0.645, "11": 0.355}, odds)
    assert (leg.pick, leg.evidence) == ("Washington Commanders Moneyline", "DraftKings -205 → 64% implied (no-vig)")
    assert moneyline_leg(g, {"28": 0.55, "11": 0.45}, odds) is None
    assert moneyline_leg(g, {"28": 0.6, "11": 0.2, "draw": 0.2}, odds) is None


def leg(pick, p, game_id, player=None):
    return Leg(pick, p, "", f"G{game_id}", game_id, player)


def test_parlay_spreads_legs_across_games_and_players():
    legs = [leg("A1", 0.95, "1", "a"), leg("A2", 0.94, "1", "a"), leg("B1", 0.93, "1", "b"), leg("C1", 0.92, "1", "c"),
            leg("D1", 0.90, "2", "d"), leg("ML", 0.80, "3")]
    chosen = build_parlay(legs, 4)
    assert [l.pick for l in chosen] == ["A1", "B1", "D1", "ML"]  # one leg per player, two per game
    assert round(combined(chosen), 4) == round(0.95 * 0.93 * 0.90 * 0.80, 4)


def test_parlay_embed_and_copyable_slip():
    chosen = [leg("Josh Downs Over 1.5 Receptions", 0.92, "1"), leg("Baltimore Ravens Moneyline", 0.84, "2")]
    embed, slip = parlay_embed("NFL", "🏈", chosen, "Safest")
    assert embed.title == "🎟️ 🏈 NFL parlay: 2 legs (Safest)"
    assert embed.fields[0].value.startswith("**77%**")
    assert slip == "```\nJosh Downs Over 1.5 Receptions\nBaltimore Ravens Moneyline\n```"


def test_injured_players_are_skipped(monkeypatch):
    summary = {"injuries": [{"injuries": [{"status": "Out", "athlete": {"displayName": "Jayden Daniels"}},
                                          {"status": "Questionable", "athlete": {"displayName": "Terry McLaurin"}}]}]}
    assert injured_names(summary) == {"Jayden Daniels"}

    client = PropsClient(espn=None)

    async def players(league, team_id):
        return [("1", "Jayden Daniels", "QB"), ("2", "Terry McLaurin", "WR")] if team_id == "28" else []

    async def games(path, aid):
        return [PlayerGame(f"2026-09-{i + 1:02d}", "2026", "IND", {"receptions": 6, "receivingYards": 80})
                for i in range(10)], False
    client.key_players = players
    client.player_games = games
    trends = asyncio.run(client.game_trends(game(), injured_names(summary)))
    assert {t.player for t in trends} == {"Terry McLaurin"}
    embed = trends_embed(game(), trends, None)
    assert embed.fields[0].name == "Washington Commanders" and "Terry McLaurin Over" in embed.fields[0].value
    assert trend_legs(game(), trends)[0].game == "Indianapolis Colts @ Washington Commanders"
