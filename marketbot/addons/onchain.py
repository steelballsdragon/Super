"""On-chain and crowd data, all from free sources.

- Bitcoin network (mempool.space): fees, hashrate and the next difficulty change.
- Stablecoins (DefiLlama): the dollars parked in crypto and how fast that pile grows; new stablecoins are fuel for
  buying, shrinking supply is money leaving.
- DeFi (DefiLlama): value locked on the biggest chains.
- Funding (Hyperliquid, which also reports Binance and Bybit): what leveraged longs pay shorts every 8 hours. Very
  positive means crowded longs, negative means shorts are paying.
- Whales (Etherscan, with a free ETHERSCAN_API_KEY): the largest USDT/USDC transfers on Ethereum, posted to the
  crypto channel. Etherscan's terms require the "Powered by Etherscan.io APIs" line wherever they're shown.
- Reddit buzz (ApeWisdom): the most-mentioned tickers on r/wallstreetbets and the crypto subreddits, and what's rising.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
from discord import app_commands

from .. import embeds as E
from ..apis import Api, ApiError, env_key
from ..briefs import Post
from ..limits import clip, fit_embed
from ..storage import read_json, write_json
from . import Feature

log = logging.getLogger(__name__)

KIND = "crypto"
ETHERSCAN_NOTE = "Powered by Etherscan.io APIs"
TOKENS = {  # symbol: (contract, decimals, smallest transfer worth a post, in tokens)
    "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6, 100_000_000),
    "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, 100_000_000),
}
WALLETS = {  # Etherscan's public name tags, checked 2026-10-07
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit",
    "0x5754284f345afc66a98fbb0a0afe71e0f007b949": "Tether Treasury",
}
ZERO = "0x" + "0" * 40
FUNDING_COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
VENUES = {"HlPerp": "Hyperliquid", "BinPerp": "Binance", "BybitPerp": "Bybit"}
PAGES = 3  # Etherscan answers at most 1,000 transfers a call; USDT moves about that many every few minutes
KEEP_WHALES = 2 * 86400


def num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def money(v: float) -> str:
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            x = v / div
            return f"${x:.0f}{unit}" if abs(x) >= 100 else f"${x:.1f}".rstrip("0").rstrip(".") + unit
    return f"${v:,.0f}"


def signed_money(v: float) -> str:
    return ("+" if v >= 0 else "-") + money(abs(v))


# ----- Bitcoin network -----

def btc_network(fees: dict, hashrate: dict, adjust: dict) -> dict:
    eta = num(adjust.get("estimatedRetargetDate")) / 1000  # milliseconds, unlike previousTime
    return {"fast": num(fees.get("fastestFee")), "hour": num(fees.get("hourFee")),
            "economy": num(fees.get("economyFee")), "hashrate": num(hashrate.get("currentHashrate")) / 1e18,
            "change": num(adjust.get("difficultyChange")), "blocks": int(num(adjust.get("remainingBlocks"))),
            "progress": num(adjust.get("progressPercent")), "eta": eta}


def network_text(n: dict) -> str:
    fee = lambda v: f"{v:g}"  # noqa: E731  (sub-1 sat/vB fees happen)
    busy = "quiet" if n["fast"] <= 5 else "busy" if n["fast"] <= 30 else "very busy"
    when = f"<t:{int(n['eta'])}:R>" if n["eta"] > 0 else f"in {n['blocks']:,} blocks"
    return (f"Fees **{fee(n['fast'])}** sat/vB fast · {fee(n['hour'])} within an hour ({busy})\n"
            f"Hashrate **{n['hashrate']:,.0f} EH/s**\n"
            f"Next difficulty change **{n['change']:+.1f}%** {when} ({n['progress']:.0f}% through the period)")


# ----- stablecoins and DeFi -----

def stable_supply(data: dict) -> dict:
    """Total USD stablecoins now and 1/7/30 days ago, and the biggest ones' weekly change."""
    total = {"now": 0.0, "day": 0.0, "week": 0.0, "month": 0.0}
    fields = {"now": "circulating", "day": "circulatingPrevDay", "week": "circulatingPrevWeek",
              "month": "circulatingPrevMonth"}
    coins = []
    for a in data.get("peggedAssets") or []:
        if a.get("pegType") != "peggedUSD":
            continue
        vals = {k: num((a.get(f) or {}).get("peggedUSD")) for k, f in fields.items()}
        for k, v in vals.items():
            total[k] += v
        coins.append((str(a.get("symbol") or "?"), vals["now"], vals["now"] - vals["week"]))
    coins.sort(key=lambda c: -c[1])
    return {**total, "top": coins[:5]}


def stable_text(s: dict) -> str:
    def change(k):
        return f"{signed_money(s['now'] - s[k])} ({(s['now'] / s[k] - 1) if s[k] else 0:+.1%})"

    week = s["now"] - s["week"]
    mood = ("new money coming in" if week > 1e9 else "money leaving" if week < -1e9 else "steady")
    lines = [f"**{money(s['now'])}** in USD stablecoins: {mood}",
             f"1 day {change('day')} · 7 days {change('week')} · 30 days {change('month')}"]
    if s["top"]:
        lines.append(" · ".join(f"{clip(sym, 8)} {money(v)} ({signed_money(d)} wk)" for sym, v, d in s["top"][:3]))
    return "\n".join(lines)


def top_chains(data, n: int = 6) -> list[tuple[str, float]]:
    rows = data.get("data") if isinstance(data, dict) else data
    out = [(str(c.get("name") or "?"), num(c.get("tvl"))) for c in rows or [] if isinstance(c, dict)]
    return sorted(out, key=lambda c: -c[1])[:n]


# ----- funding -----

def fundings(predicted) -> dict[str, dict[str, float]]:
    """{coin: {venue: % per 8 hours}} from Hyperliquid's predictedFundings."""
    out: dict[str, dict[str, float]] = {}
    for item in predicted or []:
        if not isinstance(item, list) or len(item) != 2 or item[0] not in FUNDING_COINS:
            continue
        rates = {}
        for venue in item[1] or []:
            if not isinstance(venue, list) or len(venue) != 2 or not isinstance(venue[1], dict):
                continue
            name, info = venue
            if name not in VENUES or info.get("fundingRate") in (None, ""):
                continue
            hours = num(info.get("fundingIntervalHours"), 1.0 if name == "HlPerp" else 8.0) or 8.0
            rates[VENUES[name]] = num(info["fundingRate"]) / hours * 8 * 100
        if rates:
            out[item[0]] = rates
    return {c: out[c] for c in FUNDING_COINS if c in out}


def funding_text(f: dict[str, dict[str, float]]) -> str:
    lines = []
    for coin, rates in f.items():
        avg = sum(rates.values()) / len(rates)
        mood = ("🔥 crowded longs" if avg >= 0.03 else "longs paying" if avg > 0.005 else
                "🧊 shorts paying" if avg < -0.005 else "neutral")
        lines.append(f"**{coin}** {avg:+.3f}% ({mood}) · " +
                     " · ".join(f"{v}: {r:+.3f}%" for v, r in rates.items()))
    return "\n".join(lines)


# ----- whales -----

@dataclass
class Transfer:
    token: str
    amount: float
    frm: str
    to: str
    tx: str
    block: int
    at: float

    @property
    def key(self) -> str:
        return f"{self.tx}:{self.frm}:{self.to}:{self.amount:.0f}"


def label(addr: str) -> str:
    a = addr.lower()
    if a == ZERO:
        return "mint/burn"
    return WALLETS.get(a) or f"{a[:6]}…{a[-4:]}"


def transfers(rows, token: str) -> list[Transfer]:
    """Etherscan tokentx rows (all strings) as transfers of `token` in whole units."""
    _, decimals, _ = TOKENS[token]
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            amount = int(r.get("value") or 0) / 10 ** int(r.get("tokenDecimal") or decimals)
            out.append(Transfer(token, amount, str(r.get("from") or "").lower(), str(r.get("to") or "").lower(),
                                str(r.get("hash") or ""), int(r.get("blockNumber") or 0), num(r.get("timeStamp"))))
        except (TypeError, ValueError):
            continue
    return out


def whale_kind(t: Transfer) -> tuple[str, str]:
    """(emoji, what it likely means) from the wallets' labels."""
    src, dst = label(t.frm), label(t.to)
    exchanges = set(WALLETS.values()) - {"Tether Treasury"}
    if t.frm == ZERO or src == "Tether Treasury":
        return "🖨️", f"{t.token} minted/issued"
    if t.to == ZERO:
        return "🔥", f"{t.token} burned/redeemed"
    if dst in exchanges and src not in exchanges:
        return "📥", f"to {dst} (buying power arriving)"
    if src in exchanges and dst not in exchanges:
        return "📤", f"out of {src}"
    return "🐋", "moved"


def whale_line(t: Transfer) -> str:
    emoji, what = whale_kind(t)
    return (f"{emoji} **{money(t.amount)} {t.token}** {label(t.frm)} → {label(t.to)} · {what} · "
            f"[tx](https://etherscan.io/tx/{t.tx}) <t:{int(t.at)}:R>")


def whales_embed(found: list[Transfer], title: str = "🐋 Whale transfers") -> discord.Embed:
    found = sorted(found, key=lambda t: -t.amount)
    e = discord.Embed(title=title, color=E.BLUE, description="\n".join(whale_line(t) for t in found[:10]))
    if len(found) > 10:
        e.description += f"\n…and {len(found) - 10} more"
    e.set_footer(text=f"{ETHERSCAN_NOTE} · Ethereum USDT/USDC moves of $100M+ · unknown wallets shortened · "
                      "not financial advice")
    return fit_embed(e)


# ----- Reddit buzz -----

def buzz_rows(data: dict) -> list[dict]:
    out = []
    for r in (data or {}).get("results") or []:
        try:
            before = r.get("mentions_24h_ago")
            out.append({"ticker": str(r.get("ticker") or "?").removesuffix(".X"),
                        "name": html.unescape(str(r.get("name") or "")), "rank": int(r.get("rank") or 0),
                        "rank_before": int(r["rank_24h_ago"]) if r.get("rank_24h_ago") not in (None, "") else None,
                        "mentions": int(r.get("mentions") or 0),
                        "before": int(before) if before not in (None, "") else None,
                        "upvotes": int(r.get("upvotes") or 0)})
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda r: r["rank"] or 10**6)


def rising(rows: list[dict], n: int = 5) -> list[dict]:
    """Tickers whose mentions at least doubled (and that people actually talk about)."""
    hot = [r for r in rows if r["mentions"] >= 25 and (r["before"] is None and r["mentions"] >= 50 or
                                                       r["before"] and r["mentions"] / r["before"] >= 2)]
    return sorted(hot, key=lambda r: -(r["mentions"] / (r["before"] or 1)))[:n]


def buzz_embed(rows: list[dict], market: str) -> discord.Embed:
    title = "🗣️ Reddit buzz: " + ("crypto" if market == "crypto" else "stocks")
    e = discord.Embed(title=title, color=E.PURPLE)
    if not rows:
        e.description = "ApeWisdom had nothing for this right now."
        return e

    def move(r):
        if r["rank_before"] is None:
            return "🆕"
        d = r["rank_before"] - r["rank"]
        return f"▲{d}" if d > 0 else f"▼{-d}" if d < 0 else "＝"

    def change(r):
        return f"{r['mentions'] / r['before'] - 1:+.0%}" if r["before"] else "new"

    def plural(n):
        return f"{n:,} mention" + ("" if n == 1 else "s")

    e.description = "\n".join(f"`{r['rank']:>2}.` **{clip(r['ticker'], 10)}** {clip(r['name'], 26)} · "
                              f"{plural(r['mentions'])} ({change(r)}) {move(r)}" for r in rows[:15])
    hot = rising(rows[:100])
    if hot:
        e.add_field(name="🚀 Rising fastest", value=" · ".join(
            f"**{clip(r['ticker'], 10)}** {r['mentions']:,} ({change(r)})" for r in hot), inline=False)
    e.set_footer(text="Data: ApeWisdom · mentions in the last 24h on r/wallstreetbets, r/stocks, r/CryptoCurrency and "
                      "others vs the 24h before · hype is not a reason to buy · not financial advice")
    return fit_embed(e)


def onchain_embed(parts: dict) -> discord.Embed:
    e = discord.Embed(title="⛓️ On-chain & crypto pulse", color=E.GOLD,
                      timestamp=datetime.now(timezone.utc))
    if parts.get("network"):
        e.add_field(name="₿ Bitcoin network", value=network_text(parts["network"]), inline=False)
    if parts.get("stables"):
        e.add_field(name="💵 Stablecoins", value=stable_text(parts["stables"]), inline=False)
    if parts.get("funding"):
        e.add_field(name="⚖️ Funding (per 8h)", value=funding_text(parts["funding"]), inline=False)
    if parts.get("chains"):
        e.add_field(name="🏦 DeFi value locked", value=" · ".join(f"**{clip(n, 14)}** {money(v)}"
                                                                for n, v in parts["chains"]), inline=False)
    if parts.get("whales"):
        e.add_field(name="🐋 Biggest stablecoin moves (24h)",
                    value=clip("\n".join(whale_line(t) for t in parts["whales"][:5]), 1024), inline=False)
    missing = parts.get("missing") or []
    if missing:
        e.description = "⚠️ Unavailable right now: " + ", ".join(missing)
    sources = ["mempool.space", "DefiLlama", "Hyperliquid"] + ([ETHERSCAN_NOTE] if parts.get("whales") else [])
    e.set_footer(text="Data: " + " · ".join(sources) + " · not financial advice")
    return fit_embed(e)


class OnchainDesk(Feature):
    name = "onchain"
    help_group = "⛓️ Crypto & crowd"

    def __init__(self, bot):
        super().__init__(bot)
        http = getattr(bot.engine.data, "http", None)
        if http is None:
            raise RuntimeError("no shared Http")
        self.http = http
        state = bot.data_dir / "apis.json"
        self.mempool = Api("mempool.space", http, "https://mempool.space/api", needs_key=False, limits=((20, 60.0),))
        self.llama = Api("DefiLlama", http, "https://api.llama.fi", needs_key=False, limits=((20, 60.0),))
        self.ape = Api("ApeWisdom", http, "https://apewisdom.io/api/v1.0", needs_key=False, limits=((10, 60.0),))
        self.etherscan = Api("Etherscan", http, "https://api.etherscan.io/v2", env_key("ETHERSCAN_API_KEY"),
                             key_param="apikey", limits=((3, 1.0), (90_000, 86400.0)), state_file=state)
        self.path = bot.data_dir / "onchain.json"
        data = read_json(self.path, {})
        self.blocks: dict[str, int] = data.get("blocks") or {}
        self.whales: list[dict] = data.get("whales") or []
        self.cache: dict[str, tuple[float, object]] = {}

    def jobs(self):
        return [("whales", 180, self.job_whales)] if self.etherscan.enabled else []

    def help(self):
        return [("onchain", "Bitcoin network, stablecoin money flows, funding rates, DeFi and whale moves"),
                ("buzz", "the most-talked-about tickers on Reddit and what's rising")]

    def status(self):
        lines = [f"**mempool.space / DefiLlama / ApeWisdom** {self.mempool.calls + self.llama.calls + self.ape.calls}"
                 " calls since start"]
        lines.append(f"**Etherscan** {self.etherscan.status_line()}")
        return lines

    def save(self) -> None:
        cutoff = time.time() - KEEP_WHALES
        self.whales = [w for w in self.whales if w["at"] >= cutoff][-200:]
        write_json(self.path, {"blocks": self.blocks, "whales": self.whales})

    async def cached(self, name: str, ttl: float, fetch):
        hit = self.cache.get(name)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = await fetch()
        self.cache[name] = (time.monotonic(), value)
        return value

    # ----- sources -----

    async def network(self) -> dict:
        async def fetch():
            fees = await self.mempool.get("v1/fees/recommended", wait=5)
            rate = await self.mempool.get("v1/mining/hashrate/3d", wait=5)
            adjust = await self.mempool.get("v1/difficulty-adjustment", wait=5)
            return btc_network(fees, rate, adjust)
        return await self.cached("network", 120, fetch)

    async def stables(self) -> dict:
        async def fetch():
            return stable_supply(await self.llama.get("https://stablecoins.llama.fi/stablecoins",
                                                      {"includePrices": "true"}, wait=5, timeout=30))
        return await self.cached("stables", 1800, fetch)

    async def chains(self) -> list:
        async def fetch():
            return top_chains(await self.llama.get("v2/chains", wait=5, timeout=30))
        return await self.cached("chains", 1800, fetch)

    async def funding(self) -> dict:
        async def fetch():
            return fundings(await self.http.post_json("https://api.hyperliquid.xyz/info",
                                                      {"type": "predictedFundings"}, source="Hyperliquid",
                                                      timeout=15))
        return await self.cached("funding", 300, fetch)

    async def buzz(self, market: str) -> list[dict]:
        async def fetch():
            return buzz_rows(await self.ape.get(f"filter/all-{market}/page/1", wait=5))
        return await self.cached(f"buzz:{market}", 900, fetch)

    async def tokentx(self, token: str, start: int, end: int | None = None) -> list:
        contract = TOKENS[token][0]
        data = await self.etherscan.get("api", {"chainid": 1, "module": "account", "action": "tokentx",
                                             "contractaddress": contract, "startblock": start,
                                             "endblock": end or 99_999_999, "page": 1, "offset": 1000,
                                             "sort": "desc"}, wait=5, timeout=30)
        return etherscan_result(self.etherscan, data)

    async def head(self) -> int:
        data = await self.etherscan.get("api", {"chainid": 1, "module": "proxy", "action": "eth_blockNumber"}, wait=5)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, str) or not result.startswith("0x"):
            etherscan_result(self.etherscan, data)
            raise ApiError("Etherscan", "no block number", "bad")
        return int(result, 16)

    # ----- the whale job -----

    async def job_whales(self) -> None:
        if not self.etherscan.enabled:
            return
        if not self.blocks:  # first run: start from now, the past isn't news
            head = await self.head()
            self.blocks = {t: head for t in TOKENS}
            self.save()
            return
        found: list[Transfer] = []
        for token, (_, _, minimum) in TOKENS.items():
            start = self.blocks.get(token, 0) + 1
            seen: set[str] = set()
            end = None
            top = start - 1
            try:
                for _ in range(PAGES):
                    rows = transfers(await self.tokentx(token, start, end), token)
                    for t in rows:
                        top = max(top, t.block)
                        if t.amount >= minimum and t.key not in seen:
                            seen.add(t.key)
                            found.append(t)
                    if len(rows) < 1000:
                        break
                    end = min(t.block for t in rows)  # older ones were cut off: page back from the oldest block
                    if end <= start:
                        break
            except ApiError as exc:
                log.info("Etherscan %s transfers unavailable: %s", token, exc)
                continue  # its block stays put, so the next run catches up
            self.blocks[token] = top
        known = {w["key"] for w in self.whales}
        new = [t for t in found if t.key not in known]
        for t in new:
            self.whales.append({"key": t.key, "token": t.token, "amount": t.amount, "from": t.frm, "to": t.to,
                                "tx": t.tx, "block": t.block, "at": t.at or time.time()})
        self.save()
        if new:
            embed = whales_embed(new)
            for cid, cfg in self.bot.channels.of_kind(KIND):
                if cfg.alerts:
                    await self.bot.send(cid, Post([embed]))

    def recent_whales(self, hours: float = 24) -> list[Transfer]:
        cutoff = time.time() - hours * 3600
        out = [Transfer(w["token"], w["amount"], w["from"], w["to"], w["tx"], w["block"], w["at"])
               for w in self.whales if w["at"] >= cutoff]
        return sorted(out, key=lambda t: -t.amount)

    async def pulse(self) -> dict:
        parts: dict = {"missing": []}
        for key, label_, fetch in (("network", "Bitcoin network", self.network), ("stables", "stablecoins",
                                   self.stables), ("funding", "funding", self.funding),
                                   ("chains", "DeFi", self.chains)):
            try:
                parts[key] = await fetch()
            except Exception as exc:
                log.info("/onchain %s unavailable: %s", key, exc)
                parts["missing"].append(label_)
        if self.etherscan.enabled:
            parts["whales"] = self.recent_whales()
        return parts

    def register(self, tree) -> None:
        @tree.command(name="onchain", description="Bitcoin network, stablecoin flows, funding rates, DeFi and whales")
        async def onchain_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            parts = await self.pulse()
            if len(parts["missing"]) == 4:
                await interaction.followup.send("⚠️ The on-chain sources aren't answering right now; try again "
                                                "in a minute.")
                return
            await interaction.followup.send(embed=onchain_embed(parts))

        @tree.command(name="buzz", description="The most-talked-about tickers on Reddit, and what's rising")
        @app_commands.describe(market="Stocks or crypto")
        @app_commands.choices(market=[app_commands.Choice(name="Stocks", value="stocks"),
                                      app_commands.Choice(name="Crypto", value="crypto")])
        async def buzz_cmd(interaction: discord.Interaction, market: app_commands.Choice[str] | None = None):
            await interaction.response.defer(thinking=True)
            which = market.value if market else "stocks"
            await interaction.followup.send(embed=buzz_embed(await self.buzz(which), which))


def etherscan_result(api: Api, data) -> list:
    """Etherscan answers HTTP 200 even for errors: status "0" with the reason in `result`."""
    if not isinstance(data, dict):
        raise ApiError("Etherscan", "unreadable answer", "bad")
    result = data.get("result")
    if str(data.get("status")) == "1" or isinstance(result, list) and not result:
        return result if isinstance(result, list) else []
    if isinstance(result, list):
        return result
    text = str(result or data.get("message") or "")
    low = text.lower()
    if "api key" in low:
        api.key_rejected = True
        api.last_error = "key rejected"
        raise ApiError("Etherscan", "key rejected", "key")
    if "rate limit" in low:
        api.last_error = "rate limited"
        raise ApiError("Etherscan", "rate limited", "rate")
    if "no transactions" in low or "no records" in low:
        return []
    api.last_error = clip(api.scrub(text), 100)
    raise ApiError("Etherscan", clip(api.scrub(text), 160), "bad")
