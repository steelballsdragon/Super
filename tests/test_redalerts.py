"""🚨 Red alerts: DraftKings' total-shots prices (a real sample via ESPN), a player's record against them, and the
morning post's singles, parlay and lotto, recorded for grading."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sportsbot import redalerts as R
from sportsbot.bot import SportsBot, register_commands
from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.props import PlayerGame
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


def log_of(shots, season="2026-27", opponent="COM"):
    return [PlayerGame(f"2026-10-{30 - i:02d}", season, opponent, {"totalShots": float(s)}) for i, s in enumerate(shots)]


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

    async def game_alerts(self, g):
        price = R.Price(2, "+150", 2.5)
        return [R.Alert(f"Player {g.id}", f"p{g.id}", g.home.abbrev, g.away.abbrev, g, price, 0.6 + int(g.id) / 100,
                        R.Rate(7, 10), R.Rate(5, 7), R.Rate(10, 20), R.Rate(0, 0), "2026-27", "2025-26")]
    monkeypatch.setattr(R.RedAlerts, "game_alerts", game_alerts)
    posted = asyncio.run(bot.auto_post(9, "redalerts", tz))
    embeds = [e for _, e, _, _ in sent if e is not None]
    assert posted == 3 and [e.title.split(":")[0] for e in embeds] == [
        f"🚨 Red alerts · {datetime.now(tz).date():%A %B} {datetime.now(tz).date().day}", "🚨 Red alert parlay",
        "🚨 Red alert lotto"]
    assert "Player 4 2+ Shots** · DK **+150**" in embeds[0].description  # best edge first
    slips = [c for _, e, c, v in sent if e is None]
    assert all(c.startswith("📋 Copy for DraftKings") for c in slips) and all(v for _, e, _, v in sent if e is None)
    styles = {p["style"]: p for p in bot.parlays.pending()}
    assert styles["Red alert singles"]["round_robin"] == 1 and len(styles["Red alert singles"]["legs"]) == 5
    assert len(styles["Red alert parlay"]["legs"]) == 3 and len(styles["Red alert lotto"]["legs"]) == 5
    assert all(leg["stat"] == "totalShots" for p in styles.values() for leg in p["legs"])
    assert all(cid == 9 for cid, *_ in sent)

    sent.clear()
    bot.week_games = lambda key: asyncio.sleep(0, result=[])
    assert asyncio.run(bot.auto_post(9, "redalerts", tz)) == 0 and sent == []  # nothing today: quiet
    asyncio.run(bot.espn.close())
