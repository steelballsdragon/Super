import asyncio
from dataclasses import replace

from sportsbot.espn import parse_scoreboard
from sportsbot.leagues import LEAGUES
from sportsbot.research import (LeanBook, grade, implied, leans, market_chances, most_likely, no_vig,
                                parse_research, payout_per_unit, picks_embed, record_embed, report_embed)
from sportsbot.settings import StateStore


def game(state="pre", home=0, away=0):
    comp = {"status": {"type": {"state": state, "name": "STATUS_FINAL" if state == "post" else "STATUS_SCHEDULED", "shortDetail": ""}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"id": "28", "abbreviation": "WSH", "displayName": "Washington Commanders"}},
                            {"homeAway": "away", "score": str(away), "team": {"id": "11", "abbreviation": "IND", "displayName": "Indianapolis Colts"}}]}
    [g] = parse_scoreboard({"events": [{"id": "77", "date": "2026-10-04T17:00Z", "competitions": [comp]}]}, LEAGUES["nfl"])
    return g


def summary(home_pct="43.3", away_pct="56.4", home_ml=170, away_ml=-205, open_home="-1.5", qb_out=True, total=47.5, form=True):
    s = {
        "pickcenter": [{"provider": {"name": "DraftKings"}, "overUnder": total,
                        "moneyline": {"home": {"close": {"odds": f"{home_ml:+d}"}}, "away": {"close": {"odds": f"{away_ml:+d}"}}},
                        "pointSpread": {"home": {"open": {"line": open_home}, "close": {"line": "+4.5"}},
                                        "away": {"close": {"line": "-4.5"}}},
                        "total": {"over": {"close": {"line": f"o{total}"}}},
                        "homeTeamOdds": {"favoriteAtOpen": True}, "awayTeamOdds": {"favoriteAtOpen": False}}],
        "predictor": {"homeTeam": {"id": "28", "gameProjection": home_pct}, "awayTeam": {"id": "11", "gameProjection": away_pct}},
        "injuries": [{"team": {"id": "28"}, "injuries": [
            {"status": "Out", "athlete": {"displayName": "Rachaad White", "position": {"abbreviation": "RB"}}},
            {"status": "Questionable", "athlete": {"displayName": "Terry McLaurin", "position": {"abbreviation": "WR"}}},
        ] + ([{"status": "Out", "athlete": {"displayName": "Jayden Daniels", "position": {"abbreviation": "QB"}}}] if qb_out else [])}],
        "againstTheSpread": [{"team": {"id": "11"}, "records": [{"summary": "2-2", "type": "overall"}]}],
    }
    if form:
        s["lastFiveGames"] = [
            {"team": {"id": "28"}, "events": [{"gameResult": "L", "score": "17-13", "atVs": "@", "opponent": {"abbreviation": "DET"}},
                                              {"gameResult": "W", "score": "33-31", "atVs": "vs", "opponent": {"abbreviation": "SEA"}},
                                              {"gameResult": "L", "score": "41-3", "atVs": "@", "opponent": {"abbreviation": "BAL"}}]},
            {"team": {"id": "11"}, "events": [{"gameResult": "L", "score": "34-6", "atVs": "vs", "opponent": {"abbreviation": "ATL"}},
                                              {"gameResult": "W", "score": "19-17", "atVs": "vs", "opponent": {"abbreviation": "HOU"}},
                                              {"gameResult": "L", "score": "33-30 OT", "atVs": "@", "opponent": {"abbreviation": "KC"}}]},
        ]
    return s


def test_implied_and_no_vig():
    assert round(implied("-205"), 4) == 0.6721 and round(implied("+170"), 4) == 0.3704
    home, away = no_vig("+170", "-205")
    assert round(home + away, 6) == 1 and round(away, 3) == 0.645


def test_parse_research_reads_every_source():
    r = parse_research(summary(), game())
    assert r.model == {"28": 43.3, "11": 56.4}
    assert r.open_home_spread == -1.5 and r.open_favorite == "Washington Commanders"
    assert [i.name for i in r.injuries["28"]] == ["Jayden Daniels", "Rachaad White"]  # QB first; Questionable left out
    assert r.form["28"].results == ["L 13-17 @DET", "W 33-31 vsSEA", "L 3-41 @BAL"]
    assert r.form["11"].results[2] == "L 30-33 @KC"  # overtime score parsed
    assert r.ats == {"11": "2-2 ATS (overall)"}
    assert {k: round(v, 3) for k, v in market_chances(r).items()} == {"28": 0.355, "11": 0.645}


def test_model_gap_lean_carries_its_cautions():
    found = leans(parse_research(summary(), game()))
    ml, spread = found[0], found[1]
    assert (ml.market, ml.pick, spread.pick) == ("moneyline", "Washington Commanders +170", "Washington Commanders +4.5")
    assert ml.confidence == "Low"
    assert ml.why[-1] == "Model is 7.8 points higher than the market"
    assert ml.cautions == ["Washington Commanders QB Jayden Daniels is Out; the model may not reflect it",
                           "The line moved 6 points toward Indianapolis Colts since it opened (Washington Commanders -1.5 → +4.5)"]


def test_clean_gap_gets_real_confidence_and_huge_gap_is_flagged():
    clean = leans(parse_research(summary(home_pct="47.0", away_pct="53.0", open_home="+4.5", qb_out=False), game()))
    assert clean[0].confidence == "Medium" and clean[0].cautions == []  # 11.5-point gap, nothing suspicious
    huge = leans(parse_research(summary(home_pct="55.0", away_pct="45.0", open_home="+4.5", qb_out=False), game()))
    assert huge[0].confidence == "Low" and "unusual" in huge[0].cautions[0]


def test_no_lean_when_data_agrees_with_the_line():
    assert leans(parse_research(summary(home_pct="37.0", away_pct="63.0", form=False), game())) == []


def test_total_lean_from_form_tops_out_at_medium():
    found = leans(parse_research(summary(home_pct="36", away_pct="64", total=60.5), game()))
    [total] = [l for l in found if l.market == "total"]
    assert total.pick == "Under 60.5" and total.confidence == "Medium"
    # WSH: 16.3 scored / 29.7 allowed; IND: 18.3 / 28.0 -> (16.3 + 28.0)/2 + (18.3 + 29.7)/2 = 46.2
    assert total.why[-1] == "Projected total 46.2 vs line 60.5 (-24%)"


def test_grading():
    final = game("post", home=20, away=24)  # Indianapolis Colts wins by 4
    assert grade({"market": "spread", "side": "28", "line": 4.5}, final) == "win"   # Washington Commanders +4.5 covers
    assert grade({"market": "spread", "side": "11", "line": -4.5}, final) == "loss"
    assert grade({"market": "spread", "side": "28", "line": 4.0}, final) == "push"
    assert grade({"market": "moneyline", "side": "28", "line": None}, final) == "loss"
    assert grade({"market": "total", "side": "under", "line": 47.5}, final) == "win"
    assert grade({"market": "total", "side": "over", "line": 44.0}, final) == "push"
    assert payout_per_unit("+170") == 1.7 and round(payout_per_unit("-205"), 3) == 0.488 and round(payout_per_unit(None), 3) == 0.909


def test_lean_book_records_once_and_settles(tmp_path):
    book = LeanBook(StateStore(tmp_path / "state.json"))
    g = game()
    found = leans(parse_research(summary(), g))
    book.record(g, found)
    book.record(g, found)  # running research again doesn't double count
    book.record(replace(g, state="in"), found)  # live leans aren't recorded
    assert book.pending() == 2 and book.pending_leagues() == {"nfl"}
    settled = book.settle(game("post", home=20, away=24))
    assert sorted((l["market"], l["result"]) for l in settled) == [("moneyline", "loss"), ("spread", "win")]
    s = book.summary()
    assert s["all"]["win"] == 1 and s["all"]["loss"] == 1 and round(s["all"]["units"], 3) == round(1 / 1.1 - 1, 3)
    assert s["Low"]["win"] == 1
    assert book.pending() == 0 and book.settle(game("post", 20, 24)) == []  # graded once


def test_embeds_render():
    r = parse_research(summary(), game())
    found = leans(r)
    e = report_embed(r, found)
    names = [f.name for f in e.fields]
    assert names == ["📈 Market (DraftKings)", "🧮 ESPN Matchup Predictor", "📋 Last 5 (ESPN)",
                     "🩹 Injuries (ESPN)", "💡 Leans", "🎯 Most likely result (market)"]
    assert "Line move: Washington Commanders -1.5 at open → +4.5 now" in e.fields[0].value
    assert "⚠️ Washington Commanders QB Jayden Daniels is Out" in e.fields[4].value
    assert most_likely(r) == ("Indianapolis Colts win", market_chances(r)["11"], "-205")
    picks = picks_embed("NFL", "🏈", [(r, found)])
    assert picks.fields[0].value.startswith("**Washington Commanders +4.5 / ML +170** · Indianapolis Colts @ Washington Commanders · Low ⚠️")
    assert record_embed({}, 2).description == "No graded leans yet (2 waiting on games)."


def test_leans_are_graded_when_the_bot_sees_the_final(tmp_path):
    from sportsbot.bot import SportsBot
    from sportsbot.settings import SettingsStore
    from sportsbot.storage import SubscriptionStore
    bot = SportsBot(SubscriptionStore(tmp_path / "s.json"), 10, None,
                    SettingsStore(tmp_path / "settings.json"), StateStore(tmp_path / "state.json"))
    g = game()
    bot.leans.record(g, leans(parse_research(summary(), g)))
    snaps = iter([[replace(g, state="in", status_name="STATUS_IN_PROGRESS")], [game("post", 20, 24)]])

    async def scoreboard(league):
        return next(snaps)
    bot.espn.scoreboard = scoreboard

    async def no_plays(event_id):
        return []
    bot.play_resolvers["nfl"]._fetch = no_plays
    ticks = iter(range(0, 10_000, 100))
    bot.play_resolvers["nfl"]._clock = lambda: next(ticks)
    assert "nfl" in (bot.store.leagues() | bot.leans.pending_leagues())  # polled although nobody follows the NFL
    for _ in range(2):
        asyncio.run(bot._poll_league("nfl"))
    assert bot.leans.pending() == 0 and bot.leans.summary()["all"]["win"] == 1
