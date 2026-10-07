"""Every US-listed stock and ETF and the top cryptocurrencies, with their names, so lookups ("nvidia", "brk.b",
"hyperliquid", "NV…" while typing) work instantly and without asking a data source.

A snapshot ships with the bot (marketbot/data/symbols.tsv.gz) and is refreshed weekly from Nasdaq's stock and ETF
screeners, Yahoo's crypto screener (whose symbols the bot uses, e.g. HYPE32196-USD for Hyperliquid), the S&P 500
list and the Nasdaq-100 list.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .universe import ALIASES, COINS, CRYPTO, FUTURES, INDICES, MACRO, NAMES, SECTORS, STOCKS, coin_base

log = logging.getLogger(__name__)

BUNDLED = Path(__file__).parent / "data" / "symbols.tsv.gz"
FILENAME = "symbols.tsv.gz"
FIELDS = ("symbol", "name", "market", "kind", "cap", "tags", "sector")
REFRESH_DAYS = 7
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
CRYPTO_PAGES = 4  # x 250 coins, by market cap
DERIVED = re.compile(r"tokeni[sz]ed|xstock|bstock|\bwrapped\b|\bbridged\b|\bstaked\b|\bondo\b", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "the", "holdings", "group", "class", "sa",
         "nv", "ag", "se", "lp", "llc", "trust", "etf", "fund", "usd", "shares"}
EXTRA_NAMES = {"^NDX": ("Nasdaq 100", "index"), "SI=F": ("Silver", "future"), "BZ=F": ("Brent crude", "future"),
               "NG=F": ("Natural gas", "future"), "HG=F": ("Copper", "future")}


@dataclass(frozen=True)
class Listing:
    symbol: str  # Yahoo symbol
    name: str
    market: str  # stocks or crypto
    kind: str  # stock, etf, index, future or crypto
    cap: float = 0.0  # market cap in dollars (0 when unknown)
    tags: tuple[str, ...] = ()  # "sp500", "ndx100"
    sector: str = ""

    @property
    def ticker(self) -> str:
        """What people type: BRK-B -> BRK-B, HYPE32196-USD -> HYPE, ^GSPC -> ^GSPC."""
        if self.market == CRYPTO and self.symbol.endswith("-USD"):
            return coin_base(self.symbol)
        return self.symbol


def builtins() -> list[Listing]:
    """Indices, futures and rates the screeners don't list."""
    out = [Listing(a.symbol, a.name, STOCKS, "future" if a.symbol.endswith("=F") else "index")
           for a in INDICES + FUTURES + MACRO]
    out += [Listing(s, n, STOCKS, k) for s, (n, k) in EXTRA_NAMES.items()]
    return out


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def weight(item: Listing) -> float:
    """How likely someone means this listing, all else equal: big, famous and plain beats small and derived."""
    w = 10 * math.log10(item.cap + 1)
    if item.kind == "index":
        w += 25
    if "sp500" in item.tags or "ndx100" in item.tags:
        w += 15
    if item.symbol in SECTORS:
        w += 12
    if item.market == CRYPTO and DERIVED.search(item.name):
        w -= 150  # tokenized stocks, wrapped and staked coins
    return w


class Directory:
    def __init__(self, listings: list[Listing], updated: float = 0.0):
        self.updated = updated
        self.listings: list[Listing] = []
        self._by_symbol: dict[str, Listing] = {}
        self._coins: dict[str, Listing] = {}
        for item in builtins() + list(listings):
            if not item.symbol or item.symbol in self._by_symbol:
                continue
            self._by_symbol[item.symbol] = item
            self.listings.append(item)
            if item.market == CRYPTO:
                best = self._coins.get(item.ticker)
                if best is None or item.cap > best.cap:
                    self._coins[item.ticker] = item
        # What search compares against, worked out once.
        self._index = [(item, item.symbol.lower(), item.ticker.lower(), item.name.lower(), words(item.name),
                        "".join(words(item.name)), weight(item)) for item in self.listings]

    def __len__(self) -> int:
        return len(self.listings)

    def count(self, market: str) -> int:
        return sum(1 for i in self.listings if i.market == market)

    def get(self, symbol: str) -> Listing | None:
        return self._by_symbol.get(symbol)

    def coin(self, ticker: str) -> Listing | None:
        """The biggest coin with this ticker (HYPE -> Hyperliquid, HYPE32196-USD)."""
        return self._coins.get(ticker.upper())

    def is_etf(self, symbol: str) -> bool:
        item = self._by_symbol.get(symbol)
        return bool(item and item.kind == "etf")

    def members(self, tag: str) -> list[str]:
        return [i.symbol for i in self.listings if tag in i.tags]

    def lookup(self, text: str) -> Listing | None:
        """The listing someone means by a ticker or alias ("nvda", "$BRK.B", "btc", "hype", "s&p"), or None."""
        raw = text.strip().lstrip("$")
        if not raw:
            return None
        low = raw.lower()
        if low in ALIASES:
            return self._by_symbol.get(ALIASES[low]) or Listing(ALIASES[low], NAMES.get(ALIASES[low], ALIASES[low]),
                                                                CRYPTO if ALIASES[low].endswith("-USD") else STOCKS,
                                                                "crypto" if ALIASES[low].endswith("-USD") else "stock")
        up = raw.upper().replace(" ", "")
        if up in COINS and self.coin(up):  # BTC is Bitcoin, not the ETF with that ticker
            return self.coin(up)
        if up in self._by_symbol:
            return self._by_symbol[up]
        stock = self._by_symbol.get(up.replace(".", "-").replace("/", "-"))
        if stock:
            return stock
        if self.coin(up):  # a coin's own ticker first: TUSD is TrueUSD, not Threshold (T) priced in dollars
            return self.coin(up)
        for suffix in ("-USD", "/USD", "USDT", "USD"):
            if up.endswith(suffix) and len(up) > len(suffix) and self.coin(up[:-len(suffix)]):
                return self.coin(up[:-len(suffix)])
        return None

    def search(self, text: str, limit: int = 10, market: str | None = None) -> list[Listing]:
        """Best matches for a ticker or a name, most likely first."""
        q = text.strip().lstrip("$").lower()
        if not q:
            return []
        exact = self.lookup(text)
        q_words = [w for w in words(q) if w]
        sym_q = q.upper().replace(".", "-").replace("/", "-").lower()
        scored: list[tuple[float, Listing]] = []
        q_compact = "".join(q_words)
        for item, sym, ticker, name, name_words, compact, bonus in self._index:
            if market and item.market != market:
                continue
            if item is exact:
                score = 2000.0
            elif sym == sym_q or ticker == sym_q:
                score = 1000.0
            elif name == q:
                score = 900.0
            elif sym.startswith(sym_q) or ticker.startswith(sym_q):
                score = 600.0 - 20 * (len(ticker) - len(sym_q))
            elif name.startswith(q):
                score = 500.0
            elif len(q_compact) >= 4 and compact.startswith(q_compact):
                score = 450.0  # "jpmorgan" for "JP Morgan Chase"
            elif q_words and all(any(w.startswith(qw) for w in name_words) for qw in q_words):
                score = 400.0 if not set(q_words) <= _STOP else 0.0
            elif len(q) >= 4 and q in name:
                score = 200.0
            else:
                continue
            if score:
                scored.append((score + bonus, item))
        scored.sort(key=lambda p: -p[0])
        return [item for _, item in scored[:limit]]

    # ----- files -----

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "Directory":
        """The newest good copy: the weekly refresh in the data folder, else the bundled snapshot."""
        candidates = ([Path(data_dir) / FILENAME] if data_dir else []) + [BUNDLED]
        for path in candidates:
            try:
                listings = read(path)
            except FileNotFoundError:
                continue
            except Exception:
                log.warning("Symbol list %s is unreadable", path, exc_info=True)
                continue
            if len(listings) > 1000:
                return cls(listings, path.stat().st_mtime)
        log.warning("No symbol list found; lookups will ask Yahoo")
        return cls([])


def read(path: Path) -> list[Listing]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        rows = csv.DictReader(f, delimiter="\t")
        out = []
        for r in rows:
            try:
                out.append(Listing(r["symbol"], r["name"], r["market"], r["kind"], float(r.get("cap") or 0),
                                   tuple(t for t in (r.get("tags") or "").split(",") if t), r.get("sector") or ""))
            except (KeyError, ValueError):
                continue
        return out


def write(path: Path, listings: list[Listing]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(FIELDS)
    for i in sorted(listings, key=lambda i: (i.market, i.symbol)):
        w.writerow([i.symbol, i.name.replace("\t", " "), i.market, i.kind, f"{i.cap:.0f}", ",".join(i.tags),
                    i.sector.replace("\t", " ")])
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.compress(buf.getvalue().encode("utf-8"), mtime=0))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ----- building the list -----

def crypto_name(name: str) -> str:
    return re.sub(r"\s+USD$", "", (name or "").strip())


async def download(http, nasdaq, yahoo) -> list[Listing]:
    """The full list, fresh. Raises if the stock list can't be fetched; the index lists and coins are optional
    (refresh() keeps the old ones when they come back short)."""
    from .backup import clean_name, from_nasdaq_symbol, number

    sp500: set[str] = set()
    try:
        resp = await http.get(SP500_CSV, source="GitHub")
        if resp.status == 200:
            sp500 = {r["Symbol"].strip().replace(".", "-") for r in csv.DictReader(io.StringIO(resp.text))
                     if r.get("Symbol")}
    except Exception:
        log.warning("Couldn't get the S&P 500 list", exc_info=True)
    ndx: set[str] = set()
    try:
        ndx = set(await nasdaq.nasdaq100())
    except Exception:
        log.warning("Couldn't get the Nasdaq-100 list", exc_info=True)

    out: list[Listing] = []
    for row in await nasdaq.screener():
        sym = from_nasdaq_symbol(str(row.get("symbol") or ""))
        if not sym or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", sym) or "^" in str(row.get("symbol")):
            continue  # preferred shares and other odd lines
        tags = tuple(t for t, members in (("sp500", sp500), ("ndx100", ndx)) if sym in members)
        out.append(Listing(sym, clean_name(str(row.get("name") or sym)), STOCKS, "stock",
                           number(row.get("marketCap")) or 0.0, tags, str(row.get("sector") or "")))
    if len(out) < 3000:
        raise RuntimeError(f"Nasdaq's stock list looks incomplete ({len(out)} stocks)")
    have = {i.symbol for i in out}
    data = await nasdaq._get("/screener/etf", {"tableonly": "true", "download": "true"})
    for row in (((data.get("data") or {}).get("data") or {}).get("rows")) or []:
        sym = from_nasdaq_symbol(str(row.get("symbol") or ""))
        if sym and sym not in have and re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", sym):
            out.append(Listing(sym, str(row.get("companyName") or sym).strip(), STOCKS, "etf"))
            have.add(sym)
    for start in range(0, CRYPTO_PAGES * 250, 250):
        try:
            data = await yahoo.get_json("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                                        {"scrIds": "all_cryptocurrencies_us", "count": "250", "start": str(start)})
        except Exception:
            log.warning("Couldn't get Yahoo's crypto list (from %d)", start, exc_info=True)
            break
        quotes = (((data or {}).get("finance") or {}).get("result") or [{}])[0].get("quotes") or []
        for q in quotes:
            sym = q.get("symbol") or ""
            if sym.endswith("-USD") and sym not in have:
                out.append(Listing(sym, crypto_name(q.get("shortName") or q.get("longName") or sym), CRYPTO,
                                   "crypto", float(q.get("marketCap") or 0)))
                have.add(sym)
        if len(quotes) < 250:
            break
    return out


MIN_MEMBERS = {"sp500": 450, "ndx100": 90}  # fewer means the list download failed


async def refresh(directory: Directory, data_dir: str | Path, http, nasdaq, yahoo) -> Directory:
    """A fresh list when the saved one is a week old; the old one if the download fails or looks wrong. Parts that
    came back short (the index lists, Yahoo's coins) are filled in from the old list instead of being lost."""
    if time.time() - directory.updated < REFRESH_DAYS * 86400:
        return directory
    listings = merge_lists(await download(http, nasdaq, yahoo), directory)
    if len(listings) < 0.8 * len(directory):
        raise RuntimeError(f"The new symbol list is much shorter ({len(listings)} vs {len(directory)})")
    write(Path(data_dir) / FILENAME, listings)
    return Directory(listings, time.time())


def merge_lists(fresh: list[Listing], old: Directory) -> list[Listing]:
    """The fresh list, keeping the old index tags when a fresh index list is missing or short, and the old coins
    when fewer came back (Yahoo's crypto list failed part way)."""
    from dataclasses import replace
    out = list(fresh)
    for tag, minimum in MIN_MEMBERS.items():
        if sum(1 for i in out if tag in i.tags) < minimum:
            members = set(old.members(tag))
            out = [replace(i, tags=tuple(sorted(set(i.tags) | {tag}))) if i.symbol in members else i for i in out]
    old_coins = [i for i in old.listings if i.market == CRYPTO and i.kind == "crypto"]
    new_coins = sum(1 for i in out if i.market == CRYPTO)
    if new_coins < 0.9 * len(old_coins):
        have = {i.symbol for i in out}
        out += [i for i in old_coins if i.symbol not in have]
    return out
