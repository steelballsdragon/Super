import asyncio

from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.odds import Odds
from sportsbot.props import (Leg, PlayerGame, PropsClient, Rate, TARGETS, american, best_trends, build_to_target, combined, injured_names,
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
    dog = moneyline_leg(g, {"28": 0.64, "11": 0.36}, odds, underdog=True)
    assert (dog.pick, dog.side, dog.probability) == ("Indianapolis Colts Moneyline", "11", 0.36)
    assert moneyline_leg(g, {"28": 0.8, "11": 0.2}, odds, underdog=True) is None  # too long a shot


def leg(pick, p, game_id, player=None):
    return Leg(pick, p, "", f"G{game_id}", game_id, player)


def test_american_odds():
    assert [american(p) for p in (0.5, 0.8, 0.25, 1 / 11, 1 / 101)] == ["+100", "-400", "+300", "+1000", "+10000"]


def test_parlay_spreads_legs_across_games_and_players():
    legs = [leg("A1", 0.95, "1", "a"), leg("A2", 0.94, "1", "a"), leg("B1", 0.93, "1", "b"), leg("C1", 0.92, "1", "c"),
            leg("D1", 0.90, "2", "d"), leg("ML", 0.80, "3")]
    chosen = build_to_target(legs, TARGETS["safe"])
    assert [l.pick for l in chosen][:3] == ["A1", "B1", "D1"]  # one leg per player, two per game
    assert "A2" not in [l.pick for l in chosen] and "C1" not in [l.pick for l in chosen]


def strong_legs(n_games, p, per_game=2):
    return [leg(f"P{g}-{i}", p, str(g), f"p{g}{i}") for g in range(n_games) for i in range(per_game)]


def test_safe_parlay_lands_around_even_money():
    for p in (0.95, 0.9, 0.85, 0.8, 0.7):
        chosen = build_to_target(strong_legs(8, p), TARGETS["safe"])
        assert 0.45 <= combined(chosen) <= 0.55, (p, combined(chosen))
        assert -122 <= int(american(combined(chosen))) <= 122


def test_big_parlay_lands_between_1000_and_10000():
    for p in (0.8, 0.75, 0.7, 0.6, 0.5):  # the higher lines it uses hit less often
        chosen = build_to_target(strong_legs(10, p), TARGETS["big"])
        odds = int(american(combined(chosen)))
        assert 1000 <= odds <= 10000, (p, odds, len(chosen))
    # Only near-certain legs can't reach +1000 within 15 legs: it says how close it got.
    chosen = build_to_target(strong_legs(10, 0.9), TARGETS["big"])
    assert len(chosen) == 15
    assert "Closest I could get is +386" in parlay_embed("NBA", "🏀", chosen, TARGETS["big"])[0].description


def test_mixed_legs_finish_close_to_the_aim():
    legs = [leg(f"L{i}", q, str(i), f"p{i}") for i, q in enumerate((0.92, 0.9, 0.88, 0.75, 0.66, 0.6, 0.55))]
    assert 0.45 <= combined(build_to_target(legs, TARGETS["safe"])) <= 0.55


def test_lotto_lands_between_3000_and_20000_in_4_to_10_legs():
    for p in (0.7, 0.65, 0.6, 0.5, 0.4):
        chosen = build_to_target(strong_legs(10, p), TARGETS["lotto"])
        odds = int(american(combined(chosen)))
        assert 3000 <= odds <= 20000 and 4 <= len(chosen) <= 10, (p, odds, len(chosen))
    # Mixed with underdog moneylines it still lands in range, within 10 legs.
    legs = strong_legs(8, 0.7) + [leg(f"Dog{g}", 0.35, f"d{g}") for g in range(4)]
    chosen = build_to_target(legs, TARGETS["lotto"])
    assert 3000 <= int(american(combined(chosen))) <= 20000 and 4 <= len(chosen) <= 10


def test_lotto_never_goes_past_10_legs_and_says_how_close_it_got():
    chosen = build_to_target(strong_legs(10, 0.85), TARGETS["lotto"])  # 0.85^10 is only about +408
    assert len(chosen) == 10
    assert "Closest I could get is +" in parlay_embed("NFL", "🏈", chosen, TARGETS["lotto"])[0].description


def test_lotto_needs_at_least_4_legs():
    legs = [leg("Dog1", 0.18, "1"), leg("Dog2", 0.16, "2"), leg("A", 0.7, "3"), leg("B", 0.7, "4"), leg("C", 0.7, "5")]
    chosen = build_to_target(legs, TARGETS["lotto"])
    assert len(chosen) >= 4


def test_big_payout_prefers_the_better_paying_legs():
    legs = strong_legs(10, 0.95) + [leg(f"B{g}", 0.7, str(g), f"b{g}") for g in range(10)]
    chosen = build_to_target(legs, TARGETS["big"])
    assert 1000 <= int(american(combined(chosen))) <= 10000 and len(chosen) <= 10


def test_too_few_games_says_how_close_it_got():
    chosen = build_to_target(strong_legs(2, 0.9), TARGETS["big"])  # 4 legs at most: about -190
    embed, _ = parlay_embed("NFL", "🏈", chosen, TARGETS["big"])
    assert len(chosen) == 4 and "Closest I could get is +" not in embed.description
    assert "Closest I could get is -" in embed.description


def test_parlay_embed_and_copyable_slip():
    chosen = [leg("Josh Downs Over 1.5 Receptions", 0.70, "1"), leg("Baltimore Ravens Moneyline", 0.70, "2")]
    embed, slip = parlay_embed("NFL", "🏈", chosen, TARGETS["safe"])
    assert embed.title == "🎟️ 🏈 NFL · Safe (around +100): 2 legs"
    assert embed.fields[0].name == "Estimated odds: +104" and "**49%**" in embed.fields[0].value
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


def soccer_games(goals, assists):
    return [PlayerGame(f"2026-09-{28 - i:02d}", "2026-27", "ARS",
                       {"totalShots": 3, "shotsOnTarget": 1, "totalGoals": g, "goalAssists": a, "goalOrAssist": min(1, g + a),
                        "foulsCommitted": 0}) for i, (g, a) in enumerate(zip(goals, assists))]


def test_goalscorer_and_assist_bets():
    # Scored in 4 of the last 10, assisted in 3: long shots, but real ones.
    games = soccer_games([1, 0, 1, 0, 0, 1, 0, 0, 1, 0], [0, 1, 0, 0, 1, 0, 0, 1, 0, 0])
    picks = {t.pick: t for t in best_trends("Erling Haaland", "9", "MNC", "ARS", games, "soccer", scorers=True)}
    assert set(picks) == {"Erling Haaland Anytime Goalscorer", "Erling Haaland To Record an Assist", "Erling Haaland To Score or Assist"}
    assert 0.3 < picks["Erling Haaland Anytime Goalscorer"].probability < 0.45
    # The regular trends skip these: they never reach the usual 75% bar.
    assert [t.pick for t in best_trends("Erling Haaland", "9", "MNC", "ARS", games, "soccer")] == [
        "Erling Haaland Over 2.5 Shots", "Erling Haaland Over 0.5 Shots on Target"]
    # Rarely scores: no goalscorer bet.
    rare = soccer_games([1, 0, 0, 0, 0, 0, 0, 0, 0, 0], [0] * 10)
    assert [t.pick for t in best_trends("Defender", "4", "MNC", "ARS", rare, "soccer", scorers=True)] == []


def test_lotto_from_goalscorers_in_one_game():
    legs = [leg(f"Player{i} Anytime Goal", p, "1", f"p{i}") for i, p in enumerate((0.42, 0.38, 0.33, 0.3, 0.28, 0.25))]
    chosen = build_to_target(legs, TARGETS["lotto"], per_game=15)
    assert 4 <= len(chosen) <= 10 and 3000 <= int(american(combined(chosen))) <= 20000
    embed, _ = parlay_embed("Premier League", "⚽", chosen, TARGETS["lotto"], same_game=True)
    assert any(f.name == "Same-game parlay" for f in embed.fields)


def test_scorer_list_skips_goal_or_assist_that_repeats_the_goal_line():
    from sportsbot.props import scorer_lines
    pure_scorer = soccer_games([1, 0, 1, 0, 0, 1, 0, 0, 1, 0], [0] * 10)
    text = scorer_lines(best_trends("Dominic Calvert-Lewin", "7", "LEE", "ARS", pure_scorer, "soccer", scorers=True))
    assert "Anytime Goalscorer" in text and "To Score or Assist" not in text


def test_goal_and_assist_slip_like_fanduel():
    """Two goalscorers and two assisters across games, like a typical FanDuel goal/assist slip."""
    def g(name, p, game):
        return Leg(f"{name} Anytime Goalscorer", p, "", "", game, name, "prop", "totalGoals", 1)

    def a(name, p, game):
        return Leg(f"{name} To Record an Assist", p, "", "", game, name + "-a", "prop", "goalAssists", 1)
    legs = [g("Erling Haaland", 0.55, "1"), g("Gonçalo Ramos", 0.45, "2"), g("Alexander Isak", 0.44, "3"),
            g("Kai Havertz", 0.42, "4"), a("Bruno Fernandes", 0.33, "5"), a("Martin Ødegaard", 0.3, "4"),
            a("Kevin De Bruyne", 0.28, "6")]
    chosen = build_to_target(legs, TARGETS["lotto"], balance=True)
    kinds = [leg.stat for leg in chosen]
    assert kinds.count("totalGoals") == kinds.count("goalAssists") == 2  # not four goalscorers
    assert 3000 <= int(american(combined(chosen))) <= 20000
    assert chosen[1].pick == "Bruno Fernandes To Record an Assist"  # the likeliest assister, right after the top scorer


def arsenal_leeds():
    from sportsbot.espn import parse_scoreboard
    comp = {"status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": "0", "team": {"id": "359", "abbreviation": "ARS", "displayName": "Arsenal"}},
                            {"homeAway": "away", "score": "0", "team": {"id": "357", "abbreviation": "LEE", "displayName": "Leeds United"}}]}
    [g] = parse_scoreboard({"events": [{"id": "1", "date": "2026-10-10T11:30Z", "competitions": [comp]}]}, LEAGUES["epl"])
    return g


def form(scored, allowed):
    from sportsbot.research import Form
    return Form([], scored, allowed, 5)


def test_expected_goals_from_the_betting_line_or_form():
    from sportsbot.props import expected_goals
    g = arsenal_leeds()
    usual = {"359": form(1.8, 1.2), "357": form(1.8, 1.8)}
    # Arsenal -260 / draw +390 / Leeds +650 (no-vig 68/19/13) with the total at 2.5.
    m = expected_goals(g, {"359": 0.682, "draw": 0.193, "357": 0.126}, 2.5, usual)
    assert m.source == "market" and round(m.expected["359"], 2) == 1.88 and round(m.expected["357"], 2) == 0.62
    # No line: Arsenal's scoring averaged with what Leeds concede.
    m = expected_goals(g, None, None, usual)
    assert m.source == "form" and m.expected == {"359": 1.8, "357": 1.5}
    assert expected_goals(g, None, None, {}) is None  # no form either: nothing to go on


def scorer(name, team, p):
    from sportsbot.props import PROPS, Rate, Trend
    prop = next(x for x in PROPS["soccer"] if x.stat == "totalGoals")
    return Trend(name, name, team, prop, 1, Rate(4, 10), Rate(0, 0), Rate(0, 0), Rate(0, 0), "2026-27", "", "", p)


def test_matchup_moves_goalscorer_chances():
    from sportsbot.props import Matchup, apply_matchup
    g = arsenal_leeds()
    # Arsenal expected to score well above their usual; Leeds well below theirs.
    m = Matchup({"359": 2.7, "357": 0.6}, {"359": 1.8, "357": 1.5}, "market")
    found = {t.player: t for t in apply_matchup([scorer("Kai Havertz", "ARS", 0.40), scorer("Dominic Calvert-Lewin", "LEE", 0.40),
                                                 scorer("Anton Stach", "LEE", 0.25)], g, m)}
    # Arsenal's 1.8 a game blends with a typical 1.4 to 1.62; 2.7 expected is 1.67x that: 40% -> 57%, not 67%.
    assert round(found["Kai Havertz"].probability, 2) == 0.57
    assert round(found["Dominic Calvert-Lewin"].probability, 2) == 0.23  # cut by at most half: 40% -> 23%
    assert "Anton Stach" not in found  # a long shot against a tight defence drops out
    assert found["Kai Havertz"].evidence.endswith("Arsenal expected 2.7 goals (betting line), 1.8 a game lately")
    # Without a matchup the record stands as it is.
    assert apply_matchup([scorer("Kai Havertz", "ARS", 0.40)], g, None)[0].probability == 0.40


def test_a_cold_spell_is_not_taken_as_the_teams_level():
    from sportsbot.props import Matchup, apply_matchup
    # Scoring 0.6 a game over 5 games: expecting 1.2 is double that, but only about 1.25x a typical team's level.
    m = Matchup({"359": 1.2, "357": 1.2}, {"359": 0.6, "357": 1.4}, "market", 5)
    [t] = apply_matchup([scorer("Kai Havertz", "ARS", 0.30)], arsenal_leeds(), m)
    assert 0.34 < t.probability < 0.38


def test_never_two_assists_or_two_goalscorers_from_one_team():
    """The slip that prompted this: Messi and De Paul (both Inter Miami) to assist."""
    def leg_(name, team, stat, p, game):
        wording = "To Record an Assist" if stat == "goalAssists" else "Anytime Goalscorer"
        return Leg(f"{name} {wording}", p, "", "", game, f"{name}-{stat}", "prop", stat, 1, None, "mls", "", name, team)
    legs = [leg_("Wessam Abou Ali", "CLB", "totalGoals", 0.45, "1"), leg_("Lionel Messi", "MIA", "goalAssists", 0.42, "2"),
            leg_("Rodrigo De Paul", "MIA", "goalAssists", 0.40, "2"), leg_("Luis Suárez", "MIA", "totalGoals", 0.38, "2"),
            leg_("Lionel Messi", "MIA", "totalGoals", 0.37, "2"), leg_("Myrto Uzuni", "ATX", "totalGoals", 0.33, "3"),
            leg_("Diego Rossi", "CLB", "totalGoals", 0.32, "1"), leg_("Owen Wolff", "ATX", "goalAssists", 0.30, "3")]
    chosen = build_to_target(legs, TARGETS["lotto"], per_game=15, balance=True)
    by_team = {}
    for leg in chosen:
        by_team.setdefault((leg.team, leg.stat), []).append(leg.player)
    assert all(len(players) == 1 for players in by_team.values()), by_team
    assert len({leg.player_id for leg in chosen}) == len(chosen)
    assert ("MIA", "goalAssists") in by_team and by_team[("MIA", "goalAssists")] == ["Lionel Messi"]
