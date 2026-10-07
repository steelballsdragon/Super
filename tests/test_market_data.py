"""Offline tests for where prices come from: the MarketData facade (marketbot/data.py), the on-disk history cache
(marketbot/cache.py), the symbol directory (marketbot/directory.py) and Engine.resolve/suggest.

Nothing here touches the network: MarketData runs on a real Http transport whose backend is a fake that routes each
request by host and path to canned Yahoo, Nasdaq and Coinbase answers (or fails it on purpose)."""

import asyncio
import functools
import gzip
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

np = pytest.importorskip("numpy")

from marketbot import cache as cache_mod, directory as dir_mod  # noqa: E402
from marketbot.cache import (DAY, DISK_TTL, FULL_REFRESH, MEMORY_SYMBOLS, OVERLAP_DAYS, HistoryCache,  # noqa: E402
                             merge, source_of, splice)
from marketbot.data import MIN_CAP, DataUnavailable, MarketData, nasdaq_movers  # noqa: E402
from marketbot.universe import coin_base as coin_ticker  # noqa: E402
from marketbot.directory import BUNDLED, FILENAME, Directory, Listing, builtins, read, refresh, write  # noqa: E402
from marketbot.engine import Engine, Resolved, SourcesDown, UnknownSymbol, _names_match  # noqa: E402
from marketbot.hours import NEW_YORK  # noqa: E402
from marketbot.http import Http, HttpError, Response  # noqa: E402
from marketbot.sources import Coin, Sources  # noqa: E402
from marketbot.universe import CRYPTO, STOCKS  # noqa: E402
from marketbot.yahoo import Bars, YahooError  # noqa: E402
from tests.market_helpers import from_closes  # noqa: E402

NOW = int(time.time())  # the backups work out their date ranges from the real clock
TODAY = NOW // DAY * DAY


# ----- a fake web: Yahoo, Nasdaq and Coinbase answering from canned data -----

def _json(obj, status=200) -> Response:
    return Response(status, json.dumps(obj))


def _one(params: dict, key: str, default=None):
    v = params.get(key)
    return v[0] if v else default


def _mask(b: Bars, keep) -> Bars:
    return Bars(b.symbol, b.t[keep], b.open[keep], b.high[keep], b.low[keep], b.close[keep], b.volume[keep],
                dict(b.meta))


def daily_bars(n=300, symbol="TEST", end_day=TODAY, first=100.0, step=0.25, at=0, source=None) -> Bars:
    """n daily bars ending on end_day (bars stamped `at` seconds into each UTC day), closes rising by `step`."""
    t = end_day - (n - 1 - np.arange(n, dtype=np.int64)) * DAY + at
    c = first + step * np.arange(n, dtype=float)
    meta = {"source": source} if source else {}
    return Bars(symbol, t, c - 0.1, c + 0.5, c - 0.5, c.copy(), np.full(n, 1000.0), meta)


def minute_bars(start, end, step=300, price=100.0, symbol="TEST") -> Bars:
    t = np.arange(start, end, step, dtype=np.int64)
    c = np.full(len(t), price)
    return Bars(symbol, t, c.copy(), c * 1.001, c * 0.999, c.copy(), np.ones(len(t)))


def y_quote(symbol, price, prev, name=None, quote_type="EQUITY"):
    return {"symbol": symbol, "shortName": name or symbol, "regularMarketPrice": price,
            "regularMarketPreviousClose": prev, "regularMarketChangePercent": (price / prev - 1) * 100,
            "marketState": "REGULAR", "quoteType": quote_type, "regularMarketTime": NOW}


def n_row(symbol, name, price, prev, asset="STOCKS", status="Market Open"):
    return {"symbol": symbol, "companyName": name, "lastSalePrice": f"${price:,.2f}",
            "previousClosePrice": f"${prev:,.2f}", "percentageChange": f"{(price / prev - 1) * 100:+.2f}%",
            "marketStatus": status, "assetClass": asset, "volume": "1,234,567",
            "lastTradeTimestampDateTime": "2026-10-06T12:47:45.0512345-04:00"}


def cb_stats(last, open_, volume=1000.0):
    return {"open": str(open_), "high": str(max(last, open_) * 1.01), "low": str(min(last, open_) * 0.99),
            "last": str(last), "volume": str(volume)}


def coin(ticker, name, price, change=5.0, cap=1e9):
    return Coin(symbol=ticker.lower(), name=name, price=price, cap=cap, rank=1, change_1h=None, change_24h=change,
                change_7d=None, ath_change=None, volume=1e8)


def _chart_result(symbol: str, b: Bars) -> dict:
    meta = {"symbol": symbol, "currency": "USD", "instrumentType": "EQUITY",
            "regularMarketPrice": float(b.close[-1]) if len(b) else None}
    return {"meta": meta, "timestamp": b.t.tolist(),
            "indicators": {"quote": [{"open": b.open.tolist(), "high": b.high.tolist(), "low": b.low.tolist(),
                                      "close": b.close.tolist(), "volume": b.volume.tolist()}],
                           "adjclose": [{"adjclose": b.close.tolist()}]}}


NOT_FOUND = '{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found"}}}'


class FakeNet:
    """An Http backend. Requests are routed by host (yahoo / nasdaq / coinbase) and path to the canned data below;
    a source in `down` can't be reached and a source in `blocked` answers every request with that HTTP status."""
    name = "fake"

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.down: set[str] = set()
        self.blocked: dict[str, int] = {}
        self.crumb = "Cr4mb"
        self.y_quotes: dict[str, dict] = {}
        self.y_charts: dict[str, Bars] = {}
        self.y_intraday: dict[str, Bars] = {}
        self.y_search: dict[str, list] = {}
        self.y_screens: dict[str, list] = {}
        self.n_quotes: dict[str, tuple[str, dict]] = {}  # Nasdaq symbol -> (asset class, watchlist row)
        self.n_history: dict[str, tuple[str, Bars]] = {}  # Nasdaq symbol -> (asset class, bars)
        self.n_chart: dict[str, dict] = {}  # Nasdaq symbol -> chart data
        self.n_screener: list = []
        self.cb_stats: dict[str, dict] = {}  # Coinbase product -> 24h stats
        self.cb_candles: dict[str, Bars] = {}  # Coinbase product -> candles
        self.overrides: list[tuple[str, str, Response]] = []  # (source, path part, canned answer), checked first
        self.closed = False

    def count(self, source=None, path="") -> int:
        return sum(1 for s, p, _ in self.calls if (source is None or s == source) and path in p)

    def params(self, source, path):
        return [q for s, p, q in self.calls if s == source and path in p]

    async def close(self):
        self.closed = True

    async def get(self, url, headers, timeout, proxy=None):
        parts = urlsplit(url)
        host = parts.hostname or ""
        source = ("yahoo" if host.endswith("yahoo.com") else "nasdaq" if host.endswith("nasdaq.com")
                  else "coinbase" if host.endswith("coinbase.com") else host)
        params = parse_qs(parts.query)
        self.calls.append((source, unquote(parts.path), params))
        await asyncio.sleep(0)  # let concurrent requests interleave like real ones
        if source in self.down:
            raise ConnectionError(f"{host} unreachable")
        if source in self.blocked:
            return Response(self.blocked[source], "Forbidden", url)
        for src, part, answer in self.overrides:
            if src == source and part in unquote(parts.path):
                return answer
        handler = getattr(self, f"_{source}", None)
        if handler is None:
            raise ConnectionError(f"unexpected host {host}")
        return handler(unquote(parts.path), params)

    # Yahoo
    def _yahoo(self, path, params):
        if path in ("", "/"):
            return Response(404, "<html>fc.yahoo.com</html>")
        if path == "/v1/test/getcrumb":
            return Response(200, self.crumb)
        if path == "/v7/finance/quote":
            if _one(params, "crumb") != self.crumb:
                return Response(401, '{"finance":{"error":{"code":"Unauthorized"}}}')
            syms = _one(params, "symbols", "").split(",")
            return _json({"quoteResponse": {"result": [self.y_quotes[s] for s in syms if s in self.y_quotes]}})
        if path == "/v7/finance/spark":
            syms = _one(params, "symbols", "").split(",")
            result = [{"symbol": s, "response": [{"meta": {
                "symbol": s, "regularMarketPrice": self.y_quotes[s]["regularMarketPrice"],
                "previousClose": self.y_quotes[s]["regularMarketPreviousClose"]}}]} for s in syms if s in self.y_quotes]
            return _json({"spark": {"result": result}}) if result else Response(404, '{"spark":{"result":null}}')
        if path.startswith("/v8/finance/chart/"):
            sym = path.rsplit("/", 1)[1]
            if "range" in params:
                b = self.y_intraday.get(sym)
            else:
                b = self.y_charts.get(sym)
                if b is not None:
                    b = _mask(b, b.t >= int(_one(params, "period1")))
            if b is None:
                return Response(404, NOT_FOUND)
            return _json({"chart": {"result": [_chart_result(sym, b)], "error": None}})
        if path == "/v1/finance/search":
            return _json({"quotes": self.y_search.get(_one(params, "q", "").lower(), []), "news": []})
        if path == "/v1/finance/screener/predefined/saved":
            return _json({"finance": {"result": [{"quotes": self.y_screens.get(_one(params, "scrIds"), [])}]}})
        return Response(404, "{}")

    # Nasdaq
    def _nasdaq(self, path, params):
        if path == "/api/quote/watchlist":
            rows = []
            for item in params.get("symbol", []):
                ns, cls = item.split("|")
                known = self.n_quotes.get(ns.upper())
                if known and known[0] == cls:
                    rows.append(known[1])
            return _json({"data": rows, "message": None})
        m = re.fullmatch(r"/api/quote/([^/]+)/historical", path)
        if m:
            known = self.n_history.get(m.group(1))
            if not known or known[0] != _one(params, "assetclass"):
                return _json({"data": None, "message": "Symbol not exists"})
            since = datetime.strptime(_one(params, "fromdate"), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            b = known[1]
            rows = [{"date": datetime.fromtimestamp(int(t), timezone.utc).strftime("%m/%d/%Y"),
                     "close": f"${c:,.2f}", "volume": f"{v:,.0f}", "open": f"${o:,.2f}", "high": f"${h:,.2f}",
                     "low": f"${lo:,.2f}"}
                    for t, o, h, lo, c, v in zip(b.t, b.open, b.high, b.low, b.close, b.volume) if t >= since]
            return _json({"data": {"symbol": m.group(1), "tradesTable": {"rows": rows[::-1]}}})
        m = re.fullmatch(r"/api/quote/([^/]+)/chart", path)
        if m:
            return _json({"data": self.n_chart.get(m.group(1))})
        if path == "/api/screener/stocks":
            return _json({"data": {"rows": self.n_screener}})
        return Response(404, "{}")

    # Coinbase
    def _coinbase(self, path, params):
        m = re.fullmatch(r"/products/([^/]+)/(stats|candles)", path)
        if not m:
            return Response(404, '{"message":"NotFound"}')
        product, what = m.groups()
        if what == "stats":
            s = self.cb_stats.get(product)
            return _json(s) if s else Response(404, '{"message":"NotFound"}')
        b = self.cb_candles.get(product)
        if b is None:
            return Response(404, '{"message":"NotFound"}')
        start = datetime.fromisoformat(_one(params, "start")).timestamp()
        end = datetime.fromisoformat(_one(params, "end")).timestamp()
        keep = (b.t >= start) & (b.t <= end)
        rows = [[int(t), lo, h, o, c, v] for t, o, h, lo, c, v in
                zip(b.t[keep], b.open[keep], b.high[keep], b.low[keep], b.close[keep], b.volume[keep])]
        return _json(rows[::-1])  # newest first


async def _no_sleep(_seconds):
    return None


LISTINGS = [
    Listing("AAPL", "Apple Inc.", STOCKS, "stock", 3.5e12, ("sp500", "ndx100"), "Technology"),
    Listing("NVDA", "NVIDIA Corporation", STOCKS, "stock", 4e12, ("sp500", "ndx100"), "Technology"),
    Listing("AMD", "Advanced Micro Devices, Inc.", STOCKS, "stock", 3e11, ("sp500", "ndx100")),
    Listing("JPM", "JP Morgan Chase & Co.", STOCKS, "stock", 8e11, ("sp500",)),
    Listing("BRK-B", "Berkshire Hathaway Inc.", STOCKS, "stock", 1e12, ("sp500",)),
    Listing("SPY", "SPDR S&P 500 ETF Trust", STOCKS, "etf"),
    Listing("BTC", "Grayscale Bitcoin Mini Trust", STOCKS, "etf"),
    Listing("BTC-USD", "Bitcoin", CRYPTO, "crypto", 1.7e12),
    Listing("ETH-USD", "Ethereum", CRYPTO, "crypto", 4e11),
    Listing("HYPE32196-USD", "Hyperliquid", CRYPTO, "crypto", 2.3e10),
]


def small_directory(extra=()) -> Directory:
    return Directory(LISTINGS + list(extra), updated=time.time())


@functools.lru_cache(maxsize=1)
def bundled() -> Directory:
    return Directory.load()


def make_data(net: FakeNet, directory: Directory | None = None, coins=None) -> MarketData:
    http = Http(backend=net, retries=0, sleep=_no_sleep)
    return MarketData(http=http, directory=directory if directory is not None else small_directory(), coins=coins)


def rest(data: MarketData, source="Yahoo", status=403):
    for _ in range(3):
        data.http.record_failure(source, HttpError(source, f"HTTP {status}", status))
    assert data.http.resting(source)


def run(coro):
    return asyncio.run(coro)


# ----- MarketData: quotes -----

def test_quotes_come_from_yahoo_when_it_answers():
    net = FakeNet()
    net.y_quotes = {"AAPL": y_quote("AAPL", 250.0, 245.0, "Apple Yahoo name"),
                    "BTC-USD": y_quote("BTC-USD", 61000.0, 60000.0, "Bitcoin USD", "CRYPTOCURRENCY")}

    async def go():
        data = make_data(net)
        out = await data.quotes(["AAPL", "BTC-USD", "AAPL", ""])
        await data.close()
        return out, data
    out, data = run(go())
    assert set(out) == {"AAPL", "BTC-USD"}
    assert out["AAPL"].source == "Yahoo" and out["AAPL"].name == "Apple Yahoo name"  # Yahoo's names are kept
    assert out["BTC-USD"].price == 61000.0 and out["BTC-USD"].quote_type == "CRYPTOCURRENCY"
    assert net.count("nasdaq") == 0 and net.count("coinbase") == 0
    assert net.params("yahoo", "/v7/finance/quote")[0]["symbols"] == ["AAPL,BTC-USD"]  # deduplicated, no blanks
    assert net.closed and data.yahoo_ok and data.health["Yahoo"].ok > 0


def test_quotes_nothing_asked_means_no_requests():
    net = FakeNet()

    async def go():
        data = make_data(net)
        return await data.quotes([]), await data.quotes(["", ""])
    assert run(go()) == ({}, {})
    assert net.calls == []


def test_quotes_yahoo_partial_answer_is_completed_by_the_backups():
    net = FakeNet()
    net.y_quotes = {"AAPL": y_quote("AAPL", 250.0, 245.0)}
    net.n_quotes = {"NVDA": ("stocks", n_row("NVDA", "NVIDIA Corporation Common Stock", 180.0, 175.0))}

    async def go():
        return await make_data(net).quotes(["AAPL", "NVDA"])
    out = run(go())
    assert out["AAPL"].source == "Yahoo" and out["NVDA"].source == "Nasdaq"
    assert out["NVDA"].price == 180.0 and out["NVDA"].change_pct == pytest.approx((180 / 175 - 1) * 100)
    sent = [s for q in net.params("nasdaq", "/watchlist") for s in q["symbol"]]
    assert sent == ["nvda|stocks"]  # only what Yahoo didn't have


def test_quotes_fall_back_to_nasdaq_and_coinbase_when_yahoo_is_down():
    net = FakeNet()
    net.down = {"yahoo"}
    net.n_quotes = {"AAPL": ("stocks", n_row("AAPL", "Apple Inc. Common Stock", 250.0, 245.0)),
                    "SPY": ("etf", n_row("SPY", "SPDR S&P 500 ETF Trust", 660.0, 650.0, asset="ETF")),
                    "BRK.B": ("stocks", n_row("BRK.B", "Berkshire Hathaway Inc. Class B Common Stock", 480.0, 470.0)),
                    "COMP": ("index", n_row("COMP", "NASDAQ Composite Index", 22000.0, 21800.0, asset="INDEX"))}
    net.cb_stats = {"BTC-USD": cb_stats(61000, 60000), "HYPE-USD": cb_stats(40.0, 38.0, volume=10.0)}

    async def go():
        data = make_data(net)
        out = await data.quotes(["AAPL", "SPY", "BRK-B", "^IXIC", "^GSPC", "BTC-USD", "HYPE32196-USD"])
        return out, data
    out, data = run(go())
    assert set(out) == {"AAPL", "SPY", "BRK-B", "^IXIC", "BTC-USD", "HYPE32196-USD"}  # ^GSPC: only Yahoo has it
    assert {s: q.source for s, q in out.items()} == {"AAPL": "Nasdaq", "SPY": "Nasdaq", "BRK-B": "Nasdaq",
                                                     "^IXIC": "Nasdaq", "BTC-USD": "Coinbase",
                                                     "HYPE32196-USD": "Coinbase"}
    # The directory's names replace the backups' terse or formal ones.
    assert out["AAPL"].name == "Apple Inc." and out["HYPE32196-USD"].name == "Hyperliquid"
    assert out["BRK-B"].name == "Berkshire Hathaway Inc." and out["^IXIC"].name == "Nasdaq"
    assert out["SPY"].quote_type == "ETF" and out["^IXIC"].quote_type == "INDEX"
    hype = out["HYPE32196-USD"]
    assert hype.price == 40.0 and hype.prev_close == 38.0 and hype.change_pct == pytest.approx((40 / 38 - 1) * 100)
    assert hype.volume == pytest.approx(400.0) and hype.extra["change_window"] == "24h"
    sent = {s for q in net.params("nasdaq", "/watchlist") for s in q["symbol"]}
    assert {"aapl|stocks", "spy|etf", "brk.b|stocks", "comp|index"} <= sent  # ETFs asked as ETFs
    assert not any("gspc" in s for s in sent)
    assert net.count("coinbase", "/products/HYPE-USD/stats") == 1
    assert data.outage() is None  # Yahoo is failing but the backups answer


def test_quotes_nasdaq_retries_the_other_asset_class():
    net = FakeNet()
    net.down = {"yahoo"}
    # The directory says SPY is an ETF but Nasdaq files it as a stock; XYZ isn't in the directory and is an ETF.
    net.n_quotes = {"SPY": ("stocks", n_row("SPY", "SPDR", 660.0, 650.0)),
                    "XYZ": ("etf", n_row("XYZ", "Xyz Fund", 20.0, 19.0, asset="ETF"))}

    async def go():
        return await make_data(net).quotes(["SPY", "XYZ", "NOPE"])
    out = run(go())
    assert set(out) == {"SPY", "XYZ"}
    batches = [q["symbol"] for q in net.params("nasdaq", "/watchlist")]
    assert sorted(batches[0]) == ["nope|stocks", "spy|etf", "xyz|stocks"]
    assert sorted(batches[1]) == ["nope|etf", "spy|stocks", "xyz|etf"]
    assert out["XYZ"].name == "Xyz Fund"  # not in the directory: Nasdaq's name


def test_quotes_crypto_falls_back_to_coingecko_after_coinbase():
    net = FakeNet()
    net.down = {"yahoo"}
    calls = []

    async def coins():
        calls.append(1)
        return [coin("HYPE", "Hyperliquid", 40.0, change=25.0), coin("hype", "Some other hype", 0.01),
                coin("ETH", "Ethereum", 4000.0, change=None)]

    async def go():
        return await make_data(net, coins=coins).quotes(["HYPE32196-USD", "ETH-USD", "BTC-USD"])
    out = run(go())
    assert calls == [1]  # one CoinGecko call for every coin Coinbase couldn't price
    assert set(out) == {"HYPE32196-USD", "ETH-USD"}  # CoinGecko's list has no BTC here
    h = out["HYPE32196-USD"]
    assert h.source == "CoinGecko" and h.price == 40.0  # the biggest coin with the ticker wins
    assert h.prev_close == pytest.approx(32.0) and h.change_pct == 25.0 and h.name == "Hyperliquid"
    assert h.quote_type == "CRYPTOCURRENCY" and h.extra == {"change_window": "24h"}
    e = out["ETH-USD"]
    assert e.prev_close is None and e.change_pct is None and e.change is None
    assert net.count("coinbase", "/products/HYPE-USD/stats") == 1


def test_coingecko_is_not_asked_when_coinbase_prices_everything():
    net = FakeNet()
    net.down = {"yahoo"}
    net.cb_stats = {"BTC-USD": cb_stats(61000, 60000)}

    async def coins():
        raise AssertionError("CoinGecko shouldn't be needed")

    async def go():
        return await make_data(net, coins=coins).quotes(["BTC-USD"])
    out = run(go())
    assert out["BTC-USD"].source == "Coinbase" and out["BTC-USD"].name == "Bitcoin"


def test_coingecko_failure_and_odd_numbers_leave_coins_out_quietly():
    net = FakeNet()
    net.down = {"yahoo", "coinbase"}

    async def broken():
        raise RuntimeError("CoinGecko 429")

    async def odd():
        return [coin("BTC", "Bitcoin", 0.0), coin("ETH", "Ethereum", 4000.0, change=-100.0)]

    async def go():
        return (await make_data(net, coins=broken).quotes(["BTC-USD"]),
                await make_data(net, coins=odd).quotes(["BTC-USD", "ETH-USD"]))
    failed, partial = run(go())
    assert failed == {}
    assert set(partial) == {"ETH-USD"}  # a zero price isn't a price
    assert partial["ETH-USD"].prev_close is None and partial["ETH-USD"].change_pct == -100.0


def test_quotes_skip_a_resting_yahoo_entirely():
    net = FakeNet()
    net.y_quotes = {"AAPL": y_quote("AAPL", 999.0, 998.0)}
    net.n_quotes = {"AAPL": ("stocks", n_row("AAPL", "Apple Inc. Common Stock", 250.0, 245.0))}

    async def go():
        data = make_data(net)
        rest(data)
        return await data.quotes(["AAPL"]), data
    out, data = run(go())
    assert out["AAPL"].source == "Nasdaq" and out["AAPL"].price == 250.0
    assert net.count("yahoo") == 0 and not data.yahoo_ok


def test_quotes_with_every_source_down_is_empty_not_an_error():
    net = FakeNet()
    net.down = {"yahoo", "nasdaq", "coinbase"}

    async def go():
        data = make_data(net)
        return await data.quotes(["AAPL", "BTC-USD", "^GSPC"]), data
    out, data = run(go())
    assert out == {}
    assert data.outage() and data.outage().startswith("Yahoo Finance: ")


def test_quotes_nasdaq_blocked_still_gets_crypto():
    net = FakeNet()
    net.down = {"yahoo"}
    net.blocked = {"nasdaq": 403}
    net.cb_stats = {"ETH-USD": cb_stats(4000, 3900)}

    async def go():
        data = make_data(net)
        return await data.quotes(["AAPL", "ETH-USD"]), data
    out, data = run(go())
    assert set(out) == {"ETH-USD"}
    assert data.health["Nasdaq"].status == 403 and data.health["Nasdaq"].failing


def test_concurrent_quotes_share_one_yahoo_crumb():
    net = FakeNet()
    net.y_quotes = {s: y_quote(s, 10.0 + i, 10.0) for i, s in enumerate(["AAPL", "NVDA", "AMD"])}

    async def go():
        data = make_data(net)
        return await asyncio.gather(*(data.quotes([s]) for s in ["AAPL", "NVDA", "AMD"] * 4))
    results = run(go())
    assert all(len(r) == 1 for r in results)
    assert net.count("yahoo", "/v1/test/getcrumb") == 1


@pytest.mark.parametrize("status", [403, 429])
def test_quotes_when_yahoo_blocks_the_host(status):
    net = FakeNet()
    net.blocked = {"yahoo": status}  # what Yahoo does to Python clients on cloud hosts
    net.y_quotes = {"AAPL": y_quote("AAPL", 999.0, 998.0)}
    net.n_quotes = {"AAPL": ("stocks", n_row("AAPL", "Apple Inc. Common Stock", 250.0, 245.0))}

    async def go():
        data = make_data(net)
        first = await data.quotes(["AAPL"])
        yahoo_calls = net.count("yahoo")
        second = await data.quotes(["AAPL"])
        return first, second, yahoo_calls, data
    first, second, yahoo_calls, data = run(go())
    assert first["AAPL"].source == second["AAPL"].source == "Nasdaq"
    assert 1 <= yahoo_calls <= 3 and net.count("yahoo") == yahoo_calls  # rested after failing: not asked again
    assert not data.yahoo_ok and data.health["Yahoo"].status == status
    assert data.outage() is None  # Nasdaq answers
    assert data.health["Yahoo"].line().startswith("⚠️ failing")


def test_quotes_survive_a_rejected_crumb():
    net = FakeNet()
    net.y_quotes = {"AAPL": y_quote("AAPL", 250.0, 245.0)}
    net.overrides = [("yahoo", "/v7/finance/quote", Response(401, '{"finance":{"error":"Invalid Crumb"}}'))]

    async def go():
        return await make_data(net).quotes(["AAPL"])
    out = run(go())
    assert out["AAPL"].price == 250.0 and out["AAPL"].source == "Yahoo"  # from Yahoo's chart summary instead
    assert net.count("yahoo", "/v1/test/getcrumb") == 2  # refreshed once, then gave up on the quote endpoint
    assert net.count("yahoo", "/v7/finance/spark") == 1 and net.count("nasdaq") == 0


def test_daily_yahoo_html_page_falls_back():
    net = FakeNet()
    net.y_charts["AAPL"] = daily_bars(10, "AAPL")
    net.n_history = {"AAPL": ("stocks", daily_bars(10, "AAPL"))}
    net.overrides = [("yahoo", "/v8/finance/chart", Response(200, "<html>Will be right back...</html>"))]

    async def go():
        return await make_data(net).daily("AAPL")
    bars = run(go())
    assert source_of(bars) == "Nasdaq" and len(bars) == 10


def test_daily_nasdaq_garbage_is_unavailable_not_a_crash():
    net = FakeNet()
    net.down = {"yahoo"}
    net.overrides = [("nasdaq", "/historical", Response(200, "<html>Access Denied</html>"))]

    async def go():
        with pytest.raises(DataUnavailable) as e:
            await make_data(net).daily("AAPL")
        return e.value
    exc = run(go())
    assert "not JSON" in str(exc)


@pytest.mark.xfail(strict=True, reason="BUG: coin_ticker strips digits that belong to the ticker, so CoinGecko's "
                                       "'M' coin prices M87-USD")
def test_coingecko_fallback_matches_tickers_that_end_in_digits():
    net = FakeNet()
    net.down = {"yahoo", "coinbase"}
    directory = small_directory([Listing("M87-USD", "MESSIER", CRYPTO, "crypto", 1.9e7),
                                 Listing("M35491-USD", "MemeCore", CRYPTO, "crypto", 2.4e9)])

    async def coins():  # largest first, as CoinGecko lists them
        return [coin("M", "MemeCore", 2.40, cap=2.4e9), coin("M87", "MESSIER", 0.00002, cap=1.9e7)]

    async def go():
        return await make_data(net, directory, coins=coins).quotes(["M87-USD"])
    out = run(go())
    assert out["M87-USD"].price == 0.00002  # not MemeCore's $2.40


def test_coin_ticker():
    assert coin_ticker("HYPE32196-USD") == "HYPE" and coin_ticker("BTC-USD") == "BTC"
    assert coin_ticker("SUI20947-USD") == "SUI" and coin_ticker("AAPL") == "AAPL" and coin_ticker("^GSPC") == "^GSPC"


# ----- MarketData: daily and intraday history -----

def test_daily_comes_from_yahoo():
    net = FakeNet()
    net.y_charts["AAPL"] = daily_bars(400, "AAPL", at=13 * 3600 + 1800)

    async def go():
        data = make_data(net)
        return await data.daily("AAPL"), await data.daily("AAPL", start=TODAY - 9 * DAY)
    full, recent = run(go())
    assert len(full) == 400 and source_of(full) == "Yahoo" and full.symbol == "AAPL"
    assert len(recent) == 10 and recent.t[0] >= TODAY - 9 * DAY
    assert net.count("nasdaq") == 0


def test_daily_falls_back_to_nasdaq_for_stocks_and_etfs():
    net = FakeNet()
    net.down = {"yahoo"}
    net.n_history = {"AAPL": ("stocks", daily_bars(200, "AAPL")), "SPY": ("etf", daily_bars(50, "SPY", first=600)),
                     "BRK.B": ("stocks", daily_bars(30, "BRK-B", first=480))}

    async def go():
        data = make_data(net)
        return (await data.daily("AAPL"), await data.daily("SPY", start=TODAY - 20 * DAY),
                await data.daily("BRK-B"))
    aapl, spy, brk = run(go())
    assert len(aapl) == 200 and source_of(aapl) == "Nasdaq" and aapl.symbol == "AAPL"
    assert np.allclose(aapl.close, daily_bars(200).close)
    assert np.all(np.diff(aapl.t) > 0)  # oldest first though Nasdaq sends newest first
    assert np.all(aapl.t % DAY == 14 * 3600 + 1800)  # stamped at the New York open, roughly
    assert len(spy) == 21 and spy.close[0] == pytest.approx(600 + 0.25 * 29)
    assert len(brk) == 30
    spy_calls = net.params("nasdaq", "/api/quote/SPY/historical")
    assert spy_calls[0]["assetclass"] == ["etf"]
    assert spy_calls[0]["fromdate"] == [datetime.fromtimestamp(TODAY - 20 * DAY, timezone.utc).strftime("%Y-%m-%d")]


def test_daily_nasdaq_tries_the_other_asset_class():
    net = FakeNet()
    net.down = {"yahoo"}
    net.n_history = {"ARKK": ("etf", daily_bars(20, "ARKK"))}  # not in the directory, so asked as a stock first

    async def go():
        return await make_data(net).daily("ARKK")
    bars = run(go())
    assert len(bars) == 20
    assert [q["assetclass"] for q in net.params("nasdaq", "/historical")] == [["stocks"], ["etf"]]


def test_daily_crypto_falls_back_to_coinbase():
    net = FakeNet()
    net.down = {"yahoo"}
    net.cb_candles = {"HYPE-USD": daily_bars(400, "HYPE-USD", first=10.0, step=0.1)}

    async def go():
        return await make_data(net).daily("HYPE32196-USD")
    bars = run(go())
    assert len(bars) == 400 and source_of(bars) == "Coinbase" and bars.symbol == "HYPE32196-USD"
    assert bars.close[-1] == pytest.approx(10.0 + 0.1 * 399)
    pages = net.count("coinbase", "/candles")
    assert 2 <= pages <= 3  # 300 days per call, and it stops at the coin's listing


def test_daily_unknown_everywhere_is_unknown():
    net = FakeNet()

    async def go():
        with pytest.raises(DataUnavailable) as e:
            await make_data(net).daily("ZZZZ")
        return e.value
    exc = run(go())
    assert isinstance(exc, YahooError)  # existing handlers keep working
    assert exc.unknown and exc.status == 404 and str(exc).startswith("No price history for ZZZZ: ")
    assert "Nasdaq has no history for ZZZZ" in str(exc)


def test_daily_index_with_yahoo_down_has_no_backup():
    net = FakeNet()
    net.down = {"yahoo"}

    async def go():
        with pytest.raises(DataUnavailable) as e:
            await make_data(net).daily("^GSPC")
        return e.value
    exc = run(go())
    assert not exc.unknown and exc.status is None and "ConnectionError" in str(exc)
    assert net.count("nasdaq") == 0 and net.count("coinbase") == 0


def test_daily_while_yahoo_rests_says_so():
    net = FakeNet()
    net.n_history = {"AAPL": ("stocks", daily_bars(5, "AAPL"))}

    async def go():
        data = make_data(net)
        rest(data)
        with pytest.raises(DataUnavailable) as e:
            await data.daily("^GSPC")
        return e.value, await data.daily("AAPL")
    exc, aapl = run(go())
    assert "resting" in str(exc) and not exc.unknown and exc.status is None
    assert len(aapl) == 5 and net.count("yahoo") == 0


def test_daily_blocked_backup_is_not_unknown():
    net = FakeNet()
    net.blocked = {"nasdaq": 403}

    async def go():
        with pytest.raises(DataUnavailable) as e:
            await make_data(net).daily("ZZZZ")
        return e.value
    exc = run(go())
    assert exc.status == 404 and not exc.unknown  # Yahoo said 404 but Nasdaq didn't answer
    assert "HTTP 403" in str(exc)


def test_daily_backup_with_only_bad_rows_is_unavailable():
    net = FakeNet()
    net.down = {"yahoo"}
    bad = daily_bars(5, "AAPL")
    bad.close[:] = np.nan  # Nasdaq sends "N/A" closes
    net.n_history = {"AAPL": ("stocks", bad)}

    async def go():
        with pytest.raises(DataUnavailable):
            await make_data(net).daily("AAPL")
    run(go())


def test_intraday_from_yahoo_then_backups():
    net = FakeNet()
    five = 300
    net.y_intraday["AAPL"] = minute_bars(NOW - 78 * five, NOW, five, symbol="AAPL")
    net.cb_candles = {"BTC-USD": minute_bars(NOW - 6 * DAY, NOW, five, symbol="BTC-USD")}
    day = datetime.now(NEW_YORK).date()
    wall = [datetime(day.year, day.month, day.day, 9, 30 + i, tzinfo=timezone.utc) for i in range(5)]
    net.n_chart = {"NVDA": {"chart": [{"x": int(w.timestamp() * 1000), "y": f"{180 + i}"} for i, w in enumerate(wall)]
                            + [{"x": None, "y": "1"}, {"x": 1, "y": "--"}], "previousClose": "$179.00"}}

    async def go():
        data = make_data(net)
        y = await data.intraday("AAPL")
        net.down = {"yahoo"}
        btc1 = await data.intraday("BTC-USD", "1d")
        btc5 = await data.intraday("BTC-USD", "5d", "15m")
        nvda = await data.intraday("NVDA")
        return y, btc1, btc5, nvda
    y, btc1, btc5, nvda = run(go())
    assert len(y) == 78
    granularity = [q["granularity"][0] for q in net.params("coinbase", "/candles")]
    assert granularity[0] == "300" and granularity[-1] == "900"
    assert 280 <= len(btc1) <= 290 and source_of(btc1) == "Coinbase"
    assert len(nvda) == 5 and nvda.close[0] == 180.0
    open_ny = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK).timestamp()
    assert nvda.t[0] == open_ny  # Nasdaq's x is New York wall-clock time written as UTC


def test_intraday_unavailable_messages():
    net = FakeNet()
    net.down = {"yahoo"}
    net.n_chart = {"AAPL": {"chart": [], "previousClose": "$1"}}

    async def go():
        data = make_data(net)
        out = []
        for sym in ("AAPL", "^GSPC", "ETH-USD"):
            with pytest.raises(DataUnavailable) as e:
                await data.intraday(sym)
            out.append(e.value)
        return out
    aapl, gspc, eth = run(go())
    assert str(aapl).startswith("No intraday prices for AAPL: ") and "ConnectionError" in str(aapl)
    assert "ConnectionError" in str(gspc) and not gspc.unknown
    assert "Coinbase doesn't list ETH-USD" in str(eth) and eth.status == 404 and not eth.unknown


@pytest.mark.xfail(strict=True, reason="BUG: intraday() drops the 'Yahoo is resting' reason that daily() reports, "
                                       "saying 'no source has it'")
def test_intraday_while_yahoo_rests_says_so():
    net = FakeNet()

    async def go():
        data = make_data(net)
        rest(data)
        with pytest.raises(DataUnavailable) as e:
            await data.intraday("^GSPC")
        return e.value
    exc = run(go())
    assert "resting" in str(exc) and "no source has it" not in str(exc)


# ----- MarketData: search and screener -----

def test_search_uses_yahoo_then_the_directory():
    net = FakeNet()
    net.y_search = {"apple": [{"symbol": "AAPL", "quoteType": "EQUITY", "shortname": "Apple Inc."}]}

    async def go():
        data = make_data(net)
        first = await data.search("apple", news=3)
        net.down = {"yahoo"}
        second = await data.search("apple")
        crypto = await data.search("hyperliquid", quotes=3)
        index = await data.search("s&p")
        none = await data.search("apple", quotes=0)
        return first, second, crypto, index, none
    first, second, crypto, index, none = run(go())
    assert first == ([{"symbol": "AAPL", "quoteType": "EQUITY", "shortname": "Apple Inc."}], [])
    found, news = second
    assert news == [] and found[0] == {"symbol": "AAPL", "shortname": "Apple Inc.", "longname": "Apple Inc.",
                                       "quoteType": "EQUITY", "exchDisp": "STOCK"}
    assert crypto[0][0]["symbol"] == "HYPE32196-USD" and crypto[0][0]["quoteType"] == "CRYPTOCURRENCY"
    assert crypto[0][0]["exchDisp"] == "Crypto" and len(crypto[0]) <= 3
    assert index[0][0]["symbol"] == "^GSPC" and index[0][0]["quoteType"] == "INDEX"
    assert none == ([], [])


def test_search_skips_a_resting_yahoo():
    net = FakeNet()

    async def go():
        data = make_data(net)
        rest(data)
        return await data.search("spdr")
    found, _ = run(go())
    assert found[0]["symbol"] == "SPY" and found[0]["quoteType"] == "ETF" and net.count("yahoo") == 0


def screener_row(symbol, cap, price, pct, volume, name=None):
    return {"symbol": symbol, "name": name or f"{symbol} Inc. Common Stock", "marketCap": cap, "lastsale": price,
            "pctchange": pct, "volume": volume}


SCREENER = [
    screener_row("BIG", "3,000,000,000", "$10.00", "5.5%", "1,000"),
    screener_row("HUGE", "900,000,000,000", "$500.00", "1.2%", "90,000,000"),
    screener_row("DOWN", "50,000,000,000", "$40.00", "-3.1%", "5,000,000"),
    screener_row("FLAT", "20,000,000,000", "$30.00", "0.00%", "70,000,000"),
    screener_row("EDGE", f"{MIN_CAP:.0f}", "$5.00", "9.9%", "10"),  # exactly at the floor: kept
    screener_row("TINY", "1,999,999,999", "$1.00", "80%", "999,999,999"),  # below the floor
    screener_row("NOCAP", "", "$1.00", "50%", "1"),
    screener_row("NA", "N/A", "$1.00", "40%", "1"),
    screener_row("ZERO", "5,000,000,000", "$0.00", "30%", "1"),
    screener_row("NOPCT", "5,000,000,000", "$3.00", "--", "1"),
    screener_row("ABR^D", "5,000,000,000", "$20.00", "20%", "1"),  # a preferred share
    screener_row("BRK/B", "1,000,000,000,000", "$480.00", "-0.5%", "4,000,000",
                 "Berkshire Hathaway Inc. Class B Common Stock"),
    {"symbol": None, "marketCap": "5,000,000,000", "lastsale": "$1", "pctchange": "1%"},
    {},
]


def test_nasdaq_movers_floor_sorting_signs_and_bad_rows():
    gainers = nasdaq_movers(SCREENER, "day_gainers", 25)
    assert [q["symbol"] for q in gainers] == ["EDGE", "BIG", "HUGE"]  # positive only, biggest first
    losers = nasdaq_movers(SCREENER, "day_losers", 25)
    assert [q["symbol"] for q in losers] == ["DOWN", "BRK-B"]
    active = nasdaq_movers(SCREENER, "most_actives", 25)
    assert [q["symbol"] for q in active] == ["HUGE", "FLAT", "DOWN", "BRK-B", "BIG", "EDGE"]  # flat ones count too
    assert nasdaq_movers(SCREENER, "most_actives", 2) == active[:2]
    brk = next(q for q in losers if q["symbol"] == "BRK-B")
    assert brk["shortName"] == "Berkshire Hathaway Inc. Class B" and brk["regularMarketPrice"] == 480.0
    assert brk["marketCap"] == 1e12 and brk["source"] == "Nasdaq (last close)"
    assert brk["regularMarketChangePercent"] == -0.5 and brk["regularMarketVolume"] == 4e6
    assert nasdaq_movers([], "day_gainers", 5) == []
    with pytest.raises(KeyError):
        nasdaq_movers(SCREENER, "nonsense", 5)


def test_screener_falls_back_to_nasdaq_movers():
    net = FakeNet()
    net.y_screens = {"day_gainers": [{"symbol": "YHOO", "regularMarketChangePercent": 12.0}]}
    net.n_screener = SCREENER

    async def go():
        data = make_data(net)
        yahoo = await data.screener("day_gainers", 5)
        empty = await data.screener("day_losers", 5)  # Yahoo answers an empty list: Nasdaq's then
        net.down = {"yahoo"}
        down = await data.screener("most_actives", 3)
        return yahoo, empty, down
    yahoo, empty, down = run(go())
    assert yahoo == [{"symbol": "YHOO", "regularMarketChangePercent": 12.0}]
    assert [q["symbol"] for q in empty] == ["DOWN", "BRK-B"]
    assert [q["symbol"] for q in down] == ["HUGE", "FLAT", "DOWN"]
    assert net.count("nasdaq", "/screener/stocks") == 2


def test_screener_failures():
    net = FakeNet()
    net.down = {"yahoo"}

    async def go():
        data = make_data(net)
        with pytest.raises(DataUnavailable) as other:
            await data.screener("growth_technology_stocks")
        assert net.count("nasdaq") == 0  # Nasdaq can't make that list
        net.down = {"yahoo", "nasdaq"}
        with pytest.raises(DataUnavailable) as both:
            await data.screener("day_gainers")
        net.down = set()
        net.blocked = {"nasdaq": 429}
        with pytest.raises(DataUnavailable) as limited:
            await data.screener("day_losers")
        return other.value, both.value, limited.value
    other, both, limited = run(go())
    assert str(other).startswith("No growth_technology_stocks list: ")
    assert str(both).count("ConnectionError") == 2 and not both.unknown
    assert limited.status == 429 and "429" in str(limited)


# ----- MarketData: outage and DataUnavailable -----

def _health(data, **states):
    for source, state in states.items():
        if state == "ok":
            data.http.record_ok(source, 200)
        else:
            data.http.record_failure(source, HttpError(source, f"{source}: HTTP {state}", state))


@pytest.mark.parametrize("states, down", [
    ({}, False),
    ({"Yahoo": "ok"}, False),
    ({"Yahoo": 403}, True),  # no backup has been asked
    ({"Yahoo": 403, "Nasdaq": "ok"}, False),
    ({"Yahoo": 403, "Coinbase": "ok"}, False),
    ({"Yahoo": 403, "Nasdaq": 503, "Coinbase": "ok"}, False),
    ({"Yahoo": 403, "Nasdaq": 503}, True),
    ({"Yahoo": 403, "Coinbase": 429}, True),
    ({"Yahoo": 403, "Nasdaq": 503, "Coinbase": 429}, True),
    ({"Yahoo": "ok", "Nasdaq": 503, "Coinbase": 429}, False),
    ({"Nasdaq": 503, "Coinbase": 429}, False),
])
def test_outage_in_each_combination(states, down):
    data = make_data(FakeNet())
    _health(data, **states)
    reason = data.outage()
    assert bool(reason) == down
    if down:
        assert reason == "Yahoo Finance: Yahoo: HTTP 403"


def test_outage_clears_when_yahoo_recovers_and_reports_the_latest_error():
    data = make_data(FakeNet())
    _health(data, Yahoo=403)
    _health(data, Yahoo=429)
    assert data.outage() == "Yahoo Finance: Yahoo: HTTP 429"
    _health(data, Yahoo="ok")
    assert data.outage() is None


def test_data_unavailable_fields():
    e = DataUnavailable("No price history for X", [])
    assert str(e) == "No price history for X: no source has it" and e.status is None and not e.unknown
    e = DataUnavailable("X", [YahooError("not found", 404), HttpError("Nasdaq", "none", 404)])
    assert e.unknown and e.status == 404 and str(e) == "X: not found; none"
    e = DataUnavailable("X", [YahooError("resting"), HttpError("Nasdaq", "HTTP 403", 403)])
    assert e.status == 403 and not e.unknown
    e = DataUnavailable("X", [YahooError("same", 404), YahooError("same", 404)])
    assert str(e) == "X: same" and len(e.errors) == 2  # repeated reasons are said once
    e = DataUnavailable("X", [RuntimeError("y" * 1000)])
    assert len(str(e)) == 400 and e.status is None and not e.unknown
    assert isinstance(e, YahooError)


# ----- HistoryCache: merge and splice -----

def bars_at(closes, day0, at=0, source=None, symbol="TEST"):
    b = from_closes(closes, symbol=symbol, start=day0 * DAY + at)
    if source:
        b.meta = {"source": source}
    return b


def test_merge_matches_days_with_different_timestamps():
    old = bars_at([100 + i for i in range(10)], 1000, at=0, source="Yahoo")  # days 1000..1009 at midnight
    # Nasdaq stamps the same days at 14:30 UTC; day 1009 (old's last, saved mid-day) is different.
    new = bars_at([107.0, 108.0, 111.0, 112.0, 113.0], 1007, at=14 * 3600 + 1800, source="Nasdaq")
    m = merge(old, new)
    assert m is not None and len(m) == 12
    assert list(m.close) == [100 + i for i in range(7)] + [107.0, 108.0, 111.0, 112.0, 113.0]
    assert m.t[6] == 1006 * DAY and m.t[7] == 1007 * DAY + 14 * 3600 + 1800
    assert np.all(np.diff(m.t // DAY) == 1)  # no day twice
    assert source_of(m) == "Nasdaq" and m.symbol == "TEST"


def test_merge_tolerance():
    old = bars_at([100.0] * 10, 2000)
    near = bars_at([100.19] * 5, 2005)
    far = bars_at([100.21] * 5, 2005)
    assert merge(old, near) is not None
    assert merge(old, far) is None  # re-adjusted prices: needs a full download
    assert merge(old, far, tolerance=0.01) is not None
    down = bars_at([99.0] * 5, 2005)
    assert merge(old, down) is None  # disagreement either way


def test_merge_ignores_the_saved_last_bar():
    old = bars_at([100.0] * 9 + [150.0], 3000)  # the last one was saved mid-day
    new = bars_at([100.0, 101.0, 102.0], 3008)  # days 3008, 3009, 3010
    m = merge(old, new)
    assert m is not None and len(m) == 11 and list(m.close[-3:]) == [100.0, 101.0, 102.0]
    only_last = bars_at([5.0], 3009)  # overlaps only the unchecked last bar
    m = merge(old, only_last)
    assert m is not None and len(m) == 10 and m.close[-1] == 5.0


def test_merge_edges():
    old = bars_at([1.0, 2.0, 3.0], 4000)
    empty = Bars("TEST", np.array([], dtype=np.int64), *(np.array([]),) * 5)
    assert merge(old, empty) is old and merge(empty, old) is old
    gap = bars_at([9.0, 10.0], 4010)
    m = merge(old, gap)
    assert len(m) == 5 and list(m.close) == [1.0, 2.0, 3.0, 9.0, 10.0]
    longer = bars_at([0.5, 0.8, 1.0, 2.0, 3.5, 4.0], 3998)  # starts before old (and agrees): it replaces it
    m = merge(old, longer)
    assert len(m) == 6 and list(m.close) == [0.5, 0.8, 1.0, 2.0, 3.5, 4.0]


def test_splice_rescales_older_bars_and_never_loses_history():
    old = bars_at(np.linspace(10, 110, 3000), 5000, source="Yahoo")
    old.volume = np.arange(3000, dtype=float)
    overlap_from = 2500
    new_closes = old.close[overlap_from:] * 1.1  # unadjusted: 10% higher than the dividend-adjusted closes
    new = bars_at(new_closes, 5000 + overlap_from, at=14 * 3600, source="Nasdaq")
    s = splice(old, new)
    assert len(s) == 3000  # the long saved history is kept in front of the backup's
    assert np.allclose(s.close[:overlap_from], old.close[:overlap_from] * 1.1)
    assert np.allclose(s.high[:overlap_from], old.high[:overlap_from] * 1.1)
    assert np.allclose(s.low[:overlap_from], old.low[:overlap_from] * 1.1)
    assert np.allclose(s.open[:overlap_from], old.open[:overlap_from] * 1.1)
    assert np.array_equal(s.volume[:overlap_from], old.volume[:overlap_from])  # volumes aren't prices
    assert np.array_equal(s.close[overlap_from:], new.close) and np.array_equal(s.t[overlap_from:], new.t)
    assert source_of(s) == "Nasdaq"
    assert np.all(np.diff(s.t) > 0)


@pytest.mark.filterwarnings("ignore:divide by zero:RuntimeWarning")
def test_splice_edges():
    old = bars_at([10.0, 11.0, 12.0], 6000)
    later = bars_at([20.0, 21.0], 6010)  # no overlap: nothing to rescale by
    s = splice(old, later)
    assert list(s.close) == [10.0, 11.0, 12.0, 20.0, 21.0]
    absurd = bars_at([12000.0, 13000.0], 6002)  # a factor of 1000 is a bad answer, not an adjustment
    s = splice(old, absurd)
    assert list(s.close) == [10.0, 11.0, 12000.0, 13000.0]
    zero = bars_at([0.0, 11.0, 12.0], 6000)
    s = splice(zero, bars_at([22.0], 6000))
    assert list(s.close) == [22.0]
    s = splice(zero, bars_at([5.0, 6.0], 6001))
    assert list(s.close) == [0.0, 5.0, 6.0]
    empty = Bars("TEST", np.array([], dtype=np.int64), *(np.array([]),) * 5)
    assert splice(old, empty) is old and splice(empty, old) is old
    whole = bars_at([1.0] * 5, 5990)  # the backup reaches back further: it's the longer history
    assert len(splice(old, whole)) == 5


def test_source_of():
    assert source_of(bars_at([1.0], 1)) == "Yahoo"
    assert source_of(bars_at([1.0], 1, source="Coinbase")) == "Coinbase"


# ----- HistoryCache: loading, topping up and re-downloading -----

class Clock:
    def __init__(self, now=float(NOW)):
        self.now = now
        self.mono = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.now += seconds
        self.mono += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cache_mod, "time", c)
    return c


class Hub:
    """MarketData stand-in: answers daily() from a script of Bars, exceptions or functions."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, int | None]] = []

    async def daily(self, symbol, start=None):
        self.calls.append((symbol, start))
        await asyncio.sleep(0)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, BaseException):
            raise a
        return a(symbol, start) if callable(a) else a


def saved_info(cache: HistoryCache, symbol: str) -> dict:
    return cache._read(symbol)[1]


def long_yahoo(n=4000, end_day=TODAY):
    return daily_bars(n, "AAPL", end_day=end_day, first=20.0, step=0.05, at=13 * 3600 + 1800, source="Yahoo")


def test_load_returns_a_fresh_disk_copy_without_asking(tmp_path, clock):
    saved = long_yahoo()
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 100, "full_at": clock.now - 100})
    hub = Hub(AssertionError("no download expected"))
    bars = run(HistoryCache(hub, tmp_path).daily("AAPL"))
    assert hub.calls == [] and len(bars) == len(saved) and np.array_equal(bars.close, saved.close)
    assert source_of(bars) == "Yahoo" and bars.symbol == "AAPL"


def test_load_tops_up_a_stale_copy(tmp_path, clock):
    base = long_yahoo()
    saved = base.slice(0, len(base) - 2)  # two days old
    full_at = clock.now - 86400
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 7 * 3600, "full_at": full_at})
    recent = _mask(base, base.t >= saved.t[-1] - OVERLAP_DAYS * DAY)
    hub = Hub(recent)
    c = HistoryCache(hub, tmp_path)
    bars = run(c.daily("AAPL"))
    assert hub.calls == [("AAPL", int(saved.t[-1]) - OVERLAP_DAYS * DAY)]
    assert len(bars) == len(saved) + 2 and bars.t[-1] == recent.t[-1]
    info = saved_info(c, "AAPL")
    assert info["fetched_at"] == clock.now and info["full_at"] == full_at  # still due for its weekly full download
    assert len(c._read("AAPL")[0]) == len(bars)


def test_load_splices_a_backup_top_up_that_disagrees(tmp_path, clock):
    base = long_yahoo()
    saved = base.slice(0, len(base) - 1)
    full_at = clock.now - 2 * 86400
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 7 * 3600, "full_at": full_at})
    # Yahoo is down; Nasdaq's unadjusted closes are 3% above Yahoo's dividend-adjusted ones.
    nasdaq = long_yahoo()
    nasdaq.close = nasdaq.close * 1.03
    nasdaq.meta = {"source": "Nasdaq"}
    recent = _mask(nasdaq, nasdaq.t >= saved.t[-1] - OVERLAP_DAYS * DAY)
    c = HistoryCache(Hub(recent), tmp_path)
    bars = run(c.daily("AAPL"))
    assert len(bars) == len(saved) + 1  # nothing lost
    assert np.allclose(bars.close, long_yahoo().close * 1.03)  # older bars rescaled onto the backup's prices
    assert source_of(bars) == "Nasdaq"
    info = saved_info(c, "AAPL")
    assert info["full_at"] == full_at and info["fetched_at"] == clock.now


def test_load_redownloads_when_yahoo_re_adjusted_its_prices(tmp_path, clock):
    saved = long_yahoo().slice(0, 3999)
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 7 * 3600,
                                                        "full_at": clock.now - 86400})
    readjusted = long_yahoo()
    readjusted.close = readjusted.close * 0.98  # a dividend re-adjusted every old price
    recent = _mask(readjusted, readjusted.t >= saved.t[-1] - OVERLAP_DAYS * DAY)
    hub = Hub(recent, readjusted)
    c = HistoryCache(hub, tmp_path)
    bars = run(c.daily("AAPL"))
    assert [start is None for _, start in hub.calls] == [False, True]  # top-up refused, then a full download
    assert np.array_equal(bars.close, readjusted.close)
    assert saved_info(c, "AAPL")["full_at"] == clock.now


def test_load_backup_full_download_is_spliced_onto_the_long_yahoo_history(tmp_path, clock):
    saved = long_yahoo(end_day=TODAY - DAY)
    full_at = clock.now - FULL_REFRESH - 3600  # due for a full download
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 7 * 3600, "full_at": full_at})
    nasdaq = daily_bars(2500, "AAPL", first=20.0 + 0.05 * 1501, step=0.05, at=14 * 3600 + 1800, source="Nasdaq")
    nasdaq.close = nasdaq.close * 1.05
    hub = Hub(nasdaq)
    c = HistoryCache(hub, tmp_path)
    bars = run(c.daily("AAPL"))
    assert hub.calls == [("AAPL", None)]
    assert len(bars) == len(saved) + 1  # 4000 days, not Nasdaq's 2500
    assert np.allclose(bars.close[:1500], saved.close[:1500] * 1.05)
    assert np.array_equal(bars.close[-2500:], nasdaq.close)
    assert saved_info(c, "AAPL")["full_at"] == full_at  # a Yahoo full download is still owed


def test_load_yahoo_full_download_replaces(tmp_path, clock):
    saved = long_yahoo(end_day=TODAY - DAY)
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 7 * 3600,
                                                        "full_at": clock.now - FULL_REFRESH - 1})
    fresh = daily_bars(5000, "AAPL", first=5.0, step=0.03, source="Yahoo")
    c = HistoryCache(Hub(fresh), tmp_path)
    bars = run(c.daily("AAPL"))
    assert len(bars) == 5000 and np.array_equal(bars.close, fresh.close)
    assert saved_info(c, "AAPL")["full_at"] == clock.now


def test_load_returns_the_saved_copy_when_every_source_fails(tmp_path, clock):
    saved = long_yahoo(end_day=TODAY - DAY)
    info = {"fetched_at": clock.now - 7 * 3600, "full_at": clock.now - 86400}
    HistoryCache(None, tmp_path)._write("AAPL", saved, info)
    hub = Hub(DataUnavailable("No price history for AAPL", [YahooError("Yahoo: HTTP 429", 429)]))
    c = HistoryCache(hub, tmp_path)
    bars = run(c.daily("AAPL"))
    assert len(hub.calls) == 2  # the top-up, then a full download
    assert np.array_equal(bars.close, saved.close)
    assert saved_info(c, "AAPL")["fetched_at"] == info["fetched_at"]  # not marked as fresh


def test_load_with_nothing_saved_and_every_source_failing_raises(tmp_path, clock):
    c = HistoryCache(Hub(DataUnavailable("No price history for AAPL", [])), tmp_path)

    async def go():
        with pytest.raises(DataUnavailable):
            await c.daily("AAPL")
    run(go())
    assert not (tmp_path / "AAPL.npz").exists() and c.cached("AAPL") is None


def test_load_first_download_is_saved_and_reused(tmp_path, clock):
    fresh = long_yahoo()
    fresh.meta = {"source": "Yahoo", "symbol": "AAPL", "currentTradingPeriod": {"regular": {}}, "n": 3}
    hub = Hub(fresh)
    c = HistoryCache(hub, tmp_path)
    run(c.daily("AAPL"))
    bars, info = c._read("AAPL")
    assert info["fetched_at"] == clock.now and info["full_at"] == clock.now
    assert bars.meta == {"source": "Yahoo", "symbol": "AAPL", "n": 3}  # only plain values are saved
    assert len(bars) == len(fresh)


@pytest.mark.xfail(strict=True, reason="BUG: a first download from a backup is saved with full_at=now, so the "
                                       "full Yahoo history isn't fetched for a week")
def test_load_first_download_from_a_backup_leaves_the_full_download_due(tmp_path, clock):
    nasdaq = daily_bars(2500, "AAPL", source="Nasdaq")
    yahoo = long_yahoo()
    hub = Hub(nasdaq, yahoo)
    c = HistoryCache(hub, tmp_path)
    run(c.daily("AAPL"))
    clock.advance(DISK_TTL + 60)
    bars = run(c.daily("AAPL"))
    assert hub.calls[1] == ("AAPL", None)  # Yahoo is back: the century of adjusted prices, not a top-up
    assert len(bars) == len(yahoo)


def test_load_empty_downloads(tmp_path, clock):
    empty = Bars("NEW", np.array([], dtype=np.int64), *(np.array([]),) * 5, {"source": "Yahoo"})
    c = HistoryCache(Hub(empty), tmp_path)
    assert len(run(c.daily("NEW"))) == 0
    assert not (tmp_path / "NEW.npz").exists()  # nothing worth saving
    saved = long_yahoo()
    HistoryCache(None, tmp_path)._write("AAPL", saved, {"fetched_at": clock.now - 7 * 3600,
                                                        "full_at": clock.now - 86400})
    c = HistoryCache(Hub(empty), tmp_path)
    bars = run(c.daily("AAPL"))
    assert len(bars) == len(saved) and saved_info(c, "AAPL")["fetched_at"] == clock.now


def test_unreadable_saved_history_is_downloaded_again(tmp_path, clock):
    (tmp_path / "AAPL.npz").write_bytes(b"not a zip file")
    fresh = long_yahoo()
    c = HistoryCache(Hub(fresh), tmp_path)
    bars = run(c.daily("AAPL"))
    assert len(bars) == len(fresh) and len(c._read("AAPL")[0]) == len(fresh)


def test_odd_symbols_get_safe_file_names(tmp_path, clock):
    c = HistoryCache(Hub(lambda s, start: long_yahoo(50)), tmp_path)

    async def go():
        for s in ("^GSPC", "GC=F", "BRK-B", "../../etc/passwd", "DX-Y.NYB"):
            await c.daily(s)
    run(go())
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted([".._.._etc_passwd.npz", "BRK-B.npz", "DX-Y.NYB.npz", "GC_F.npz", "_GSPC.npz"])
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


def test_memory_copy_and_concurrent_loads(tmp_path, clock):
    hub = Hub(lambda s, start: long_yahoo(100))
    c = HistoryCache(hub, tmp_path)

    async def go():
        first = await asyncio.gather(*(c.daily("AAPL") for _ in range(8)))
        return first, await c.daily("AAPL")
    first, again = run(go())
    assert len(hub.calls) == 1  # one download for eight concurrent asks
    assert all(b is first[0] for b in first) and again is first[0]
    assert c.cached("AAPL") is first[0] and c.cached("MSFT") is None
    clock.advance(601)
    run(c.daily("AAPL"))  # the memory copy is re-checked, the disk copy is old enough to top up
    assert len(hub.calls) == 2 and hub.calls[1][1] is not None
    run(c.daily("AAPL", fresh=10_000))
    assert len(hub.calls) == 2


def test_memory_keeps_the_most_recent_symbols(tmp_path, clock):
    c = HistoryCache(Hub(lambda s, start: from_closes([1.0, 2.0], symbol=s)), tmp_path)

    async def go():
        for i in range(MEMORY_SYMBOLS + 1):
            await c.daily(f"S{i}")
    run(go())
    assert c.cached("S0") is None and c.cached(f"S{MEMORY_SYMBOLS}") is not None
    assert len(c._memory) == MEMORY_SYMBOLS


# ----- the symbol directory -----

def test_read_write_round_trip(tmp_path):
    items = [Listing("AAA", 'Tab\there "quoted"\nname', STOCKS, "stock", 1.4e9, ("sp500", "ndx100"), "Tech\tnology"),
             Listing("BTC-USD", "Bitcoin", CRYPTO, "crypto", 1.7e12),
             Listing("ZZZ", "Zeta, Inc.", STOCKS, "etf", 0.0, (), "")]
    path = tmp_path / "deep" / "er" / FILENAME
    write(path, items)
    back = read(path)
    assert [i.symbol for i in back] == ["BTC-USD", "AAA", "ZZZ"]  # sorted by market, then symbol
    a = back[1]
    assert a.name == 'Tab here "quoted"\nname' and a.sector == "Tech nology"  # tabs can't break the columns
    assert a.tags == ("sp500", "ndx100") and a.cap == 1.4e9 and a.kind == "stock" and a.market == STOCKS
    assert back[2] == items[2] and back[0] == items[1]
    assert not list(path.parent.glob("*.tmp"))
    write(path, items)
    assert path.read_bytes() == gzip.compress(gzip.decompress(path.read_bytes()), mtime=0)  # reproducible files


def test_read_skips_broken_rows(tmp_path):
    path = tmp_path / FILENAME
    text = ("symbol\tname\tmarket\tkind\tcap\ttags\tsector\n"
            "AAA\tGood\tstocks\tstock\t5\t\t\n"
            "BBB\tBad cap\tstocks\tstock\tlots\t\t\n"
            "CCC\tNo cap\tstocks\tetf\t\t\t\n")
    path.write_bytes(gzip.compress(text.encode()))
    assert [(i.symbol, i.cap) for i in read(path)] == [("AAA", 5.0), ("CCC", 0.0)]


def test_builtins_are_always_there_and_win():
    d = Directory([Listing("^GSPC", "Impostor", STOCKS, "stock"), Listing("AAA", "First", STOCKS, "stock"),
                   Listing("AAA", "Second", STOCKS, "stock"), Listing("", "Blank", STOCKS, "stock")])
    assert d.get("^GSPC").name == "S&P 500" and d.get("^GSPC").kind == "index"
    assert d.get("AAA").name == "First" and d.get("") is None
    for s, kind in (("^NDX", "index"), ("^VIX", "index"), ("GC=F", "future"), ("SI=F", "future"),
                    ("ES=F", "future"), ("^TNX", "index"), ("DX-Y.NYB", "index")):
        assert d.get(s) is not None and d.get(s).kind == kind, s
    assert len(Directory([])) == len(builtins()) and Directory([]).count(CRYPTO) == 0
    assert len(d) == len(builtins()) + 1


def test_directory_basics():
    d = small_directory()
    assert d.is_etf("SPY") and d.is_etf("BTC") and not d.is_etf("AAPL") and not d.is_etf("NOPE")
    assert d.members("ndx100") == ["AAPL", "NVDA", "AMD"] and "JPM" in d.members("sp500")
    assert d.count(CRYPTO) == 3 and d.count(STOCKS) == len(d) - 3
    assert d.coin("hype").symbol == "HYPE32196-USD" and d.coin("HYPE") is d.coin("hype") and d.coin("XYZ") is None
    assert d.get("HYPE32196-USD").ticker == "HYPE" and d.get("BRK-B").ticker == "BRK-B"


@pytest.mark.parametrize("text, symbol", [
    ("nvidia", "NVDA"), ("NVDA", "NVDA"), ("nvda", "NVDA"), ("$nvda", "NVDA"), ("  $NVDA  ", "NVDA"),
    ("s&p", "^GSPC"), ("nasdaq 100", "^NDX"), ("gold", "GC=F"), ("bitcoin", "BTC-USD"),
    ("btc", "BTC-USD"), ("BTC", "BTC-USD"), ("$btc", "BTC-USD"),  # the coin, not the ETF with that ticker
    ("brk.b", "BRK-B"), ("BRK.B", "BRK-B"), ("$BRK.B", "BRK-B"), ("BRK/B", "BRK-B"), ("brk-b", "BRK-B"),
    ("berkshire", "BRK-B"), ("hype", "HYPE32196-USD"), ("HYPE-USD", "HYPE32196-USD"),
    ("hypeusdt", "HYPE32196-USD"), ("HYPE/USD", "HYPE32196-USD"), ("hypeusd", "HYPE32196-USD"),
    ("eth", "ETH-USD"), ("ethusdt", "ETH-USD"), ("ETH-USD", "ETH-USD"), ("eth/usd", "ETH-USD"),
    ("spy", "SPY"), ("jpmorgan", "JPM"), ("jp morgan", "JPM"),
])
def test_lookup(text, symbol):
    found = small_directory().lookup(text)
    assert found is not None and found.symbol == symbol


@pytest.mark.parametrize("text", ["", "   ", "$", "zzzzz", "apple inc", "-USD", "USD", "nvidia corp", "BRK.Z"])
def test_lookup_unknown(text):
    assert small_directory().lookup(text) is None


def test_lookup_precedence_details():
    d = small_directory([Listing("FOO12345-USD", "Foo Coin", CRYPTO, "crypto", 5e8),
                         Listing("FOO99-USD", "Small Foo", CRYPTO, "crypto", 1e6),
                         Listing("FOO", "Foo Industries", STOCKS, "stock", 1e9)])
    assert d.lookup("FOO").symbol == "FOO"  # a listed stock beats a coin people didn't ask for with -USD
    assert d.lookup("foo-usd").symbol == "FOO12345-USD"  # the biggest coin with the ticker
    assert d.lookup("foousdt").symbol == "FOO12345-USD"
    empty = Directory([])
    alias = empty.lookup("hype")  # an alias the list doesn't have is still known
    assert alias.symbol == "HYPE32196-USD" and alias.market == CRYPTO and alias.kind == "crypto"
    assert empty.lookup("tesla").symbol == "TSLA" and empty.lookup("tesla").kind == "stock"
    assert empty.lookup("bitcoin").name == "Bitcoin" and empty.lookup("BTC") is None


@pytest.mark.xfail(strict=True, reason="BUG: lookup strips a USD/USDT suffix before trying the whole ticker, so "
                                       "TUSD resolves to Threshold (T) instead of TrueUSD")
def test_lookup_stablecoin_tickers_ending_in_usd():
    d = bundled()
    assert d.get("TUSD-USD") is not None and d.get("CRVUSD-USD") is not None and d.get("CUSD-USD") is not None
    assert d.lookup("tusd").symbol == "TUSD-USD"
    assert d.lookup("crvUSD").symbol == "CRVUSD-USD"
    assert d.lookup("cusd").symbol == "CUSD-USD"


@pytest.mark.xfail(strict=True, reason="BUG: Listing.ticker strips trailing digits that belong to the ticker, so "
                                       "lookup('API3') and lookup('C98') find nothing")
def test_lookup_coin_tickers_ending_in_digits():
    d = bundled()
    assert d.get("API3-USD") is not None and d.get("C98-USD") is not None
    assert d.lookup("api3") is not None and d.lookup("api3").symbol == "API3-USD"
    assert d.lookup("C98") is not None and d.lookup("C98").symbol == "C98-USD"


def test_search_ranking_on_the_bundled_list():
    d = bundled()
    first = lambda q, **kw: d.search(q, 5, **kw)[0].symbol  # noqa: E731
    assert first("apple") == "AAPL"
    assert first("nv") == "NVDA"
    assert first("jpmorgan") == "JPM"
    assert first("hyperliquid") == "HYPE32196-USD"
    assert first("advanced micro") == "AMD"
    assert first("coinbase") == "COIN"
    assert first("berkshire") == "BRK-B"
    assert first("$brk.b") == "BRK-B"
    assert first("sol", market=CRYPTO) == "SOL-USD"
    assert all(i.market == CRYPTO for i in d.search("sol", 10, market=CRYPTO))
    nvda = [i.symbol for i in d.search("nvda", 5)]
    assert nvda[0] == "NVDA" and len(nvda) == 5  # tokenized NVDA coins come after the stock
    assert d.search("", 5) == [] and d.search("   ", 5) == [] and d.search("$", 5) == []
    assert d.search("qwzxv", 5) == [] and d.search("apple", 0) == []
    assert len(d.search("a", 7)) == 7


def test_search_on_a_small_list():
    d = small_directory([Listing("JPST", "JPMorgan Ultra-Short Income ETF", STOCKS, "etf")])
    assert [i.symbol for i in d.search("jpm", 3)][:2] == ["JPM", "JPST"]
    assert d.search("inc", 5) == []  # only filler words: nothing matches by name alone
    assert d.search("Micro Devices", 3)[0].symbol == "AMD"
    assert d.search("ethereum", 3)[0].symbol == "ETH-USD"


def test_bundled_snapshot():
    d = bundled()
    assert d.updated > 0 and len(d) > 12000
    kinds = Counter(i.kind for i in d.listings)
    assert kinds["stock"] > 6000 and kinds["etf"] > 4000 and kinds["crypto"] >= 900
    assert len(d.members("sp500")) >= 490 and 95 <= len(d.members("ndx100")) <= 105
    assert all(d.get(s).kind == "stock" for s in d.members("sp500") + d.members("ndx100"))
    expect = {"NVDA": ("NVIDIA", "stock", STOCKS), "AAPL": ("Apple", "stock", STOCKS),
              "BRK-B": ("Berkshire Hathaway", "stock", STOCKS), "BTC-USD": ("Bitcoin", "crypto", CRYPTO),
              "SPY": ("S&P 500", "etf", STOCKS), "ETH-USD": ("Ethereum", "crypto", CRYPTO),
              "HYPE32196-USD": ("Hyperliquid", "crypto", CRYPTO), "QQQ": ("QQQ", "etf", STOCKS)}
    for s, (name, kind, market) in expect.items():
        item = d.get(s)
        assert item is not None and name in item.name and item.kind == kind and item.market == market, s
    assert {"sp500", "ndx100"} <= set(d.get("NVDA").tags) and d.get("NVDA").cap > 1e11
    assert d.get("BTC-USD").cap > d.get("ETH-USD").cap > 0
    raw = read(BUNDLED)
    symbols = [i.symbol for i in raw]
    assert len(symbols) == len(set(symbols))
    assert all(i.symbol and i.market in (STOCKS, CRYPTO) and i.kind in ("stock", "etf", "crypto") for i in raw)
    assert all(i.symbol.endswith("-USD") for i in raw if i.market == CRYPTO)
    assert all(re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", i.symbol) for i in raw if i.market == STOCKS)


@pytest.mark.xfail(strict=True, reason="BUG: backup.clean_name cuts a name at any word ending in 'ads', so the "
                                       "snapshot names TEAD 'Te' and ADSE ''")
def test_bundled_snapshot_names_are_whole():
    d = bundled()
    assert all(i.name.strip() for i in d.listings)
    assert d.get("TEAD").name.startswith("Teads")


@pytest.mark.xfail(strict=True, reason="BUG: backup.clean_name cuts 'Threads Inc. Common Stock' to 'Thre' in the "
                                       "Nasdaq movers")
def test_nasdaq_movers_keep_whole_names():
    rows = [screener_row("THRD", "5,000,000,000", "$10.00", "5%", "1", "Threads Inc. Common Stock"),
            screener_row("ADSE", "5,000,000,000", "$10.00", "4%", "1", "ADS-TEC Energy plc Ordinary Shares")]
    names = [q["shortName"] for q in nasdaq_movers(rows, "day_gainers", 5)]
    assert names == ["Threads Inc.", "ADS-TEC Energy plc"]


def _listings(n, prefix="S", kind="stock", market=STOCKS):
    return [Listing(f"{prefix}{i:04d}" + ("-USD" if market == CRYPTO else ""), f"{prefix} {i}", market, kind,
                    float(n - i)) for i in range(n)]


def test_load_prefers_the_refreshed_copy(tmp_path):
    write(tmp_path / FILENAME, _listings(1200))
    d = Directory.load(tmp_path)
    assert d.get("S0001") is not None and d.get("NVDA") is None and len(d) == 1200 + len(builtins())
    assert d.updated == pytest.approx((tmp_path / FILENAME).stat().st_mtime)


def test_load_falls_back_to_the_bundled_list(tmp_path):
    (tmp_path / FILENAME).write_bytes(b"\x1f\x8b not really gzip")
    d = Directory.load(tmp_path)
    assert d.get("NVDA") is not None and d.updated == pytest.approx(BUNDLED.stat().st_mtime)
    write(tmp_path / FILENAME, _listings(1000))  # too short to be the real list
    d = Directory.load(tmp_path)
    assert d.get("NVDA") is not None and d.get("S0001") is None
    assert Directory.load(tmp_path / "nowhere").get("NVDA") is not None


def test_load_with_no_list_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(dir_mod, "BUNDLED", tmp_path / "missing.tsv.gz")
    d = Directory.load(tmp_path)
    assert len(d) == len(builtins()) and d.updated == 0.0
    assert d.lookup("nvidia").symbol == "NVDA"  # aliases still work


class FakeListSources:
    """The http, nasdaq and yahoo objects download() talks to."""

    def __init__(self, stocks=3200, etfs=20, coins=600, crypto_fail_from=None):
        self.stocks, self.etfs, self.coins, self.crypto_fail_from = stocks, etfs, coins, crypto_fail_from
        self.calls: list[str] = []

    async def get(self, url, **kw):
        self.calls.append("sp500")
        return Response(200, "Symbol,Security\nS0001,Stock 1\nBRK.B,Berkshire\n")

    async def nasdaq100(self):
        self.calls.append("ndx")
        return ["S0002"]

    async def screener(self):
        self.calls.append("stocks")
        rows = [{"symbol": f"S{i:04d}", "name": f"Stock {i} Common Stock", "marketCap": f"{1e9 + i:.0f}",
                 "sector": "Technology"} for i in range(self.stocks)]
        rows += [{"symbol": "BRK/B", "name": "Berkshire Hathaway Inc. Class B Common Stock", "marketCap": "1e12"},
                 {"symbol": "ABR^D", "name": "Preferred", "marketCap": "1"}, {"symbol": "lower1", "name": "x"},
                 {"symbol": "", "name": "blank"}, {"symbol": "TOOLONGSYMBOL", "name": "x"}]
        return rows

    async def _get(self, path, params=None):
        self.calls.append(path)
        rows = [{"symbol": f"E{i:03d}", "companyName": f"ETF {i} "} for i in range(self.etfs)]
        rows.append({"symbol": "S0001", "companyName": "dupe of a stock"})
        return {"data": {"data": {"rows": rows}}}

    async def get_json(self, url, params=None, crumb=False):
        start = int(params["start"])
        self.calls.append(f"crypto{start}")
        if self.crypto_fail_from is not None and start >= self.crypto_fail_from:
            raise YahooError("Yahoo: HTTP 429", 429)
        quotes = [{"symbol": f"C{i:04d}-USD", "shortName": f"Coin {i} USD", "marketCap": 1e9 - i}
                  for i in range(start, min(start + 250, self.coins))]
        quotes.append({"symbol": "NOTACOIN", "shortName": "x"})
        return {"finance": {"result": [{"quotes": quotes}]}}


def test_download_builds_the_whole_list():
    src = FakeListSources()
    listings = run(dir_mod.download(src, src, src))
    by = {i.symbol: i for i in listings}
    assert sum(1 for i in listings if i.kind == "stock") == 3202  # with BRK-B and LOWER1
    assert by["BRK-B"].tags == ("sp500",) and by["BRK-B"].name == "Berkshire Hathaway Inc. Class B"
    assert by["S0001"].tags == ("sp500",) and by["S0002"].tags == ("ndx100",) and by["S0001"].kind == "stock"
    assert by["S0003"].name == "Stock 3" and by["S0003"].sector == "Technology" and by["S0003"].cap == 1e9 + 3
    assert "ABR^D" not in by and "TOOLONGSYMBOL" not in by and "" not in by
    assert by["LOWER1"].name == "x"  # Nasdaq's symbols are upper-cased
    assert by["E001"].kind == "etf" and by["E001"].name == "ETF 1"
    assert by["C0000-USD"].market == CRYPTO and by["C0000-USD"].name == "Coin 0" and "NOTACOIN" not in by
    assert sum(1 for i in listings if i.market == CRYPTO) == 600
    assert "crypto750" not in src.calls  # the third page was short: no more pages
    with pytest.raises(RuntimeError):
        run(dir_mod.download(*(FakeListSources(stocks=2990),) * 3))


def test_refresh_skips_a_fresh_list(tmp_path):
    d = Directory(_listings(50), updated=time.time() - 3600)
    src = FakeListSources()
    assert run(refresh(d, tmp_path, src, src, src)) is d
    assert src.calls == [] and not (tmp_path / FILENAME).exists()


def test_refresh_writes_a_new_list(tmp_path):
    old = Directory(_listings(3000) + _listings(500, "C", "crypto", CRYPTO), updated=time.time() - 8 * 86400)
    src = FakeListSources()
    new = run(refresh(old, tmp_path, src, src, src))
    assert new is not old and new.updated == pytest.approx(time.time(), abs=60)
    assert new.get("C0599-USD") is not None and new.get("E005") is not None
    again = Directory.load(tmp_path)
    assert len(again) == len(new) and again.get("BRK-B").tags == ("sp500",)


def test_refresh_keeps_the_old_coins_when_yahoo_fails(tmp_path):
    old_coins = [Listing(f"OLD{i}-USD", f"Old coin {i}", CRYPTO, "crypto", 1e9 - i) for i in range(400)]
    old = Directory(_listings(3000) + old_coins, updated=0.0)
    src = FakeListSources(crypto_fail_from=0)
    new = run(refresh(old, tmp_path, src, src, src))
    assert new.count(CRYPTO) == 400 and new.get("OLD7-USD").name == "Old coin 7"
    assert new.coin("OLD") is not None


@pytest.mark.xfail(strict=True, reason="BUG: when Yahoo's crypto list fails after its first page, refresh drops "
                                       "every coin past that page")
def test_refresh_keeps_the_old_coins_when_yahoo_fails_part_way(tmp_path):
    old = Directory(_listings(3200) + _listings(1000, "C", "crypto", CRYPTO), updated=0.0)
    src = FakeListSources(coins=1000, crypto_fail_from=250)  # page one comes back, page two doesn't
    new = run(refresh(old, tmp_path, src, src, src))
    assert new.get("C0900-USD") is not None  # Yahoo never said this coin is gone


def test_refresh_refuses_a_much_shorter_list(tmp_path):
    old = Directory(_listings(5000), updated=0.0)
    src = FakeListSources(stocks=3500, coins=0)
    with pytest.raises(RuntimeError, match="much shorter"):
        run(refresh(old, tmp_path, src, src, src))
    assert not (tmp_path / FILENAME).exists()


# ----- Engine.resolve and suggest -----

@pytest.fixture
def engines(tmp_path):
    made = []

    def make(net, directory=None, coins=None):
        eng = Engine(tmp_path, data=make_data(net, directory if directory is not None else bundled(), coins),
                     sources=Sources())
        made.append(eng)
        return eng
    yield make
    for eng in made:
        eng._pool.shutdown(wait=False)


ALL_DOWN = {"yahoo", "nasdaq", "coinbase"}


def test_resolve_from_the_directory_with_every_source_down(engines):
    net = FakeNet()
    net.down = set(ALL_DOWN)
    eng = engines(net)

    async def go():
        return [await eng.resolve(t) for t in ("nvidia", "brk.b", "hype", "$NVDA", "BTC", "s&p", "spy", "hyperliquid")]
    nvda, brk, hype, nvda2, btc, spx, spy, hype2 = run(go())
    assert net.calls == []  # answered without asking anyone
    assert nvda == Resolved("NVDA", "NVIDIA Corporation", STOCKS) and nvda2 == nvda
    assert brk.symbol == "BRK-B" and brk.market == STOCKS and "Berkshire" in brk.name
    assert hype == Resolved("HYPE32196-USD", "Hyperliquid", CRYPTO) and hype2 == hype
    assert btc == Resolved("BTC-USD", "Bitcoin", CRYPTO)
    assert spx == Resolved("^GSPC", "S&P 500", STOCKS) and spy.symbol == "SPY"


def test_resolve_names_through_the_directory_search(engines):
    net = FakeNet()
    net.down = set(ALL_DOWN)
    eng = engines(net)

    async def go():
        return await eng.resolve("advanced micro devices"), await eng.resolve("Coinbase Global")
    amd, coin_ = run(go())
    assert amd.symbol == "AMD" and coin_.symbol == "COIN"


def test_resolve_unknown_with_yahoo_answering(engines):
    net = FakeNet()
    eng = engines(net)

    async def go():
        with pytest.raises(UnknownSymbol):
            await eng.resolve("qwzxv")
    run(go())
    assert net.count("yahoo", "/v1/finance/search") == 1 and net.count("nasdaq", "/watchlist") >= 1
    assert eng.data.outage() is None


def test_resolve_unknown_with_every_source_failing(engines):
    net = FakeNet()
    net.down = set(ALL_DOWN)
    eng = engines(net)

    async def go():
        with pytest.raises(SourcesDown) as e:
            await eng.resolve("qwzxv")
        return e.value
    exc = run(go())
    assert str(exc).startswith("Yahoo Finance: ") and "ConnectionError" in str(exc)
    assert "qwzxv" not in eng._resolved


def test_resolve_unknown_while_yahoo_is_down_but_nasdaq_answers(engines):
    net = FakeNet()
    net.down = {"yahoo"}
    eng = engines(net)

    async def go():
        with pytest.raises(UnknownSymbol):
            await eng.resolve("qwzxv")
    run(go())  # Nasdaq said it has no such stock


def test_resolve_a_ticker_the_directory_lacks(engines):
    net = FakeNet()
    net.y_quotes = {"ZZZQ": y_quote("ZZZQ", 12.0, 11.0, "Zeta Quantum Corp"),
                    "NEWC-USD": y_quote("NEWC-USD", 0.5, 0.4, "Newcoin USD", "CRYPTOCURRENCY")}
    eng = engines(net)
    assert eng.directory.lookup("zzzq") is None

    async def go():
        return await eng.resolve("zzzq"), await eng.resolve("NEWC-USD")
    z, c = run(go())
    assert z == Resolved("ZZZQ", "Zeta Quantum Corp", STOCKS)
    assert c == Resolved("NEWC-USD", "Newcoin USD", CRYPTO)


def test_resolve_a_ticker_the_directory_lacks_while_yahoo_is_down(engines):
    net = FakeNet()
    net.down = {"yahoo"}
    net.n_quotes = {"ZZZQ": ("stocks", n_row("ZZZQ", "Zeta Quantum Corp Common Stock", 12.0, 11.0))}
    eng = engines(net)
    r = run(eng.resolve("zzzq"))
    assert r == Resolved("ZZZQ", "Zeta Quantum Corp", STOCKS)


def test_resolve_falls_back_to_yahoo_search(engines):
    net = FakeNet()
    net.y_search = {"xyzzy quux": [{"symbol": "XYQ.OPT", "quoteType": "OPTION", "shortname": "an option"},
                                   {"quoteType": "EQUITY"},
                                   {"symbol": "XYQ.L", "quoteType": "EQUITY", "longname": "Xyzzy Quux plc"}]}
    eng = engines(net)
    r = run(eng.resolve("Xyzzy Quux"))
    assert r == Resolved("XYQ.L", "Xyzzy Quux plc", STOCKS)


def test_resolve_caches_answers(engines):
    net = FakeNet()
    net.y_quotes = {"ZZZQ": y_quote("ZZZQ", 12.0, 11.0, "Zeta Quantum Corp")}
    eng = engines(net)

    async def go():
        first = await eng.resolve("ZZZQ")
        calls = len(net.calls)
        again = await eng.resolve("  zzzq ")
        assert len(net.calls) == calls and again is first
        net.down = set(ALL_DOWN)
        assert await eng.resolve("zzzq") is first  # still known while the sources are down
        eng.forget_lookups()
        with pytest.raises(SourcesDown):
            await eng.resolve("zzzq")
    run(go())


def test_resolve_cache_is_bounded(engines):
    net = FakeNet()
    eng = engines(net)
    eng._resolved = {f"k{i}": Resolved("X", "X", STOCKS) for i in range(2001)}
    run(eng.resolve("nvidia"))
    assert list(eng._resolved) == ["nvidia"]


@pytest.mark.parametrize("text, name, symbol, ok", [
    ("advanced micro", "Advanced Micro Devices, Inc.", "AMD", True),
    ("jpmorgan", "JP Morgan Chase & Co.", "JPM", True),
    ("Coinbase", "Coinbase Global, Inc.", "COIN", True),
    ("amd", "Advanced Micro Devices, Inc.", "AMD", True),
    ("micro adv", "Advanced Micro Devices, Inc.", "AMD", True),
    ("apple pie", "Apple Inc.", "AAPL", False),
    ("pple", "Apple Inc.", "AAPL", False),
    ("", "Apple Inc.", "AAPL", False),
    ("!!!", "Apple Inc.", "AAPL", False),
])
def test_names_match(text, name, symbol, ok):
    assert _names_match(text, name, symbol) is ok


def test_suggest(engines):
    net = FakeNet()
    net.y_search = {"xyzzy": [{"symbol": "XYZY", "quoteType": "EQUITY", "shortname": "Xyzzy Inc", "exchDisp": "NYSE"},
                              {"symbol": "XYZY240C", "quoteType": "OPTION"},
                              {"symbol": "XYZF", "quoteType": "FUTURE", "longname": "Xyzzy future"}]}
    eng = engines(net)

    async def go():
        nv = await eng.suggest("nv")
        assert net.calls == []  # plenty of directory hits: Yahoo isn't asked
        odd = await eng.suggest("xyzzy")
        calls = len(net.calls)
        assert await eng.suggest("XYZZY ") == odd and len(net.calls) == calls  # remembered
        return nv, odd, await eng.suggest("   ")
    nv, odd, blank = run(go())
    assert nv[0] == ("NVDA · NVIDIA Corporation (stock)", "NVDA") and len(nv) == 12
    assert all(len(label) <= 100 for label, _ in nv)
    assert odd == [("XYZY · Xyzzy Inc (NYSE)", "XYZY"), ("XYZF · Xyzzy future ()", "XYZF")]
    assert blank == []


def test_suggest_with_yahoo_down(engines):
    net = FakeNet()
    net.down = set(ALL_DOWN)
    eng = engines(net)
    assert run(eng.suggest("xyzzy")) == []
    hype = run(eng.suggest("hyperliq"))
    assert hype[0] == ("HYPE · Hyperliquid (crypto)", "HYPE32196-USD")


def test_engine_builds_its_own_market_data(tmp_path):
    write(tmp_path / FILENAME, _listings(1500))
    eng = Engine(tmp_path, sources=SimpleNamespace(top_coins=None))
    try:
        assert eng.directory is eng.data.directory and eng.directory.get("S0001") is not None
        assert eng.cache.data is eng.data and eng.cache.folder == Path(tmp_path) / "history"
        assert eng.data.coins is not None and eng.data.nasdaq.etfs("SPY") is False
    finally:
        eng._pool.shutdown(wait=False)
