"""Player stats from ESPN's box score: /stats and the 📊 button on finals."""

import asyncio
from types import SimpleNamespace

from sportsbot.boxscore import StatsButton, stats_embed, stats_view, team_stats
from tests.test_commands_safety import Inter, make_bot


def header(away, away_score, home, home_score, status="Final"):
    return {"competitions": [{"status": {"type": {"shortDetail": status}}, "competitors": [
        {"homeAway": "away", "score": str(away_score), "team": {"displayName": away}},
        {"homeAway": "home", "score": str(home_score), "team": {"displayName": home}}]}]}


def group(keys, rows, **extra):
    return {"keys": keys, "athletes": [{"athlete": {"displayName": n}, "stats": s} for n, s in rows], **extra}


def test_basketball_top_scorers_with_threes():
    keys = ["minutes", "points", "rebounds", "assists", "threePointFieldGoalsMade-threePointFieldGoalsAttempted"]
    summary = {"header": header("Miami Heat", 114, "Toronto Raptors", 93),
               "boxscore": {"players": [{"team": {"displayName": "Miami Heat"}, "statistics": [group(keys, [
                   ("Bench Guy", ["0", "0", "0", "0", "0-0"]),
                   ("Bam Adebayo", ["30", "10", "6", "0", "0-1"]),
                   ("Tyler Herro", ["34", "24", "3", "5", "4-9"])])]}]}}
    [heat] = team_stats(summary, "basketball")
    assert heat.lines == ["**Tyler Herro** 24 PTS · 3 REB · 5 AST · 4/9 3PT · 34 MIN",
                          "**Bam Adebayo** 10 PTS · 6 REB · 0 AST · 30 MIN"]  # 0 minutes: left out
    e = stats_embed(summary, "nba")
    assert e.title == "📊 🏀 Miami Heat 114 @ Toronto Raptors 93" and e.footer.text == "NBA · Final"


def test_hockey_scorers_once_and_goalies():
    keys = ["goals", "assists", "shotsTotal"]
    skaters = [("Bo Horvat", ["1", "1", "4"]), ("Quiet Guy", ["0", "0", "1"]), ("Shooter", ["0", "0", "5"])]
    summary = {"boxscore": {"players": [{"team": {"displayName": "New York Islanders"}, "statistics": [
        group(keys, skaters, name="forwards"), group(keys, skaters, name="skaters"),
        group(["saves", "shotsAgainst", "savePct"], [("Ilya Sorokin", ["30", "32", ".938"])], name="goalies")]}]}}
    [nyi] = team_stats(summary, "hockey")
    assert nyi.lines == ["**Bo Horvat** 1 G · 1 A · 4 SOG", "**Shooter** 5 SOG", "🥅 **Ilya Sorokin** 30/32 saves (.938)"]


def test_football_passing_rushing_receiving_and_defense():
    summary = {"boxscore": {"players": [{"team": {"displayName": "Pittsburgh Steelers"}, "statistics": [
        group(["completions/passingAttempts", "passingYards", "passingTouchdowns", "interceptions"],
              [("Aaron Rodgers", ["22/40", "299", "3", "2"])], name="passing"),
        group(["rushingAttempts", "rushingYards", "rushingTouchdowns"],
              [("Jaylen Warren", ["17", "93", "0"]), ("Travis Homer", ["2", "-1", "0"])], name="rushing"),
        group(["receptions", "receivingYards", "receivingTouchdowns"],
              [("Roman Wilson", ["3", "74", "1"]), ("DK Metcalf", ["5", "115", "0"])], name="receiving"),
        group(["totalTackles", "sacks"], [("T.J. Watt", ["4", "2"]), ("No Sack", ["9", "0"])], name="defensive")]}]}}
    [pit] = team_stats(summary, "football")
    assert pit.lines == ["🎯 **Aaron Rodgers** 22/40, 299 yds, 3 TD, 2 INT", "🏃 **Jaylen Warren** 17 car, 93 yds",
                         "🙌 **DK Metcalf** 5 rec, 115 yds", "🙌 **Roman Wilson** 3 rec, 74 yds, 1 TD",
                         "💥 **T.J. Watt** 2 sacks, 4 tkl"]


def test_baseball_hitters_and_pitchers():
    summary = {"boxscore": {"players": [{"team": {"displayName": "Chicago White Sox"}, "statistics": [
        group(["hits-atBats", "runs", "hits", "RBIs", "homeRuns"],
              [("Munetaka Murakami", ["2-4", "1", "2", "2", "1"]), ("Hitless", ["0-4", "0", "0", "0", "0"])],
              type="batting"),
        group(["fullInnings.partInnings", "hits", "earnedRuns", "strikeouts"], [("Hagen Smith", ["6.0", "3", "1", "8"])],
              type="pitching")]}]}}
    [cws] = team_stats(summary, "baseball")
    assert cws.lines == ["**Munetaka Murakami** 2-4 · 1 HR · 2 RBI · 1 R", "⚾ **Hagen Smith** 6.0 IP, 3 H, 1 ER, 8 K"]


def player(name, starter=True, keeper=False, **stats):
    return {"athlete": {"displayName": name}, "starter": starter, "position": {"abbreviation": "G" if keeper else "F"},
            "stats": [{"name": k, "value": v} for k, v in stats.items()]}


def test_soccer_players_team_totals_and_keeper_saves():
    summary = {"header": header("Liverpool", 1, "AFC Bournemouth", 0, "FT"),
               "boxscore": {"teams": [{"team": {"id": "349"}, "statistics": [
                   {"name": "possessionPct", "displayValue": "46.2"}, {"name": "totalShots", "displayValue": "9"},
                   {"name": "shotsOnTarget", "displayValue": "2"}, {"name": "saves", "displayValue": "2"}]}]},
               "rosters": [{"team": {"id": "349", "displayName": "AFC Bournemouth"}, "roster": [
                   player("Djordje Petrovic", keeper=True, shotsFaced=0, goalsConceded=1),
                   player("Evanilson", totalShots=2, shotsOnTarget=2),
                   player("Ryan Christie", totalGoals=1, goalAssists=1, totalShots=4, shotsOnTarget=1, yellowCards=1),
                   player("Bench", starter=False),
                   player("Spare Keeper", starter=False, keeper=True)]}]}
    [bou] = team_stats(summary, "soccer")
    assert bou.summary == "46.2% possession · 9 shots · 2 on target"
    assert bou.lines == ["**Ryan Christie** ⚽ 1 · 🅰️ 1 · 4 shots (1 on target) · 🟨",
                         "**Evanilson** 2 shots (2 on target)",
                         "🧤 **Djordje Petrovic** 2 saves, 1 conceded"]  # the team's saves: ESPN's per-keeper count is 0
    assert stats_embed(summary, "epl").title == "📊 ⚽ AFC Bournemouth 0 - 1 Liverpool"


def test_no_stats_yet():
    e = stats_embed({"header": header("A", 0, "B", 0, "Scheduled")}, "nba")
    assert e.description == "No player stats on ESPN for this game yet."


def test_button_on_finals_for_sports_with_box_scores():
    view = stats_view(SimpleNamespace(league=SimpleNamespace(sport="hockey"), league_key="nhl", id="401891818"))
    [button] = view.children
    assert button.custom_id == "stats:nhl:401891818" and button.item.label == "Player stats"
    assert stats_view(SimpleNamespace(league=SimpleNamespace(sport="cricket"), league_key="ipl", id="1")) is None

    sent = []

    async def summary(path, gid):
        assert (path, gid) == ("hockey/nhl", "401891818")
        return {"header": header("New Jersey Devils", 0, "New York Islanders", 2)}

    class Response:
        async def defer(self, **kw):
            pass

    async def followup(content=None, embed=None, ephemeral=False):
        sent.append((embed.title, ephemeral))
    inter = SimpleNamespace(client=SimpleNamespace(espn=SimpleNamespace(summary=summary)), response=Response(),
                            followup=SimpleNamespace(send=followup))
    asyncio.run(StatsButton("nhl", "401891818").callback(inter))
    assert sent == [("📊 🏒 New Jersey Devils 0 @ New York Islanders 2", True)]


def test_stats_command_finds_the_teams_game(tmp_path):
    from sportsbot.bot import register_commands
    from tests.test_plays import nhl_board
    bot = make_bot(tmp_path)
    register_commands(bot)
    bot.store.add(7, "nhl")
    boards = {"nhl": nhl_board(away=1, home=2)}

    async def scoreboard(league, date=None):
        return boards[league.key]

    async def summary(path, gid):
        return {"header": header("New Jersey Devils", 1, "New York Islanders", 2, "2nd 5:00")}
    bot.espn.scoreboard, bot.espn.summary = scoreboard, summary
    stats = bot.tree.get_command("stats")
    assert {p.name for p in stats.parameters} == {"league", "team", "game"}
    i = Inter()
    asyncio.run(stats.callback(i, team="Islanders"))
    assert i.sent == ["📊 🏒 New Jersey Devils 1 @ New York Islanders 2"]
    i = Inter()
    asyncio.run(stats.callback(i, team="Rangers"))
    assert i.sent == ["No NHL game for **Rangers** today."]
    i = Inter()
    asyncio.run(stats.callback(i, game="nhl:7"))
    assert i.sent == ["📊 🏒 New Jersey Devils 1 @ New York Islanders 2"]

    suggest = stats._params["game"].autocomplete
    [choice] = asyncio.run(suggest(Inter(), "island"))
    assert choice.value == "nhl:7" and choice.name.startswith("New Jersey Devils 1 @ New York Islanders 2")
    asyncio.run(bot.espn.close())
