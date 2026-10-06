"""The symbols the bot knows by default, friendly names for them, and which market each belongs to."""

from __future__ import annotations

import re
from dataclasses import dataclass

STOCKS = "stocks"
CRYPTO = "crypto"
MARKETS = (STOCKS, CRYPTO)


@dataclass(frozen=True)
class Asset:
    symbol: str  # Yahoo symbol
    name: str
    market: str
    emoji: str = ""


# The headline instruments on each board, in the order shown.
INDICES = [
    Asset("^GSPC", "S&P 500", STOCKS, "🇺🇸"),
    Asset("^IXIC", "Nasdaq", STOCKS, "💻"),
    Asset("^DJI", "Dow Jones", STOCKS, "🏭"),
    Asset("^RUT", "Russell 2000", STOCKS, "🏘️"),
    Asset("^VIX", "VIX (fear gauge)", STOCKS, "😱"),
]
FUTURES = [
    Asset("ES=F", "S&P futures", STOCKS),
    Asset("NQ=F", "Nasdaq futures", STOCKS),
    Asset("YM=F", "Dow futures", STOCKS),
]
MACRO = [
    Asset("^TNX", "US 10-year yield", STOCKS, "🏦"),
    Asset("^IRX", "US 3-month yield", STOCKS, "🏦"),
    Asset("DX-Y.NYB", "US dollar index", STOCKS, "💵"),
    Asset("GC=F", "Gold", STOCKS, "🥇"),
    Asset("CL=F", "Crude oil (WTI)", STOCKS, "🛢️"),
]
DEFAULT_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "BRK-B",
                  "LLY", "V", "XOM", "WMT", "AMD", "NFLX", "PLTR", "COST"]
DEFAULT_CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "DOGE-USD", "ADA-USD", "AVAX-USD",
                  "LINK-USD", "TRX-USD", "SUI20947-USD", "TON11419-USD", "LTC-USD", "DOT-USD"]
# Sector ETFs: where money is rotating, for the close recap and news impact.
SECTORS = {
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health care", "XLY": "Consumer discretionary",
    "XLP": "Consumer staples", "XLI": "Industrials", "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate",
    "XLC": "Communication", "SMH": "Semiconductors",
}
# Long histories used to train the stocks model and to search for historical look-alikes.
STOCK_TRAINING = ["^GSPC", "^DJI", "^IXIC", "^RUT", "AAPL", "MSFT", "IBM", "KO", "XOM", "JPM", "GE", "PG", "JNJ",
                  "WMT", "INTC", "CVX", "PFE", "MRK", "HD", "DIS", "CAT", "MMM", "NVDA", "AMZN", "CSCO", "ORCL", "BA"]
CRYPTO_TRAINING = ["BTC-USD", "ETH-USD", "XRP-USD", "LTC-USD", "BNB-USD", "DOGE-USD", "ADA-USD", "SOL-USD",
                   "LINK-USD", "TRX-USD", "XLM-USD", "DOT-USD", "AVAX-USD", "BCH-USD"]
BENCHMARK = {STOCKS: "^GSPC", CRYPTO: "BTC-USD"}

# What people type -> Yahoo symbol.
ALIASES = {
    "spx": "^GSPC", "s&p": "^GSPC", "s&p500": "^GSPC", "s&p 500": "^GSPC", "sp500": "^GSPC", "gspc": "^GSPC",
    "spy500": "^GSPC", "dow": "^DJI", "dji": "^DJI", "djia": "^DJI", "dow jones": "^DJI", "nasdaq": "^IXIC",
    "ixic": "^IXIC", "comp": "^IXIC", "ndx": "^NDX", "nasdaq 100": "^NDX", "russell": "^RUT", "rut": "^RUT",
    "russell 2000": "^RUT", "vix": "^VIX", "tnx": "^TNX", "10y": "^TNX", "10 year": "^TNX", "dxy": "DX-Y.NYB",
    "dollar": "DX-Y.NYB", "gold": "GC=F", "xau": "GC=F", "silver": "SI=F", "oil": "CL=F", "wti": "CL=F",
    "brent": "BZ=F", "natgas": "NG=F", "copper": "HG=F", "es": "ES=F", "nq": "NQ=F", "ym": "YM=F",
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "ether": "ETH-USD", "solana": "SOL-USD", "ripple": "XRP-USD",
    "dogecoin": "DOGE-USD", "cardano": "ADA-USD", "sui": "SUI20947-USD", "ton": "TON11419-USD",
    "toncoin": "TON11419-USD", "uni": "UNI7083-USD", "uniswap": "UNI7083-USD", "pepe": "PEPE24478-USD",
    "brk.b": "BRK-B", "brkb": "BRK-B", "brk.a": "BRK-A", "google": "GOOGL", "alphabet": "GOOGL",
    "facebook": "META", "tesla": "TSLA", "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "hype": "HYPE32196-USD", "hyperliquid": "HYPE32196-USD", "tron": "TRX-USD", "avalanche": "AVAX-USD",
    "chainlink": "LINK-USD", "polkadot": "DOT-USD", "litecoin": "LTC-USD", "binance coin": "BNB-USD",
    "jpmorgan": "JPM", "jp morgan": "JPM", "berkshire": "BRK-B", "netflix": "NFLX", "palantir": "PLTR",
}
# Coins people write without "-USD".
COINS = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "TRX", "LTC", "DOT", "BCH", "XLM",
         "SHIB", "NEAR", "APT", "ARB", "OP", "ATOM", "HBAR", "ETC", "FIL", "ICP", "INJ", "AAVE", "MKR", "XMR",
         "ALGO", "VET", "SEI", "TIA", "RNDR", "FET", "WIF", "BONK", "HYPE", "ONDO", "ENA", "TAO"}

NAMES = {a.symbol: a.name for a in INDICES + FUTURES + MACRO}
NAMES.update({"^NDX": "Nasdaq 100", "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana",
              "XRP-USD": "XRP", "BNB-USD": "BNB", "DOGE-USD": "Dogecoin", "ADA-USD": "Cardano",
              "AVAX-USD": "Avalanche", "LINK-USD": "Chainlink", "TRX-USD": "TRON", "SUI20947-USD": "Sui",
              "TON11419-USD": "Toncoin", "LTC-USD": "Litecoin", "DOT-USD": "Polkadot", "SI=F": "Silver",
              "BZ=F": "Brent crude", "NG=F": "Natural gas", "HG=F": "Copper"})
NAMES.update(SECTORS)

_SYMBOL = re.compile(r"^[\^A-Z0-9][A-Z0-9.\-=^]{0,19}$")


def normalize(text: str) -> str:
    """Turns what someone typed ("btc", "s&p", "aapl", "$nvda") into a Yahoo symbol, without asking Yahoo."""
    raw = text.strip().lstrip("$")
    low = raw.lower()
    if low in ALIASES:
        return ALIASES[low]
    up = raw.upper().replace(" ", "")
    if up in COINS:
        return f"{up}-USD"
    if up.endswith("USDT") and up[:-4] in COINS:
        return f"{up[:-4]}-USD"
    if up.endswith("/USD"):
        return up.replace("/", "-")
    return up


def looks_like_symbol(text: str) -> bool:
    return bool(_SYMBOL.match(text))


def market_of(symbol: str, quote_type: str | None = None) -> str:
    if quote_type:
        return CRYPTO if quote_type.upper() == "CRYPTOCURRENCY" else STOCKS
    return CRYPTO if symbol.endswith("-USD") and not symbol.startswith("^") else STOCKS


def display_name(symbol: str, fallback: str | None = None) -> str:
    return NAMES.get(symbol) or fallback or symbol


BOARD_LABELS = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow", "^RUT": "Russell 2k", "^VIX": "VIX",
                "ES=F": "S&P fut", "NQ=F": "Nasdaq fut", "YM=F": "Dow fut", "^TNX": "10Y yield", "^IRX": "3M yield",
                "DX-Y.NYB": "Dollar", "GC=F": "Gold", "CL=F": "Oil", "SI=F": "Silver", "HG=F": "Copper"}


def tag(symbol: str) -> str:
    """How a symbol is named in titles: tickers as-is, indices and futures by name."""
    return BOARD_LABELS.get(symbol) or (display_name(symbol) if symbol.startswith("^") or "=" in symbol else short(symbol))


def title_of(symbol: str, name: str | None = None) -> str:
    """"NVDA · NVIDIA Corporation", or just "S&P 500" for an index or future."""
    name = name or display_name(symbol)
    if symbol.startswith("^") or "=" in symbol or symbol == "DX-Y.NYB":
        return name
    return f"{short(symbol)} · {name}"


def short(symbol: str) -> str:
    """Symbol as shown in tight tables: BTC-USD -> BTC, SUI20947-USD -> SUI."""
    if symbol.endswith("-USD"):
        return re.sub(r"\d+$", "", symbol[:-4])
    return symbol
