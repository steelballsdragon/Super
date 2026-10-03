"""Your real bets and bankroll: the "I placed it" button on a slip logs your stake and price,
the bets settle with the slip, and /bankroll shows your balance, results and what to stake.

Staking is in units: a unit is a small share of your current bankroll (1-3% by style), so
stakes shrink after losses and grow after wins. Longer shots get smaller stakes: a bet that
hits about half the time gets a full unit, a Lotto a quarter. Money is personal, so each
person's bankroll and bets are their own and only shown to them.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from itertools import combinations

import discord

from .espn import EASTERN
from .limits import fitted
from .props import american

STYLES = {  # name, share of the bankroll in one unit
    "careful": ("Careful", 0.01),
    "standard": ("Standard", 0.02),
    "aggressive": ("Aggressive", 0.03),
}
DEFAULT_STYLE = "standard"
DAILY_LIMIT = 0.10  # stop for the day once this share of the bankroll is staked
BIG_STAKE_UNITS = 3  # a single stake above this many units gets a warning
DRAWDOWN_WARNING = 0.25  # warn when the balance is this far below its peak
MIN_STAKE = 0.10  # FanDuel's minimum


def units_for(chance: float) -> float:
    """Units to stake on a bet that returns something with this chance: smaller for longer shots."""
    if chance >= 0.30:
        return 1.0
    if chance >= 0.05:
        return 0.5
    return 0.25


def parse_price(text: str) -> float | None:
    """A price as decimal odds: American (+450, 450, -120) or decimal (5.5). None if it isn't one."""
    text = text.strip().replace(" ", "")
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
        return None
    value = float(text)
    if "." in text and not text.startswith(("+", "-")) and 1.0 < value < 100:
        return value  # decimal odds
    if value >= 100:
        return 1 + value / 100
    if value <= -100:
        return 1 + 100 / -value
    return None


def american_price(decimal: float) -> str:
    if decimal >= 2:
        return f"+{round(100 * (decimal - 1))}"
    return f"-{round(100 / (decimal - 1))}"


def money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def signed(x: float) -> str:
    return f"+{money(x)}" if x >= 0 else money(x)


def _round_stake(x: float) -> float:
    return max(MIN_STAKE, math.floor(x * 10 + 1e-9) / 10)


def _chance_any(parlay: dict) -> float:
    """The bot's chance this bet returns something: every leg for a parlay, any pair for a round robin."""
    probs = [leg["probability"] for leg in parlay["legs"]]
    if size := parlay.get("round_robin"):
        from .props import chance_at_least
        return chance_at_least(probs, size)
    return math.prod(probs)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, EASTERN).date().isoformat()


def bet_profit(bet: dict, parlay: dict) -> float:
    """What a settled bet won or lost. Void legs come off like at the book: a parlay is repriced
    without them (using the bot's fair price for the void leg), a round-robin pair with one void
    leg becomes a single."""
    legs = parlay["legs"]
    if size := parlay.get("round_robin"):
        prices, per_bet, total = bet["prices"], bet["stake"], 0.0
        for combo in combinations(range(len(legs)), size):
            statuses = [legs[i]["status"] for i in combo]
            if any(s not in ("hit", "void") for s in statuses):
                total -= per_bet  # a miss, or a leg that never got graded
                continue
            payout = math.prod(prices[i] for i in combo if legs[i]["status"] == "hit")
            total += per_bet * (payout - 1)  # all void: payout 1, the stake comes back
        return round(total, 2)
    if parlay["status"] == "lost":
        return -bet["stake"]
    if parlay["status"] == "void":
        return 0.0
    price = bet["price"]
    for leg in legs:
        if leg["status"] == "void":
            price *= leg["probability"]  # take that leg's (fair) price back out
    return round(bet["stake"] * (max(price, 1.0) - 1), 2)


class BetBook:
    """Everyone's bankroll and logged bets, kept with the parlays in record.json."""

    def __init__(self, state) -> None:
        self._state = state

    # ----- bankroll -----

    def bankroll(self, user_id: int) -> dict | None:
        return self._state.get("bankrolls", str(user_id))

    def set_bankroll(self, user_id: int, amount: float, style: str | None = None) -> dict:
        """Starts the bankroll over at this amount; results before now no longer count toward it."""
        old = self.bankroll(user_id) or {}
        entry = {"start": round(amount, 2), "added": 0.0, "since": time.time(),
                 "style": style or old.get("style", DEFAULT_STYLE)}
        self._state.set("bankrolls", str(user_id), entry)
        return entry

    def add_money(self, user_id: int, amount: float) -> dict | None:
        entry = self.bankroll(user_id)
        if entry is None:
            return None
        entry = {**entry, "added": round(entry.get("added", 0.0) + amount, 2)}
        self._state.set("bankrolls", str(user_id), entry)
        return entry

    def set_style(self, user_id: int, style: str) -> dict | None:
        entry = self.bankroll(user_id)
        if entry is None:
            return None
        entry = {**entry, "style": style}
        self._state.set("bankrolls", str(user_id), entry)
        return entry

    def balance(self, user_id: int) -> float | None:
        """The bankroll now: what you started with, plus money added, plus settled results since."""
        entry = self.bankroll(user_id)
        if entry is None:
            return None
        settled = sum(b["profit"] for b in self.bets(user_id)
                      if b["status"] != "pending" and b["placed"] >= entry["since"])
        return round(entry["start"] + entry.get("added", 0.0) + settled, 2)

    def unit(self, user_id: int) -> float | None:
        entry, balance = self.bankroll(user_id), self.balance(user_id)
        if entry is None or balance is None or balance <= 0:
            return None
        return balance * STYLES.get(entry["style"], STYLES[DEFAULT_STYLE])[1]

    def suggested_stake(self, user_id: int, parlay: dict) -> float | None:
        """The plan's stake for this slip (per bet for a round robin), or None without a bankroll."""
        unit = self.unit(user_id)
        if unit is None:
            return None
        stake = unit * units_for(_chance_any(parlay))
        if size := parlay.get("round_robin"):
            stake /= math.comb(len(parlay["legs"]), size)  # the units cover all the pairs together
        return _round_stake(stake)

    # ----- bets -----

    def bets(self, user_id: int | None = None) -> list[dict]:
        out = [b for _, b in self._state.items("bets") if user_id is None or b["user"] == user_id]
        return sorted(out, key=lambda b: b["placed"])

    def get(self, parlay_id: str, user_id: int) -> dict | None:
        return self._state.get("bets", f"{parlay_id}:{user_id}")

    def place(self, parlay: dict, user_id: int, stake: float, price: float | None = None,
              prices: list[float] | None = None) -> dict:
        """Logs (or changes) your bet on a slip. stake is per bet for a round robin."""
        key = f"{parlay['id']}:{user_id}"
        old = self._state.get("bets", key) or {}
        bet = {"parlay": parlay["id"], "user": user_id, "stake": round(stake, 2), "placed": old.get("placed", time.time()),
               "league": parlay["league"], "style": parlay["style"], "status": "pending", "profit": 0.0,
               "fair": _chance_any(parlay), "legs": len(parlay["legs"])}
        if size := parlay.get("round_robin"):
            bet.update(prices=prices, bets=math.comb(len(parlay["legs"]), size), round_robin=size)
        else:
            bet["price"] = price
        self._state.set("bets", key, bet)
        return bet

    def remove(self, parlay_id: str, user_id: int) -> None:
        self._state.delete("bets", f"{parlay_id}:{user_id}")

    def settle(self, parlay: dict) -> list[dict]:
        """Settles everyone's bets on a slip that just finished; returns them."""
        done = []
        for key, bet in self._state.items("bets"):
            if bet["parlay"] != parlay["id"] or bet["status"] != "pending":
                continue
            bet = {**bet, "profit": bet_profit(bet, parlay)}
            bet["status"] = "won" if bet["profit"] > 0 else "lost" if bet["profit"] < 0 else "void"
            bet["settled"] = time.time()
            self._state.set("bets", key, bet)
            done.append(bet)
        return done

    def total_staked(self, bet: dict) -> float:
        return bet["stake"] * bet.get("bets", 1)

    def staked_today(self, user_id: int, now: float | None = None) -> float:
        today = _day(now or time.time())
        return sum(self.total_staked(b) for b in self.bets(user_id) if _day(b["placed"]) == today)

    def peak(self, user_id: int) -> float | None:
        """The highest the balance has been since the bankroll was set (counting money added from the start)."""
        entry = self.bankroll(user_id)
        if entry is None:
            return None
        running = best = entry["start"] + entry.get("added", 0.0)
        settled = [b for b in self.bets(user_id) if b["status"] != "pending" and b["placed"] >= entry["since"]]
        for b in sorted(settled, key=lambda b: b.get("settled", b["placed"])):
            running += b["profit"]
            best = max(best, running)
        return best


def edge(bet: dict) -> float | None:
    """The bot's expected return per $1 at the price you got (0.12 = +12%). None if it can't tell."""
    if bet.get("round_robin"):
        return None  # shown per pick in the reply instead
    if not bet.get("price"):
        return None
    return bet["fair"] * bet["price"] - 1


def round_robin_edge(parlay: dict, prices: list[float]) -> float:
    """Expected return per $1 across all the pairs, from the bot's chances and your prices."""
    legs, size = parlay["legs"], parlay["round_robin"]
    combos = list(combinations(range(len(legs)), size))
    total = sum(math.prod(legs[i]["probability"] * prices[i] for i in combo) for combo in combos)
    return total / len(combos) - 1


def placed_reply(book: BetBook, parlay: dict, bet: dict, user_id: int) -> str:
    """What you see after logging a bet: what it pays, whether the price is worth it, and the plan's checks."""
    lines = []
    if bet.get("round_robin"):
        total = book.total_staked(bet)
        prices = bet["prices"]
        best = max(math.prod(prices[i] for i in combo) for combo in combinations(range(len(prices)), bet["round_robin"]))
        lines.append(f"✅ Logged: **{bet['bets']} bets × {money(bet['stake'])} = {money(total)}** "
                     f"(best pair pays {money(bet['stake'] * best)}).")
        value = round_robin_edge(parlay, prices)
        worse = [str(i) for i, (leg, p) in enumerate(zip(parlay["legs"], prices), 1) if leg["probability"] * p < 1]
        if worse:
            lines.append(f"⚠️ Pick{'s' if len(worse) > 1 else ''} {', '.join(worse)} pay less than fair: "
                         "no edge there by the bot's numbers.")
    else:
        total = bet["stake"]
        lines.append(f"✅ Logged: **{money(total)} at {american_price(bet['price'])}** "
                     f"(to win {money(total * (bet['price'] - 1))}).")
        value = edge(bet) or 0.0
    if value > 0:
        lines.append(f"📈 Edge at your price: **+{value:.0%}** by the bot's numbers (fair {american(bet['fair'])} for any return).")
    else:
        lines.append(f"📉 Edge at your price: **{value:.0%}**: the bot's numbers say this price is worse than fair. "
                     "Shop for a better price or skip it.")
    balance, unit = book.balance(user_id), book.unit(user_id)
    if balance is None or unit is None:
        lines.append("💵 Set your bankroll with `/bankroll start:` and I'll suggest stakes and track your balance.")
        return "\n".join(lines)
    units, plan = total / unit, units_for(_chance_any(parlay))
    lines.append(f"💵 That's **{units:.2g} units** ({total / balance:.1%} of your {money(balance)} bankroll). "
                 f"The plan for this bet: {plan:g} unit{'s' if plan > 1 else ''} = {money(plan * unit)}.")
    if units > BIG_STAKE_UNITS:
        lines.append(f"⚠️ That's more than {BIG_STAKE_UNITS} units on one bet. A cold run will hurt.")
    today = book.staked_today(user_id)
    if today > DAILY_LIMIT * balance:
        lines.append(f"🛑 You've staked {money(today)} today, over the plan's daily limit of "
                     f"{money(DAILY_LIMIT * balance)} ({DAILY_LIMIT:.0%}). Time to stop for today.")
    return "\n".join(lines)


def _price_hint(parlay: dict) -> str:
    if parlay.get("round_robin"):
        return "e.g. " + ", ".join(american(leg["probability"]) for leg in parlay["legs"]) + " (fair prices)"
    return f"e.g. +1250 (fair price {american(math.prod(leg['probability'] for leg in parlay['legs']))})"


class PlacedModal(discord.ui.Modal):
    def __init__(self, book: BetBook, parlay: dict, user_id: int):
        rr = bool(parlay.get("round_robin"))
        super().__init__(title="Log your round robin" if rr else "Log your bet", timeout=600)
        self.book, self.parlay = book, parlay
        old = book.get(parlay["id"], user_id)
        stake = old["stake"] if old else book.suggested_stake(user_id, parlay)
        self.stake = discord.ui.TextInput(
            label="Stake per bet ($)" if rr else "Stake ($)", max_length=12,
            default=f"{stake:.2f}" if stake else None, placeholder="0 removes the bet")
        if rr:
            default = ", ".join(american_price(p) for p in old["prices"]) if old and old.get("prices") else None
            label = f"Each pick's price, in order ({len(parlay['legs'])})"
        else:
            default = american_price(old["price"]) if old and old.get("price") else None
            label = "Price you got"
        self.price = discord.ui.TextInput(label=label[:45], max_length=100, required=False,
                                          default=default, placeholder=_price_hint(parlay)[:100])
        self.add_item(self.stake)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        try:
            stake = float(self.stake.value.strip().lstrip("$").replace(",", ""))
        except ValueError:
            await interaction.response.send_message("The stake should be a number, like 5 or 2.50.", ephemeral=True)
            return
        if stake <= 0:
            self.book.remove(self.parlay["id"], user_id)
            await interaction.response.send_message("Removed your bet on this slip.", ephemeral=True)
            return
        if stake > 1_000_000:
            await interaction.response.send_message("That stake looks off. Try again.", ephemeral=True)
            return
        if self.parlay.get("round_robin"):
            parts = [p for p in re.split(r"[,\s/]+", self.price.value) if p]
            prices = [parse_price(p) for p in parts]
            if len(prices) != len(self.parlay["legs"]) or None in prices:
                await interaction.response.send_message(
                    f"Enter each pick's price in order, {len(self.parlay['legs'])} of them, like "
                    f"`{_price_hint(self.parlay).removeprefix('e.g. ').removesuffix(' (fair prices)')}`.", ephemeral=True)
                return
            bet = self.book.place(self.parlay, user_id, stake, prices=prices)
        else:
            price = parse_price(self.price.value or "")
            if price is None:
                await interaction.response.send_message(
                    "Enter the price the book gave you, like +1250 or -110.", ephemeral=True)
                return
            bet = self.book.place(self.parlay, user_id, stake, price=price)
        await interaction.response.send_message(placed_reply(self.book, self.parlay, bet, user_id), ephemeral=True)


class PlacedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"placed:(?P<pid>[0-9a-f]{8})"):
    """The "I placed it" button under a slip. It works after restarts: the slip's id is in the button."""

    def __init__(self, parlay_id: str):
        super().__init__(discord.ui.Button(label="I placed it", emoji="💵", style=discord.ButtonStyle.success,
                                           custom_id=f"placed:{parlay_id}"))
        self.parlay_id = parlay_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        parlay = bot.records.get("parlays", self.parlay_id)
        if parlay is None:
            await interaction.response.send_message("I no longer have this slip.", ephemeral=True)
            return
        if parlay["status"] != "pending":
            await interaction.response.send_message("This slip is already settled.", ephemeral=True)
            return
        await interaction.response.send_modal(PlacedModal(bot.bets, parlay, interaction.user.id))


def placed_view(parlay_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PlacedButton(parlay_id))
    return view


def result_lines(bets: list[dict]) -> list[str]:
    """The settled bets for a slip's result post: who won or lost what."""
    return [f"💵 <@{b['user']}>: **{signed(b['profit'])}** on {money(b['stake'] * b.get('bets', 1))}" for b in bets]


@fitted
def bankroll_embed(book: BetBook, user_id: int, name: str) -> discord.Embed:
    """Your bankroll, stakes, results by bet type and recent bets (shown only to you)."""
    entry, balance, unit = book.bankroll(user_id), book.balance(user_id), book.unit(user_id)
    bets = book.bets(user_id)
    embed = discord.Embed(title=f"💵 {name}'s bankroll", color=discord.Color.gold())
    lines = []
    if entry is None:
        lines.append("No bankroll set yet. Use `/bankroll start:` with the money you've put aside for betting "
                     "(only what you can afford to lose), and I'll size every stake from it.")
    else:
        start = entry["start"] + entry.get("added", 0.0)
        style_name, share = STYLES.get(entry["style"], STYLES[DEFAULT_STYLE])
        lines.append(f"**Balance: {money(balance)}** ({signed(balance - start)} since you set {money(start)})")
        if unit:
            lines.append(f"**Unit: {money(unit)}** ({share:.0%} of your balance, {style_name}). Stakes per slip: "
                         f"Safe-type (30%+ to hit) 1 unit = {money(unit)} · Big 0.5 unit = {money(unit / 2)} · "
                         f"Lotto and long shots 0.25 unit = {money(unit / 4)}. Round robins split it across the pairs.")
        else:
            lines.append("⚠️ Your balance is at zero. Set a new bankroll when you're ready.")
        open_bets = [b for b in bets if b["status"] == "pending"]
        if open_bets:
            lines.append(f"At risk now: {money(sum(book.total_staked(b) for b in open_bets))} on {len(open_bets)} open bet"
                         + ("s" if len(open_bets) > 1 else ""))
        today = book.staked_today(user_id)
        if balance and balance > 0:
            lines.append(f"Staked today: {money(today)} of the {money(DAILY_LIMIT * balance)} daily limit")
        peak = book.peak(user_id)
        if peak and balance is not None and balance < (1 - DRAWDOWN_WARNING) * peak:
            lines.append(f"⚠️ You're {1 - balance / peak:.0%} below your peak of {money(peak)}. Your units have shrunk "
                         "with the balance, as planned. Consider the Careful style until it recovers.")
    embed.description = "\n".join(lines)
    settled = [b for b in bets if b["status"] != "pending"]
    if settled:
        groups: dict[str, list[dict]] = {}
        for b in settled:
            groups.setdefault("All", []).append(b)
            groups.setdefault(b["style"], []).append(b)
        rows = []
        for style, group in sorted(groups.items(), key=lambda kv: kv[0] != "All"):
            staked = sum(book.total_staked(b) for b in group)
            profit = sum(b["profit"] for b in group)
            won = sum(b["status"] == "won" for b in group)
            lost = sum(b["status"] == "lost" for b in group)
            rows.append(f"**{style}**: {won}-{lost} · staked {money(staked)} · **{signed(profit)}**"
                        + (f" · ROI {profit / staked:+.0%}" if staked else ""))
        priced = [e for b in settled if (e := edge(b)) is not None]
        if priced:
            better = sum(e > 0 for e in priced)
            rows.append(f"*You got a price better than fair on {better} of {len(priced)} parlays. "
                        "Over time, that's what makes money.*")
        embed.add_field(name="Results", value="\n".join(rows), inline=False)
    if bets:
        recent = []
        for b in bets[-5:][::-1]:
            icon = {"won": "✅", "lost": "❌", "void": "➖"}.get(b["status"], "⏳")
            price = f" at {american_price(b['price'])}" if b.get("price") else f" ({b['bets']} bets)" if b.get("bets") else ""
            result = f" → {signed(b['profit'])}" if b["status"] != "pending" else ""
            recent.append(f"{icon} {b['style']} · {money(book.total_staked(b))}{price}{result}")
        embed.add_field(name="Recent bets", value="\n".join(recent), inline=False)
    embed.set_footer(text="Log a bet with 💵 I placed it under any slip. Only you can see this. "
                          "Bet only what you can afford to lose.")
    return embed
