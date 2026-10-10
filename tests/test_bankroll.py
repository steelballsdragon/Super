"""Logged bets, bankroll and stakes: the money has to be right."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from sportsbot.bankroll import (BetBook, PlacedButton, PlacedModal, bankroll_embed, bet_profit, parse_price,
                                placed_reply, units_for)
from sportsbot.parlays import ParlayBook, settle
from sportsbot.settings import StateStore
from tests.test_parlays import FakeBot, legs, nfl_game


def test_prices_in_american_or_decimal():
    assert parse_price("+450") == 5.5
    assert parse_price("450") == 5.5
    assert parse_price("-200") == 1.5
    assert parse_price("5.5") == 5.5
    assert parse_price("+1,250") is None  # not a number as typed
    for bad in ("", "abc", "+50", "-99", "0", "1.0"):
        assert parse_price(bad) is None, bad


def test_longer_shots_get_smaller_stakes():
    assert units_for(0.5) == 1.0
    assert units_for(0.08) == 0.5
    assert units_for(0.01) == 0.25


def parlay(statuses, probs=(0.5, 0.5, 0.5), status="pending", rr=None):
    return {"id": "abc12345", "league": "nfl", "style": "Safe", "status": status, "channel": 9,
            **({"round_robin": rr} if rr else {}),
            "legs": [{"probability": p, "status": s} for p, s in zip(probs, statuses)]}


def test_parlay_profit_won_lost_void_and_a_void_leg():
    bet = {"stake": 10.0, "price": 8.0}
    assert bet_profit(bet, parlay(["hit"] * 3, status="won")) == 70.0
    assert bet_profit(bet, parlay(["hit", "miss", "hit"], status="lost")) == -10.0
    assert bet_profit(bet, parlay(["void"] * 3, status="void")) == 0.0
    # A void leg comes out of the price: 8.0 without a fair-even leg is 4.0.
    assert bet_profit(bet, parlay(["hit", "void", "hit"], status="won")) == 30.0


def test_round_robin_profit_pays_each_pair():
    p = parlay(["hit", "hit", "miss"], rr=2)
    bet = {"stake": 2.0, "prices": [3.0, 4.0, 5.0]}
    # 1+2 cashes at 12.0 (+22), 1+3 and 2+3 lose 2 each.
    assert bet_profit(bet, p) == 18.0
    # A void pick turns its pairs into singles: 1+3 pays 3.0 (+4), 2+3 pays 4.0 (+6).
    assert bet_profit(bet, parlay(["hit", "hit", "void"], rr=2)) == 22 + 4 + 6
    assert bet_profit(bet, parlay(["miss", "miss", "hit"], rr=2)) == -6.0
    assert bet_profit(bet, parlay(["void"] * 3, rr=2)) == 0.0


def test_balance_unit_stakes_and_daily_limit(tmp_path):
    book = BetBook(StateStore(tmp_path / "r.json"))
    assert book.balance(1) is None and book.suggested_stake(1, parlay(["pending"] * 3)) is None
    book.set_bankroll(1, 500)
    assert book.balance(1) == 500 and book.unit(1) == 10  # Standard: 2%
    assert book.suggested_stake(1, parlay(["pending"] * 3)) == 5.0  # 12.5% to hit: half a unit
    assert book.suggested_stake(1, parlay(["pending"] * 3, probs=(0.3, 0.3, 0.3), rr=2)) == 1.6  # 0.5u over 3 pairs
    book.set_style(1, "careful")
    assert book.unit(1) == 5
    book.add_money(1, 100)
    assert book.balance(1) == 600

    p = parlay(["pending"] * 3)
    bet = book.place(p, 1, 70.0, price=8.0)
    text = placed_reply(book, p, bet, 1)
    assert "$70.00 at +700" in text and "to win $490.00" in text
    assert "Edge at your price: **0%**" in text or "📉" in text  # fair is exactly +700
    assert "more than 3 units" in text and "daily limit" in text

    p["legs"] = [{**leg, "status": "hit"} for leg in p["legs"]]
    p["status"] = "won"
    [settled] = book.settle(p)
    assert settled["status"] == "won" and settled["profit"] == 490.0
    assert book.balance(1) == 1090.0
    assert book.settle(p) == []  # once only

    # Setting the bankroll again starts the tracking over.
    book.set_bankroll(1, 200)
    assert book.balance(1) == 200 and book.bankroll(1)["style"] == "careful"


def test_people_only_see_their_own_bets(tmp_path):
    book = BetBook(StateStore(tmp_path / "r.json"))
    book.set_bankroll(1, 100)
    book.place(parlay(["pending"] * 3), 2, 5.0, price=9.0)
    embed = bankroll_embed(book, 1, "Sam")
    assert "Recent bets" not in [f.name for f in embed.fields]
    assert "Balance: $100.00" in embed.description


def test_settling_a_slip_settles_the_bets_and_posts_them(tmp_path):
    bot = FakeBot(tmp_path, [nfl_game()], {"p1": {"receptions": 6}, "p2": {"receivingYards": 55}})
    bot.bets = BetBook(bot.state)
    book = ParlayBook(bot.state)
    pid = book.record(9, "nfl", "Safe", legs())
    bot.bets.set_bankroll(42, 1000)
    bot.bets.place(bot.state.get("parlays", pid), 42, 10.0, price=2.5)
    asyncio.run(settle(bot, book))
    [(_, embed)] = bot.sent
    assert embed.title.startswith("🎟️ ✅ Parlay won")
    [field] = [f for f in embed.fields if f.name == "Your bets"]
    assert field.value == "💵 <@42>: **+$15.00** on $10.00"
    assert bot.bets.balance(42) == 1015.0


class Inter:
    def __init__(self, records, user_id=42):
        self.user = SimpleNamespace(id=user_id, display_name="Sam")
        self.sent, self.modal = [], None
        self.client = SimpleNamespace(records=records, bets=BetBook(records))
        outer = self

        class Response:
            async def send_message(self, content=None, embed=None, ephemeral=False, **kw):
                assert ephemeral  # money is private
                outer.sent.append(content if embed is None else (content, embed))

            async def send_modal(self, modal):
                outer.modal = modal
        self.response = Response()


def submit(modal, inter, stake, price):
    modal.stake._value, modal.price._value = stake, price
    asyncio.run(modal.on_submit(inter))


def test_button_opens_the_form_and_logs_the_bet(tmp_path):
    records = StateStore(tmp_path / "r.json")
    pid = ParlayBook(records).record(9, "nfl", "Big payout", legs())
    inter = Inter(records)
    inter.client.bets.set_bankroll(42, 400)

    async def press():
        await PlacedButton(pid).callback(inter)
    asyncio.run(press())
    modal = inter.modal
    assert isinstance(modal, PlacedModal) and modal.stake.default == "8.00"  # 1 unit: ~41% to hit
    submit(modal, inter, "abc", "+150")
    assert "should be a number" in inter.sent[-1]
    submit(modal, inter, "8", "lots")
    assert "Enter the price" in inter.sent[-1]
    submit(modal, inter, "$8", "+150")
    assert "Logged: **$8.00 at +150**" in inter.sent[-1]
    assert inter.client.bets.get(pid, 42)["price"] == 2.5
    submit(modal, inter, "0", "")
    assert inter.sent[-1] == "Removed your bet on this slip." and inter.client.bets.get(pid, 42) is None

    # A settled slip can't be logged any more.
    p = records.get("parlays", pid)
    records.set("parlays", pid, {**p, "status": "lost"})
    asyncio.run(press())
    assert inter.sent[-1] == "This slip is already settled."


def test_round_robin_form_takes_each_picks_price(tmp_path):
    records = StateStore(tmp_path / "r.json")
    pid = ParlayBook(records).record(9, "nba", "3-pointers round robin", legs(), round_robin=2)
    inter = Inter(records)
    asyncio.run(PlacedButton(pid).callback(inter))
    modal = inter.modal
    assert modal.stake.label == "Stake per bet ($)" and modal.stake.default is None  # no bankroll yet
    submit(modal, inter, "2", "+150, +200")
    assert "3 of them" in inter.sent[-1]
    submit(modal, inter, "2", "+150 +200 -120")
    reply = inter.sent[-1]
    assert "3 bets × $2.00 = $6.00" in reply and "/bankroll start:" in reply
    assert inter.client.bets.get(pid, 42)["prices"] == [2.5, 3.0, pytest.approx(1 + 100 / 120)]


def test_bankroll_command(tmp_path):
    from tests.test_commands_safety import make_bot
    from sportsbot.bot import register_commands
    bot = make_bot(tmp_path)
    register_commands(bot)
    cmd = bot.tree.get_command("bankroll")
    assert {p.name for p in cmd.parameters} == {"start", "add", "style"}
    inter = Inter(bot.records)
    run = cmd.callback
    asyncio.run(run(inter, add=50))
    content, embed = inter.sent[-1]
    assert "Set your bankroll first" in content and "No bankroll set" in embed.description
    style = SimpleNamespace(value="aggressive")
    asyncio.run(run(inter, start=1000, style=style))
    content, embed = inter.sent[-1]
    assert content == "Bankroll set to $1,000.00. Results count from now. Style: Aggressive."
    assert "**Unit: $30.00**" in embed.description
    asyncio.run(run(inter, add=-200))
    assert "Took out $200.00." == inter.sent[-1][0] and bot.bets.balance(42) == 800
    asyncio.run(bot.espn.close())
