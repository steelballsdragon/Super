"""On-chain and crowd data: parsing real mempool.space, DefiLlama, Hyperliquid and ApeWisdom answers (tests/data),
Etherscan's 200-with-an-error answers, and the whale job (first run silent, paging, no repeats)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from marketbot.addons import onchain as O  # noqa: E402
from marketbot.apis import Api, ApiError  # noqa: E402

DATA = Path(__file__).parent / "data"


def load(name):
    return json.loads((DATA / name).read_text())


def test_bitcoin_network():
    m = load("mempool_space.json")
    n = O.btc_network(m["fees"], m["hashrate"], m["adjust"])
    assert n["fast"] == 3 and round(n["hashrate"]) == 986 and n["blocks"] == 1318
    assert n["eta"] == pytest.approx(1792158246.968)  # milliseconds turned into seconds
    text = O.network_text(n)
    assert "**986 EH/s**" in text and "**+5.7%** <t:1792158246:R>" in text and "(quiet)" in text


def test_stablecoin_supply():
    s = O.stable_supply(load("defillama_stablecoins.json"))
    assert [c[0] for c in s["top"]] == ["USDT", "USDC"]
    assert s["now"] == pytest.approx(s["top"][0][1] + s["top"][1][1])
    text = O.stable_text(s)
    assert text.startswith("**$258B** in USD stablecoins: steady")
    assert "7 days -$268M (-0.1%)" in text and "USDT $184B (+$508M wk) · USDC $73.8B (-$776M wk)" in text


def test_top_chains():
    chains = O.top_chains(load("defillama_v2_chains.json"), 3)
    assert [c[0] for c in chains] == ["Ethereum", "Solana", "Base"] and chains[0][1] > chains[1][1]


def test_funding_normalised_to_8_hours():
    f = O.fundings(load("hyperliquid_predicted_fundings.json"))
    assert list(f) == ["BTC", "ETH"]
    assert f["BTC"]["Hyperliquid"] == pytest.approx(0.01)  # 0.00125% an hour
    assert f["BTC"]["Binance"] == pytest.approx(-0.001892)
    assert "**BTC** +0.003% (neutral) · Binance: -0.002% · Hyperliquid: +0.010%" in O.funding_text(f)
    odd = [["BTC", [["BinPerp", {"fundingRate": "0.0004"}], ["BybitPerp", None], ["Other", {"fundingRate": "1"}]]],
           ["PEPE", [["HlPerp", {"fundingRate": "0.01"}]]], "junk"]
    assert O.fundings(odd) == {"BTC": {"Binance": pytest.approx(0.04)}}  # no interval: assume 8 hours
    assert "🔥 crowded longs" in O.funding_text(O.fundings(odd))


def test_reddit_buzz():
    rows = O.buzz_rows(load("apewisdom_all_stocks.json"))
    assert rows[1]["name"] == "SPDR S&P 500 ETF Trust"  # HTML entities undone
    assert [r["ticker"] for r in O.rising(rows)] == ["BULL"]  # 2 → 122 mentions
    coins = O.buzz_rows(load("apewisdom_all_crypto.json"))
    assert coins[0]["ticker"] == "BTC"  # ".X" dropped
    weird = O.buzz_rows({"results": [{"rank": "2", "ticker": "A", "mentions": "60", "rank_24h_ago": None,
                                      "mentions_24h_ago": None}, {"rank": "x"}]})
    assert weird[0]["mentions"] == 60 and weird[0]["before"] is None and O.rising(weird)
    e = O.buzz_embed(rows, "stocks")
    assert "**BULL**" in e.description and "🆕" not in e.description and e.fields[0].name == "🚀 Rising fastest"
    assert "1 mention " in O.buzz_embed([{**rows[0], "mentions": 1}], "stocks").description
    assert O.buzz_embed([], "crypto").description.startswith("ApeWisdom had nothing")


def tx(value, frm="0xaaa0000000000000000000000000000000000001", to=O.ZERO, block=100, h="0xh", dec="6"):
    return {"value": str(value), "tokenDecimal": dec, "from": frm, "to": to, "hash": h, "blockNumber": str(block),
            "timeStamp": "1791400000"}


def test_whale_labels_and_kinds():
    binance = "0x28C6c06298d514Db089934071355E5743bf21d60"
    [t] = O.transfers([tx(250_000_000 * 10**6, to=binance)], "USDT")
    assert t.amount == 250_000_000 and O.whale_kind(t) == ("📥", "to Binance (buying power arriving)")
    [m] = O.transfers([tx(5 * 10**6, frm=O.ZERO, to=binance)], "USDC")
    assert O.whale_kind(m)[1] == "USDC minted/issued"
    assert O.label("0xabcdef0000000000000000000000000000001234") == "0xabcd…1234"
    e = O.whales_embed(O.transfers([tx(10**15)] * 12, "USDT"))
    assert O.ETHERSCAN_NOTE in e.footer.text and "…and 2 more" in e.description


def test_etherscan_errors_come_with_http_200():
    api = Api("Etherscan", http=None, base="https://x.test", key="k", key_param="apikey")
    assert O.etherscan_result(api, {"status": "0", "message": "No transactions found", "result": []}) == []
    assert O.etherscan_result(api, {"status": "1", "message": "OK", "result": [{"a": 1}]}) == [{"a": 1}]
    with pytest.raises(ApiError) as err:
        O.etherscan_result(api, {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"})
    assert err.value.kind == "rate" and api.enabled
    with pytest.raises(ApiError):
        O.etherscan_result(api, {"status": "0", "message": "NOTOK", "result": "Invalid API Key (#err2)"})
    assert not api.enabled


def make_desk(tmp_path, pages):
    from marketbot.channels import ChannelStore
    sent = []

    async def send(cid, post):
        sent.append((cid, post))

    channels = ChannelStore(tmp_path / "channels.json")
    channels.set(5, "crypto")
    bot = SimpleNamespace(channels=channels, send=send, data_dir=tmp_path)
    desk = O.OnchainDesk.__new__(O.OnchainDesk)
    O.Feature.__init__(desk, bot)
    desk.etherscan = SimpleNamespace(enabled=True)
    desk.path = tmp_path / "onchain.json"
    desk.blocks, desk.whales, desk.cache = {}, [], {}
    calls = []

    async def tokentx(token, start, end=None):
        calls.append((token, start, end))
        return pages.get((token, end), [])

    async def head():
        return 500

    desk.tokentx, desk.head = tokentx, head
    return desk, sent, calls


def test_whale_job_first_run_silent_then_pages_and_never_repeats(tmp_path):
    big = 300_000_000 * 10**6
    full = [tx(1, block=600 - i // 20, h=f"0x{i}") for i in range(999)] + [tx(big, block=550, h="0xbig")]
    pages = {("USDT", None): full, ("USDT", 550): [tx(big, block=550, h="0xbig"), tx(big, block=520, h="0xold")],
             ("USDC", None): [tx(50 * 10**6 * 10**6, block=530)]}
    desk, sent, calls = make_desk(tmp_path, pages)
    asyncio.run(desk.job_whales())
    assert desk.blocks == {"USDT": 500, "USDC": 500} and not sent and not calls
    asyncio.run(desk.job_whales())
    assert calls == [("USDT", 501, None), ("USDT", 501, 550), ("USDC", 501, None)]
    [(cid, post)] = sent
    assert cid == 5 and post.embeds[0].description.count("$300M USDT") == 2  # the $50M USDC is too small
    assert desk.blocks == {"USDT": 600, "USDC": 530}
    desk.blocks = {"USDT": 500, "USDC": 500}  # the same transfers again aren't posted twice
    asyncio.run(desk.job_whales())
    assert len(sent) == 1
    saved = O.read_json(tmp_path / "onchain.json", {})
    assert len(saved["whales"]) == 2 and saved["blocks"]["USDT"] == 600
    assert [t.tx for t in desk.recent_whales(24 * 365 * 10)] == ["0xbig", "0xold"]


def test_one_tokens_failure_leaves_its_block_for_the_next_run(tmp_path):
    desk, sent, calls = make_desk(tmp_path, {("USDC", None): [tx(200 * 10**6 * 10**6, block=510)]})
    desk.blocks = {"USDT": 500, "USDC": 500}
    real = desk.tokentx

    async def flaky(token, start, end=None):
        if token == "USDT":
            raise ApiError("Etherscan", "timeout", "network")
        return await real(token, start, end)

    desk.tokentx = flaky
    asyncio.run(desk.job_whales())
    assert desk.blocks == {"USDT": 500, "USDC": 510} and "$200M USDC" in sent[0][1].embeds[0].description


def test_pulse_reports_what_is_missing(tmp_path):
    desk, _, _ = make_desk(tmp_path, {})

    async def boom():
        raise ApiError("x", "down", "network")

    async def network():
        m = load("mempool_space.json")
        return O.btc_network(m["fees"], m["hashrate"], m["adjust"])

    desk.network, desk.stables, desk.funding, desk.chains = network, boom, boom, boom
    desk.etherscan = SimpleNamespace(enabled=False)
    parts = asyncio.run(desk.pulse())
    assert parts["missing"] == ["stablecoins", "funding", "DeFi"]
    e = O.onchain_embed(parts)
    assert [f.name for f in e.fields] == ["₿ Bitcoin network"] and "stablecoins, funding, DeFi" in e.description
    assert O.ETHERSCAN_NOTE not in e.footer.text


def test_money():
    assert [O.money(v) for v in (950, 1500, 2_600_000_000, 314e9, 1.2e12)] == ["$950", "$1.5K", "$2.6B", "$314B",
                                                                               "$1.2T"]
    assert O.signed_money(-409e6) == "-$409M"
