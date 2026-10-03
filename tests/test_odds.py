import asyncio

from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.odds import Odds, OddsBook, grade_text, line_text, parse_odds
from sportsbot.settings import StateStore


def nfl_odds_json():
    side = lambda line, odds: {"close": {"line": line, "odds": odds}}
    return {"provider": {"name": "DraftKings"}, "overUnder": 47.5,
            "moneyline": {"home": {"close": {"odds": "+170"}}, "away": {"close": {"odds": "-205"}}},
            "pointSpread": {"home": side("+4.5", "-115"), "away": side("-4.5", "-105")},
            "total": {"over": {"close": {"line": "o47.5", "odds": "-108"}}}}


def nfl(home, away, state="post", odds=True):
    comp = {"status": {"type": {"state": state, "name": "STATUS_FINAL" if state == "post" else "STATUS_SCHEDULED", "shortDetail": "Final"}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "28", "abbreviation": "WSH", "displayName": "Washington Commanders"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "11", "abbreviation": "IND", "displayName": "Indianapolis Colts"}}],
            "odds": [nfl_odds_json()] if odds else []}
    [g] = parse_scoreboard({"events": [{"id": "5", "date": "", "competitions": [comp]}]}, LEAGUES["nfl"])
    return g


def test_parse_real_shaped_lines():
    o = parse_odds({"odds": [nfl_odds_json()]})
    assert o == Odds("DraftKings", home_ml="+170", away_ml="-205", home_spread=4.5, away_spread=-4.5, total=47.5)
    assert parse_odds({"odds": []}) is None
    g = nfl(0, 0, "pre")
    assert line_text(g, g.odds) == "Spread: Indianapolis Colts -4.5 · Washington Commanders +4.5\nTotal: O/U 47.5\nMoneyline: Indianapolis Colts -205 · Washington Commanders +170"


def test_favorite_covers_and_under():
    g = nfl(17, 30)
    assert grade_text(g, g.odds).splitlines() == ["Spread: Indianapolis Colts -4.5 ✅ covered", "Total: Under 47.5 ✅ (47)", "Moneyline: Indianapolis Colts -205 ✅"]


def test_underdog_covers_while_losing():
    g = nfl(24, 27)  # IND wins by 3, so WSH +4.5 covers
    assert grade_text(g, g.odds).splitlines()[:2] == ["Spread: Washington Commanders +4.5 ✅ covered", "Total: Over 47.5 ✅ (51)"]


def test_pushes():
    g = nfl(20, 24)
    odds = Odds("DraftKings", home_spread=4.0, away_spread=-4.0, total=44.0)
    assert grade_text(g, odds).splitlines() == ["Spread: push (Washington Commanders +4)", "Total: push (44)"]


def soccer(home_goals, away_goals, name="STATUS_FULL_TIME", detail="FT", goals=()):
    comp = {"status": {"type": {"state": "post", "name": name, "shortDetail": detail}},
            "competitors": [{"homeAway": "home", "score": str(home_goals), "team": {"id": "359", "abbreviation": "ARS", "displayName": "Arsenal"}},
                            {"homeAway": "away", "score": str(away_goals), "team": {"id": "357", "abbreviation": "LEE", "displayName": "Leeds United"}}],
            "details": [{"scoringPlay": True, "team": {"id": t}, "clock": {"displayValue": m}, "athletesInvolved": [{"displayName": "x"}]} for t, m in goals]}
    [g] = parse_scoreboard({"events": [{"id": "6", "date": "", "competitions": [comp]}]}, LEAGUES["epl"])
    return g


SOCCER_ODDS = Odds("DraftKings", home_ml="-260", away_ml="+650", draw_ml="+390", home_spread=-1.5, away_spread=1.5, total=2.5)


def test_soccer_line_and_draw():
    g = soccer(1, 1)
    assert line_text(g, SOCCER_ODDS) == "Total: O/U 2.5 goals\nMoneyline: Arsenal -260 · Draw +390 · Leeds United +650"
    assert grade_text(g, SOCCER_ODDS).splitlines() == ["Total: Under 2.5 ✅ (2)", "Moneyline: Draw +390 ✅"]


def test_soccer_extra_time_settles_on_90_minutes():
    g = soccer(2, 1, "STATUS_FINAL_AET", "AET", goals=[("359", "30'"), ("357", "88'"), ("359", "105'")])
    assert grade_text(g, SOCCER_ODDS).splitlines() == [
        "Total: Under 2.5 ✅ (2)", "Moneyline: Draw +390 ✅", "*Settled on the 90-minute score: Arsenal 1-1 Leeds United*"]


def test_book_keeps_the_pregame_line_after_espn_drops_it(tmp_path):
    book = OddsBook(StateStore(tmp_path / "state.json"))
    book.remember([nfl(0, 0, "pre")])
    live_without_odds = nfl(7, 3, "in", odds=False)
    book.remember([live_without_odds])
    assert book.get(live_without_odds).total == 47.5
    assert OddsBook(StateStore(tmp_path / "state.json")).get(live_without_odds).home_ml == "+170"  # survives restarts


def test_bot_shows_line_at_start_and_grades_at_final(tmp_path):
    from sportsbot.bot import SportsBot
    from sportsbot.settings import SettingsStore
    from sportsbot.storage import SubscriptionStore
    bot = SportsBot(SubscriptionStore(tmp_path / "subs.json"), 10, None,
                    SettingsStore(tmp_path / "settings.json"), StateStore(tmp_path / "state.json"))
    bot.store.add(1, "nfl")
    bot.store.add(2, "nfl")
    bot.settings.update(2, odds=False)
    sent = []

    async def send(channel_id, embed=None, content=None):
        sent.append((channel_id, embed.title, [(f.name, f.value) for f in embed.fields]))
    bot._send = send

    async def no_plays(event_id):
        return []
    bot.play_resolvers["nfl"]._fetch = no_plays
    ticks = iter(range(0, 10_000, 100))  # each check is 100s later, so held finals are released
    bot.play_resolvers["nfl"]._clock = lambda: next(ticks)
    final = nfl(17, 30, odds=False)
    snaps = iter([nfl(0, 0, "pre"), nfl(0, 0, "in", odds=False), final, final])

    async def scoreboard(league):
        g = next(snaps)
        return [g if g.state != "in" else __import__("dataclasses").replace(g, status_name="STATUS_IN_PROGRESS")]
    bot.espn.scoreboard = scoreboard
    for _ in range(4):
        asyncio.run(bot._poll_league("nfl"))
    by_channel = {}
    for cid, title, fields in sent:
        by_channel.setdefault(cid, []).append((title, fields))
    start, final = [x for x in by_channel[1] if x[0] in ("🏈 Game started", "🏈 Final")]
    assert start[1] == [("📊 Line (DraftKings)", "Spread: Indianapolis Colts -4.5 · Washington Commanders +4.5\nTotal: O/U 47.5\nMoneyline: Indianapolis Colts -205 · Washington Commanders +170")]
    assert final[1][-1] == ("📊 Bets (DraftKings closing line)",
                            "Spread: Indianapolis Colts -4.5 ✅ covered\nTotal: Under 47.5 ✅ (47)\nMoneyline: Indianapolis Colts -205 ✅")
    assert all(not any(name.startswith("📊") for name, _ in fields) for _, fields in by_channel[2])  # odds off
