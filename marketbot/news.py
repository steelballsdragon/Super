"""Reads a headline the way a trader would: what kind of event it is, whether it's good or bad news for risk
assets, which markets it should move, in which direction and by roughly how much.

Typical moves come from event studies (e.g. a CPI surprise moves the S&P 500 about 1% and the 10-year yield
about 8 basis points), are scaled to how volatile each market is right now, and then corrected by the bot's
own record: every call is checked against what the market actually did over the next day.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .feeds import Headline

# Markets a story can move: key -> (Yahoo symbol, name, unit). Yields move in basis points.
TARGETS = {
    "SPX": ("^GSPC", "S&P 500", "%"), "NDX": ("^IXIC", "Nasdaq", "%"), "RUT": ("^RUT", "Small caps", "%"),
    "BTC": ("BTC-USD", "Bitcoin", "%"), "ETH": ("ETH-USD", "Ethereum", "%"), "ALT": ("SOL-USD", "Altcoins", "%"),
    "GOLD": ("GC=F", "Gold", "%"), "OIL": ("CL=F", "Oil", "%"), "DXY": ("DX-Y.NYB", "US dollar", "%"),
    "UST10": ("^TNX", "10-yr yield", "bp"), "BANKS": ("XLF", "Bank stocks", "%"),
    "ENERGY": ("XLE", "Energy stocks", "%"), "SEMIS": ("SMH", "Chip stocks", "%"),
}

CRYPTO_CONTEXT = re.compile(
    r"\b(bitcoin|btc|crypto\w*|ether(eum)?|eth|tokens?|coins?|blockchain|defi|stablecoins?|solana|xrp|ripple|"
    r"binance|coinbase|kraken|okx|bybit|tether|usdt|usdc|altcoins?|memecoins?|web3|nft|ibit|dogecoin|cardano)\b", re.I)


@dataclass(frozen=True)
class Event:
    key: str
    name: str
    emoji: str
    pattern: str
    importance: int
    impacts: tuple  # (target, sign, typical move): sign is the move's direction when the news is good (+1)
    good: str = ""  # wording that makes it good news for risk assets (or, for oil/dollar events, "up")
    bad: str = ""
    good_phrase: str = ""  # whole phrases that settle it (they count three times a single word)
    bad_phrase: str = ""
    company: float = 0.0  # typical move of the company named in the story (0: not a company event)
    crypto: bool = False  # needs crypto wording in the story
    fixed: int = 0  # always good (+1) or bad (-1) news, whatever the wording
    priced_in: bool = False  # describes a move that already happened


EVENTS = [
    Event("fed", "Central bank / rates", "🏦",
          r"\b(fed|fomc|federal reserve|powell|rate (cut|hike|decision)s?|interest rates?|central bank|ecb|"
          r"bank of japan|boj|bank of england|monetary policy|fed minutes|fed chair)\b", 85,
          (("SPX", 1, 0.8), ("NDX", 1, 1.0), ("RUT", 1, 1.2), ("UST10", -1, 8), ("DXY", -1, 0.4),
           ("GOLD", 1, 0.8), ("BTC", 1, 2.5), ("ETH", 1, 3.0)),
          good=r"\b(cuts?|cutting|lower(s|ing)? rates|dovish|eas(e|es|ing)|slash\w*|pause[sd]?|signals? cuts?)\b",
          bad=r"\b(hikes?|hiking|rais\w* rates|hawkish|higher for longer|tighten\w*|no cuts?|delays? cuts?|"
              r"push(es)? back|fewer cuts|rate hike)\b",
          # "Weak jobs data cuts the odds of a hike" is dovish, though it says "hike".
          good_phrase=r"\b((cut|cool|dim|reduc|lower|eas|trim|slash|pare|fad|damp)\w* .{0,40}(hike|tightening)|"
                      r"(hike|tightening) (odds|bets|chances|expectations|fears|pricing) .{0,15}(plummet|fall|drop|fade|"
                      r"cool|eas|dim|slid|tumbl|sink|reced|shrink)\w*|relief on (the )?fed)",
          bad_phrase=r"\b((cool|dim|reduc|lower|trim|slash|pare|fad|damp|dash)\w* .{0,40}(rate cuts?|easing|cut (odds|bets|hopes))|"
                     r"(cut|easing) (odds|bets|chances|hopes|expectations) .{0,15}(plummet|fall|drop|fade|cool|eas|"
                     r"dim|slid|tumbl|sink|reced|shrink)\w*)"),
    Event("inflation", "Inflation data", "🔥",
          r"\b(cpi|inflation|pce|ppi|consumer prices?|producer prices?|price index|core prices)\b", 85,
          (("SPX", 1, 0.9), ("NDX", 1, 1.2), ("UST10", -1, 8), ("DXY", -1, 0.4), ("GOLD", 1, 0.7),
           ("BTC", 1, 2.5)),
          good=r"\b(cool\w*|eas(e|es|ed|ing)|slow\w*|soft\w*|below (expectations|forecasts?|estimates)|"
               r"lower than expected|ris\w* less than expected|moderat\w*|declin\w*|falls?|fell|drops?|dropped|"
               r"lowest)\b",
          bad=r"\b(hot(ter)?|accelerat\w*|jump\w*|surg\w*|spik\w*|sticky|above (expectations|forecasts?|estimates)|"
              r"higher than expected|ris\w* more than expected|highest|rebound\w*|re-?accelerat\w*|firm\w*)\b"),
    Event("jobs", "Jobs data", "👷",
          r"\b(jobs report|nonfarm|non-farm|payrolls?|unemployment|jobless claims|labou?r market|job openings|jolts|"
          r"hiring)\b", 75,
          (("SPX", 0, 0.6), ("UST10", 1, 7), ("DXY", 1, 0.4), ("GOLD", -1, 0.6), ("BTC", 0, 2.0)),
          good=r"\b(beat\w*|strong\w*|surg\w*|jump\w*|robust|solid|above (expectations|estimates)|more than expected|"
               r"unemployment (falls|drops|dips))\b",
          bad=r"\b(weak\w*|miss\w*|slow\w*|cool\w*|below (expectations|estimates)|fewer than expected|"
              r"unemployment (rises|jumps|climbs)|layoffs? rise)\b"),
    Event("growth", "Growth / recession", "📊",
          r"\b(gdp|recession|economic growth|retail sales|ism|pmi|consumer (confidence|sentiment|spending)|"
          r"slowdown|contraction|soft landing|hard landing|industrial production|housing starts)\b", 65,
          (("SPX", 1, 0.6), ("RUT", 1, 0.9), ("OIL", 1, 1.5), ("UST10", 1, 5), ("BTC", 1, 1.5)),
          good=r"\b(beat\w*|expand\w*|grow\w*|strong\w*|rebound\w*|better than expected|accelerat\w*|soft landing|"
               r"resilient|upbeat)\b",
          bad=r"\b(contract\w*|shrink\w*|recession|weak\w*|miss\w*|slump\w*|worse than expected|declin\w*|"
              r"hard landing|slowdown|downturn)\b"),
    Event("trade", "Tariffs / trade", "🚢",
          r"\b(tariffs?|trade (war|deal|talks|truce|tensions)|export controls?|import (duties|taxes)|"
          r"customs duties|protectionis\w*)\b", 75,
          (("SPX", 1, 1.0), ("NDX", 1, 1.3), ("SEMIS", 1, 2.0), ("GOLD", -1, 0.7), ("BTC", 1, 2.5)),
          good=r"\b(deal|truce|pause[sd]?|exempt\w*|lift\w*|eas(e|es|ing)|roll(s|ed)? back|cuts? tariffs|"
               r"agreement|delay\w*|suspend\w*|reduc\w*)\b",
          bad=r"\b(impos\w*|new tariffs|rais\w*|hik\w*|escalat\w*|retaliat\w*|threat\w*|ban\w*|restrict\w*|"
              r"slap\w*|steeper|sweeping)\b"),
    Event("geopolitics", "Geopolitics / conflict", "🌍",
          r"\b(war|invasion|invade[sd]?|missile (strikes?|attacks?|launch\w*)|airstrikes?|military (strikes?|action|"
          r"escalation)|ceasefire|troops|nuclear (test|threat|talks|deal)|hormuz|blockade|houthis?|drone attacks?|"
          r"shelling|martial law|coup)\b", 70,
          (("SPX", 1, 0.6), ("OIL", -1, 3.0), ("GOLD", -1, 1.0), ("BTC", 1, 2.0), ("DXY", -1, 0.3)),
          good=r"\b(ceasefire|peace|truce|de-?escalat\w*|talks|deal|withdraw\w*|calm\w*|reopen\w*)\b",
          bad=r"\b(attack\w*|strikes?|invad\w*|escalat\w*|missiles?|clash\w*|threat\w*|bomb\w*|blockade|seiz\w*|"
              r"clos(e|es|ed|ure))\b"),
    Event("oil", "Oil supply", "🛢️",
          r"\b(opec\+?|crude|oil prices?|brent|wti|oil output|oil production|barrels?|refiner\w*)\b", 60,
          (("OIL", 1, 3.0), ("ENERGY", 1, 1.5), ("SPX", -1, 0.3), ("UST10", 1, 3)),
          good=r"\b(cuts? (output|production)|curb\w*|disrupt\w*|outage\w*|surg\w*|jump\w*|rall\w*|tight\w*|"
               r"sanction\w*)\b",
          bad=r"\b(boost\w* (output|production)|increas\w* (output|production)|glut|plung\w*|slump\w*|surplus|"
              r"falls?|tumbl\w*|oversupply)\b"),
    Event("banks", "Banking stress", "🏚️",
          r"\b(bank (runs?|failures?|collapse)|banking (crisis|turmoil)|fdic|bailout|credit crunch|"
          r"liquidity crisis|contagion|regional banks?|private credit)\b", 80,
          (("BANKS", 1, 3.0), ("SPX", 1, 1.0), ("GOLD", -1, 1.0), ("UST10", 1, 10), ("BTC", 0, 3.0)),
          good=r"\b(rescu\w*|backstop\w*|stabiliz\w*|acquired by|deal|calm\w*|rebound\w*)\b",
          bad=r"\b(fail\w*|collaps\w*|runs?|crisis|default\w*|contagion|plung\w*|turmoil|losses)\b"),
    Event("yields", "Bonds / government debt", "📜",
          r"\b(treasury yields?|bond yields?|10-year|30-year|debt ceiling|government shutdown|credit rating|"
          r"deficit|bond (market|selloff|rout)|term premium|treasury auction)\b", 60,
          (("SPX", 1, 0.7), ("NDX", 1, 0.9), ("UST10", -1, 8), ("BTC", 1, 1.5)),
          good=r"\b(falls?|fell|drop\w*|eas\w*|averted|deal|resolv\w*|rall(y|ies)|declin\w*|(mov|head|edg|drift)\w* lower)\b",
          bad=r"\b(surg\w*|spik\w*|jump\w*|soar\w*|shutdown|downgrad\w*|default\w*|rout|selloff|sell-off|climb\w*|"
              r"(march|push|mov|head|edg|creep)\w* higher)\b"),
    Event("dollar", "US dollar", "💵", r"\b(dollar|greenback|dxy|dollar index)\b", 35,
          (("DXY", 1, 0.5), ("GOLD", -1, 0.6), ("BTC", -1, 1.0), ("SPX", -1, 0.2)),
          good=r"\b(strength\w*|jump\w*|surg\w*|rall\w*|gain\w*|climb\w*|rises?)\b",
          bad=r"\b(weak\w*|slump\w*|fall\w*|drop\w*|slid\w*|declin\w*|tumbl\w*)\b"),
    Event("ai", "AI / chips", "🤖",
          r"\b(ai chips?|semiconductors?|chipmakers?|gpus?|data cent(er|re)s?|openai|artificial intelligence|"
          r"hyperscalers?|ai spending|ai capex)\b", 55,
          (("SEMIS", 1, 1.8), ("NDX", 1, 0.6))),
    Event("earnings", "Earnings / guidance", "🧾",
          r"\b(earnings|quarterly (results|profit|revenue)|q[1-4] (results|earnings|sales)|revenue|eps|"
          r"profit|beats? (estimates|expectations)|miss(es)? (estimates|expectations)|guidance|outlook|"
          r"forecast)\b", 55, (("NDX", 1, 0.2),),
          good=r"\b(beat\w*|tops?|topped|exceed\w*|surpass\w*|record|rais\w* (its |full-year |annual )?"
               r"(guidance|forecast|outlook)|strong\w*|above (estimates|expectations)|upbeat|blowout|soar\w*)\b",
          bad=r"\b(miss\w*|falls? short|below (estimates|expectations)|cut\w* (its |full-year |annual )?"
              r"(guidance|forecast|outlook)|weak\w*|disappoint\w*|warn\w*|lower\w* (guidance|forecast|outlook)|"
              r"slump\w*|plung\w*)\b",
          company=6.0),
    Event("deal", "Merger / acquisition", "🤝",
          r"\b(acquir\w*|acquisition|merger|to buy|takeover|buyout|bid for|deal to (buy|purchase)|"
          r"agrees to buy|in talks to buy)\b", 55, (), company=5.0,
          good=r"\b(agree\w*|to buy|approv\w*|premium|sweeten\w*)\b",
          bad=r"\b(block\w*|scrap\w*|collaps\w*|terminat\w*|reject\w*|walk\w* away)\b"),
    Event("analyst", "Analyst rating", "🎯",
          r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiates? coverage|overweight|underweight|"
          r"outperform rating|buy rating|sell rating)\b", 35, (), company=2.0,
          good=r"\b(upgrad\w*|rais\w* (its |the )?(price )?target|overweight|outperform|buy rating|bullish)\b",
          bad=r"\b(downgrad\w*|cut\w* (its |the )?(price )?target|lower\w* (its |the )?(price )?target|"
              r"underweight|underperform|sell rating|bearish)\b"),
    Event("legal", "Legal / regulatory", "⚖️",
          r"\b(lawsuit|sued|sues|probe|investigation|antitrust|fined?|settles? (with|charges|claims|lawsuits?|"
          r"cases?|probes?)|settlement|doj|ftc|sec charges|indict\w*|class action|recall\w*)\b", 50, (), company=3.0,
          good=r"\b(settle\w*|dismiss\w*|cleared|wins?|won|drop\w* (the )?(case|probe|lawsuit))\b",
          bad=r"\b(sue[sd]?|probe|investigat\w*|fine[sd]?|charg\w*|block\w*|indict\w*|recall\w*|class action)\b"),
    Event("fda", "Drug approval / trial", "💊",
          r"\b(fda|clinical trial|phase (1|2|3|i|ii|iii)|drug approval|biologics license|breakthrough therapy)\b",
          50, (), company=8.0,
          good=r"\b(approv\w*|success\w*|positive|met (its )?(primary )?endpoint|breakthrough|clears?)\b",
          bad=r"\b(reject\w*|fail\w*|halt\w*|negative|delay\w*|crl|complete response letter|missed)\b"),
    Event("bankruptcy", "Bankruptcy", "💀", r"\b(bankruptcy|chapter 11|insolven\w*|default(s|ed)? on)\b", 70,
          (), company=25.0, fixed=-1),
    Event("capital", "Buyback / dividend / split", "💰",
          r"\b(buybacks?|share repurchases?|dividend (hike|increase|raise)|raises? (its )?dividend|stock split|"
          r"special dividend)\b", 35, (), company=2.0, fixed=1),
    Event("index", "Index inclusion", "📥",
          r"\b(join(s|ing)? the s&p 500|added to (the )?s&p 500|s&p 500 inclusion|index inclusion|"
          r"removed from the s&p)\b", 45, (), company=5.0,
          good=r"\b(join|added|inclusion)\b", bad=r"\bremoved\b"),
    Event("layoffs", "Layoffs / restructuring", "✂️",
          r"\b(layoffs?|job cuts|cut(s|ting)? (\d[\d,]* )?jobs|workforce reduction|restructuring)\b", 35, (),
          company=1.5),
    Event("crypto_etf", "Crypto ETFs", "🧺",
          r"\b(etfs?|etf (inflows?|outflows?|flows?)|ibit|spot (bitcoin|ether|ethereum|solana|xrp) etf)\b", 70,
          (("BTC", 1, 3.0), ("ETH", 1, 4.0), ("ALT", 1, 5.0)), crypto=True,
          good=r"\b(approv\w*|inflows?|record|launch\w*|files? for|green ?light|bought|buy\w*)\b",
          bad=r"\b(reject\w*|delay\w*|outflows?|den(y|ies|ied)|redemptions?|withdraw\w*)\b"),
    Event("crypto_rules", "Crypto regulation", "🏛️",
          r"\b(sec|cftc|regulat\w*|crypto (bill|law|rules)|stablecoin (bill|law)|clarity act|genius act|"
          r"market structure bill|crackdown|mica|enforcement)\b", 70,
          (("BTC", 1, 2.5), ("ETH", 1, 3.5), ("ALT", 1, 6.0)), crypto=True,
          good=r"\b(approv\w*|pass(es|ed)?|clarity|framework|dismiss\w*|drop\w* (the )?(case|lawsuit|probe)|"
               r"pro-crypto|wins?|sign\w* into law|ends? (the )?(case|lawsuit))\b",
          bad=r"\b(sue[sd]?|charg\w*|ban\w*|crackdown|reject\w*|enforcement action|restrict\w*|subpoena\w*)\b"),
    Event("crypto_hack", "Hack / exploit", "🏴‍☠️",
          r"\b(hack(ed|ers?)?|exploit(ed)?|drain(ed)?|stolen|breach\w*|rug ?pull|security incident)\b", 65,
          (("ALT", 1, 4.0), ("ETH", 1, 1.5), ("BTC", 1, 1.0)), crypto=True, fixed=-1),
    Event("crypto_collapse", "Exchange / lender failure", "🧨",
          r"\b(halts? withdrawals|freez\w* withdrawals|paus\w* withdrawals|insolven\w*|exchange collapse|"
          r"files? for bankruptcy)\b", 90, (("BTC", 1, 7.0), ("ETH", 1, 9.0), ("ALT", 1, 12.0)), crypto=True,
          fixed=-1),
    Event("stablecoin", "Stablecoins", "🪙", r"\b(stablecoins?|depeg\w*|de-peg\w*|loses? (its )?peg|tether|usdc)\b",
          55, (("BTC", 1, 2.0), ("ETH", 1, 2.5)), crypto=True,
          good=r"\b(mint\w*|record (supply|market cap)|inflows?|approv\w*|launch\w*)\b",
          bad=r"\b(depeg\w*|de-peg\w*|lose[s]? (its )?peg|redemptions?|freez\w*|collaps\w*)\b"),
    Event("adoption", "Adoption / treasury buying", "🏦",
          r"\b(strategic (bitcoin |crypto )?reserve|(buys|adds|bought|acquires|acquired) [\d,.]+ (btc|bitcoin|eth)|"
          r"bitcoin treasury|treasury company|adds? bitcoin to (its )?balance sheet|legal tender|"
          r"strategy (buys|adds)|microstrategy)\b", 55, (("BTC", 1, 2.0), ("ETH", 1, 1.5)), crypto=True,
          good=r"\b(buy\w*|bought|adds?|acquir\w*|adopt\w*|reserve|purchas\w*)\b",
          bad=r"\b(sell\w*|sold|dump\w*|liquidat\w*)\b"),
    Event("crypto_tech", "Network upgrade", "🛠️",
          r"\b(halving|hard fork|network upgrade|mainnet|testnet|pectra|fusaka|dencun|firedancer)\b", 40,
          (("ETH", 1, 2.0), ("ALT", 1, 3.0)), crypto=True,
          good=r"\b(launch\w*|live|success\w*|complet\w*|activat\w*)\b", bad=r"\b(delay\w*|bug|halt\w*|fail\w*)\b"),
    Event("crypto_move", "Crypto price action", "📈",
          r"\b(liquidat\w*|(bitcoin|btc|ether|crypto)\w* (falls?|drops?|plunges?|tumbles?|surges?|jumps?|rall\w*|"
          r"soars?|hits?|climbs?|slides?|sinks?|crash\w*))\b", 40, (("BTC", 1, 1.5), ("ETH", 1, 2.0)),
          crypto=True, priced_in=True),
    Event("stock_move", "Market moves", "📉",
          r"\b(stocks?|wall street|s&p 500|nasdaq|dow|futures|equities)\b.{0,40}\b(rall\w*|surg\w*|jump\w*|"
          r"soar\w*|tumbl\w*|plung\w*|slid\w*|slump\w*|falls?|fell|drops?|sinks?|record|rebound\w*)\b", 30,
          (("SPX", 1, 0.5), ("NDX", 1, 0.6)), priced_in=True),
]
EVENT_BY_KEY = {e.key: e for e in EVENTS}

POSITIVE = re.compile(
    r"\b(surg\w*|soar\w*|jump\w*|rall(y|ies|ied)|gain\w*|ris(e|es|ing)|climb\w*|beat\w*|tops?|exceed\w*|record|"
    r"strong\w*|upgrad\w*|boost\w*|approv\w*|wins?|won|breakthrough|bullish|rebound\w*|recover\w*|optimis\w*|"
    r"outperform\w*|expand\w*|growth|profit\w*|upbeat|inflows?|all-time high|best|deal|eases|relief)\b", re.I)
NEGATIVE = re.compile(
    r"\b(plung\w*|tumbl\w*|slump\w*|crash\w*|sink\w*|sank|drop\w*|fall(s|ing)?|fell|slid\w*|miss\w*|"
    r"downgrad\w*|weak\w*|warn\w*|lawsuit|probe|investigat\w*|fraud|hack\w*|exploit\w*|bankrupt\w*|default\w*|"
    r"recession|selloff|sell-off|bearish|fears?|worr\w*|concern\w*|declin\w*|loss(es)?|outflows?|ban\w*|"
    r"crackdown|sanction\w*|turmoil|slowdown|cut\w*|worst|collaps\w*|panic|rout|risk-off)\b", re.I)
STRONG = re.compile(r"\b(record|biggest|largest|worst|best|historic\w*|massive|plung\w*|soar\w*|crash\w*|surg\w*|"
                    r"unprecedented|since (19|20)\d\d|all-time|emergency|shock\w*|stun\w*|skyrocket\w*|"
                    r"collaps\w*|panic|bloodbath|meltdown|tumbl\w*)\b", re.I)
HEDGED = re.compile(r"\b(may|might|could|reportedly|considers?|weighs?|mulls?|plans?|expected to|sources say|"
                    r"rumou?rs?|poised|eyes|seeks?|proposes?|would|if|preview|what to (watch|expect)|ahead of)\b",
                    re.I)
NOT = re.compile(r"\b(no|not|never|without|fails? to|unlikely)\b", re.I)
OPINION = re.compile(r"(\?$|^(is|are|should|why|how|what|here'?s|opinion|prediction)\b|"
                     r"\bhere(\s+(are|is)|'s|’s) (\d+|the|why|what|how|a)\b|\b(i'?d|i'?m|i am|i'll|my)\b|"
                     r"\b\d+ (top |great |best )?(stocks|reasons|things|ways|etfs|picks)\b|"
                     r"\b(buy now|to buy|to watch|best stocks|my top|i'?m buying|millionaire|retire\w*|"
                     r"motley fool|zacks|opinion)\b)", re.I)
# Stories mostly about another economy (a Thai CPI print, Indian shares) matter less to US-listed markets.
FOREIGN = re.compile(r"\b(thai\w*|turk\w*|india\w*|australia\w*|japan\w*|nikkei|uk|britain|british|ftse|euro ?zone|"
                     r"europe\w*|german\w*|france|french|ital\w*|spain|spanish|canad\w*|brazil\w*|mexic\w*|korea\w*|"
                     r"indonesia\w*|south africa\w*|argentin\w*|philippin\w*|vietnam\w*|malaysia\w*|singapore\w*|"
                     r"hong kong|hang seng|new zealand|swiss|switzerland|swed\w*|norw\w*|pol(and|ish)|asian|"
                     r"chin(a|ese)|ecb|boj|bank of (england|japan|canada|korea)|rba|rbi|snb|pboc|sensex|nifty|asx)\b",
                     re.I)
AMERICAN = re.compile(r"\b(u\.?s\.?|united states|americ\w*|fed|federal reserve|fomc|wall street|s&p|nasdaq|dow|"
                      r"treasur\w*|white house|congress|trump|washington|powell|nyse)\b", re.I)
# Recaps of moves that already happened ("Nasdaq closes at a record").
WRAP = re.compile(r"\b(stocks?|shares|s&p( 500)?|nasdaq|dow|futures|wall street|equities|markets?|bitcoin|crypto)\b"
                  r".{0,60}\b(clos(e|es|ed|ing)|end(s|ed)?|finish\w*|on track|session|midday|trade[sd]? (higher|lower|"
                  r"mixed)|open(s|ed)? (higher|lower)|hover\w*|edg(e|es|ed) (up|down|higher|lower)|slip\w*|mixed|"
                  r"record (close|high)s?|rejected|under pressure|hits? (a |an )?(new |fresh )?(record|all-time)\w*|"
                  r"steady|stall\w*)\b|\bstock market (news|today)\b|"
                  r"\bmarkets? (wrap|today)\b", re.I)
NOISE = re.compile(r"\b(approval of (an )?application|application by|enforcement action|termination of enforcement|"
                   r"request(s)? for (public )?comment|announces? (the )?(appointment|retirement)|discount rate meeting|"
                   r"board meeting|statement on|minutes of the board|live updates?|morning (risk )?report|podcast|"
                   r"newsletter|daily briefing|week ahead|what to watch)\b", re.I)

COMPANIES = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "tesla": "TSLA", "amazon": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL", "meta": "META", "facebook": "META", "broadcom": "AVGO",
    "jpmorgan": "JPM", "jp morgan": "JPM", "berkshire": "BRK-B", "eli lilly": "LLY",
    "exxon": "XOM", "walmart": "WMT", "amd": "AMD", "netflix": "NFLX", "palantir": "PLTR", "costco": "COST",
    "intel": "INTC", "boeing": "BA", "oracle": "ORCL", "salesforce": "CRM", "coinbase": "COIN",
    "microstrategy": "MSTR", "micron": "MU", "tsmc": "TSM", "taiwan semiconductor": "TSM", "disney": "DIS",
    "pfizer": "PFE", "goldman": "GS", "morgan stanley": "MS", "bank of america": "BAC", "citigroup": "C",
    "wells fargo": "WFC", "chevron": "CVX", "mcdonald's": "MCD", "starbucks": "SBUX", "nike": "NKE",
    "uber": "UBER", "airbnb": "ABNB", "shopify": "SHOP", "adobe": "ADBE", "qualcomm": "QCOM", "arm holdings": "ARM",
    "super micro": "SMCI", "unitedhealth": "UNH", "johnson & johnson": "JNJ", "procter": "PG", "coca-cola": "KO",
    "pepsico": "PEP", "home depot": "HD", "caterpillar": "CAT", "ibm": "IBM", "cisco": "CSCO", "paypal": "PYPL",
    "robinhood": "HOOD", "gamestop": "GME", "rivian": "RIVN", "general motors": "GM",
    "novo nordisk": "NVO", "moderna": "MRNA", "merck": "MRK", "abbvie": "ABBV", "blackrock": "BLK",
    "riot platforms": "RIOT", "snowflake": "SNOW",
}
COINS = {"bitcoin": "BTC-USD", "btc": "BTC-USD", "ether": "ETH-USD", "ethereum": "ETH-USD", "solana": "SOL-USD",
         "xrp": "XRP-USD", "ripple": "XRP-USD", "dogecoin": "DOGE-USD", "cardano": "ADA-USD", "bnb": "BNB-USD",
         "chainlink": "LINK-USD", "avalanche": "AVAX-USD", "litecoin": "LTC-USD", "tron": "TRX-USD",
         "polkadot": "DOT-USD", "toncoin": "TON11419-USD"}
TICKER = re.compile(r"(?:\$|\b(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX)\s*:\s*)([A-Z]{1,5}(?:[.-][A-Z])?)\b")
_NAME = re.compile(r"\b(" + "|".join(sorted((re.escape(k) for k in list(COMPANIES) + list(COINS)), key=len,
                                            reverse=True)) + r")\b", re.I)


@dataclass
class Impact:
    target: str  # a TARGETS key, or "TICKER:XYZ" for a company
    symbol: str
    name: str
    direction: int  # +1 up, -1 down, 0 = big move either way
    low: float
    high: float
    unit: str  # "%" or "bp"
    typical: float  # the unscaled typical move (for grading)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


@dataclass
class Analysis:
    headline: Headline
    events: list[Event]
    polarity: int  # +1 good news for risk assets, -1 bad, 0 unclear
    sentiment: float  # -1..1 from the wording alone
    intensity: float
    importance: int  # 0-100
    impacts: list[Impact]
    confidence: str  # High / Medium / Low
    market: str  # stocks, crypto or macro
    tickers: list[str] = field(default_factory=list)
    hedged: bool = False
    opinion: bool = False
    priced_in: bool = False  # describes a move that already happened
    note: str = ""  # one-line takeaway (from the AI reader when enabled)
    source: str = "rules"

    @property
    def emoji(self) -> str:
        return self.events[0].emoji if self.events else "📰"

    @property
    def kind(self) -> str:
        return " · ".join(e.name for e in self.events[:2]) if self.events else "General"


def _score(pattern: str, text: str) -> int:
    """How many matches of the wording, ignoring ones right after a negation."""
    if not pattern:
        return 0
    count = 0
    for m in re.finditer(pattern, text, re.I):
        # A negation counts only in the same clause, within the three words before ("does not cool").
        clause = re.split(r"[,.;:!?—–]", text[max(0, m.start() - 40): m.start()])[-1]
        count += -1 if NOT.search(" ".join(clause.split()[-3:])) else 1
    return count


def sentiment(text: str) -> float:
    pos = len(POSITIVE.findall(text))
    neg = len(NEGATIVE.findall(text))
    return 0.0 if pos + neg == 0 else (pos - neg) / (pos + neg)


def tickers_in(h: Headline) -> list[str]:
    found = list(h.tickers)
    found += TICKER.findall(f"{h.title} {h.summary}")
    for m in _NAME.finditer(h.title):
        name = m.group(1).lower()
        found.append(COMPANIES.get(name) or COINS[name])
    return list(dict.fromkeys(t for t in found if t))


def analyse(h: Headline, vol_ratio: dict[str, float] | None = None, calibration=None,
            watched: set[str] | None = None, now: float | None = None) -> Analysis:
    """Classifies a headline and estimates its market impact."""
    now = now or time.time()
    title = h.title
    text = f"{h.title}. {h.summary}"
    is_crypto = bool(CRYPTO_CONTEXT.search(text))
    events = [e for e in EVENTS if re.search(e.pattern, title, re.I) and (not e.crypto or is_crypto)]
    if not events:  # the summary counts for less than the headline
        events = [e for e in EVENTS if re.search(e.pattern, h.summary, re.I) and (not e.crypto or is_crypto)
                  and not e.priced_in]
    # Stories that only describe a move rank below stories that cause one.
    if any(not e.priced_in for e in events):
        events = [e for e in events if not e.priced_in]
    events.sort(key=lambda e: -e.importance)
    events = events[:3]
    sent = sentiment(title) * 0.7 + sentiment(h.summary) * 0.3 if h.summary else sentiment(title)
    hedged = bool(HEDGED.search(title))
    opinion = bool(OPINION.search(title.strip()))
    strong = len(STRONG.findall(title))
    intensity = min(1.0 + 0.25 * strong, 1.6) * (0.7 if hedged else 1.0)
    tickers = tickers_in(h)

    polarity = 0
    specific = False
    for e in events:
        if e.fixed:
            polarity, specific = e.fixed, True
            break
        g = _score(e.good, text) + 3 * _score(e.good_phrase, text)
        b = _score(e.bad, text) + 3 * _score(e.bad_phrase, text)
        if g != b:
            polarity, specific = (1 if g > b else -1), True
            break
    if polarity == 0 and abs(sent) >= 0.34:
        polarity = 1 if sent > 0 else -1

    vol_ratio = vol_ratio or {}
    impacts: list[Impact] = []
    lead = events[0] if events else None
    for e in events:
        if e.priced_in and lead is not e:
            continue
        for target, sign, typical in e.impacts:
            if any(i.target == target for i in impacts):
                continue
            symbol, name, unit = TARGETS[target]
            direction = sign * polarity
            impacts.append(_impact(target, symbol, name, unit, direction, typical, intensity, e.key,
                                   vol_ratio.get(symbol, 1.0), calibration))
        if e.company:
            for t in tickers[:2]:
                key = f"TICKER:{t}"
                if any(i.target == key for i in impacts):
                    continue
                impacts.insert(0, _impact(key, t, t, "%", polarity, e.company, intensity, e.key,
                                          vol_ratio.get(t, 1.0), calibration))
    if not events and tickers and abs(sent) >= 0.34:  # company news with no recognised event
        for t in tickers[:2]:
            impacts.append(_impact(f"TICKER:{t}", t, t, "%", polarity, 2.0, intensity * 0.7, "general", 1.0,
                                   calibration))

    if FOREIGN.search(title) and not AMERICAN.search(title) and not is_crypto:
        for i in impacts:
            if not i.target.startswith("TICKER:"):
                i.low, i.high = round(i.low * 0.35, 2), round(i.high * 0.35, 2)
    market = h.market
    if is_crypto and (not events or any(e.crypto for e in events)):
        market = "crypto"
    elif events and events[0].key in ("fed", "inflation", "jobs", "growth", "yields", "dollar", "trade"):
        market = "macro"
    importance = 20.0
    if events:
        importance = max(e.importance for e in events) * (0.75 + 0.25 * min(intensity, 1.4))
        if events[0].priced_in:
            importance *= 0.8
    from_summary = bool(events) and not any(re.search(e.pattern, title, re.I) for e in events)
    foreign = bool(FOREIGN.search(title)) and not AMERICAN.search(title) and market != "crypto"
    wrap = bool(WRAP.search(title))
    if from_summary:
        importance *= 0.6
    if foreign:
        importance *= 0.4
    if wrap:
        importance *= 0.5
    if NOISE.search(title):
        importance *= 0.25
    importance *= h.weight * min(1 + 0.08 * len(h.also), 1.3)
    if hedged:
        importance *= 0.85
    if opinion:
        importance *= 0.45  # "Is X a buy?" pieces aren't news
    if watched and any(t in watched for t in tickers):
        importance *= 1.15
    age_h = max(now - h.published, 0) / 3600
    if age_h > 6:
        importance *= 0.8
    if events and lead.company and not tickers:
        importance *= 0.6  # a company event without a company we can name
    if polarity == 0:
        importance *= 0.75  # can't tell which way it cuts
    confidence = "Low"
    if specific and events and lead.importance >= 70 and not (hedged or opinion or foreign or wrap or from_summary):
        confidence = "High"
    elif specific or (polarity and abs(sent) >= 0.6):
        confidence = "Medium"
    return Analysis(h, events, polarity, round(sent, 2), round(intensity, 2), int(min(importance, 100)),
                    impacts[:6], confidence, market, tickers, hedged, opinion, priced_in=wrap or (
                        bool(events) and events[0].priced_in))


def _impact(target, symbol, name, unit, direction, typical, intensity, event_key, vol_ratio, calibration) -> Impact:
    scale = intensity * min(max(vol_ratio, 0.6), 2.0)
    if calibration is not None:
        scale *= calibration(event_key, target)
    mid = typical * scale
    return Impact(target, symbol, name, direction, round(mid * 0.5, 2), round(mid * 1.5, 2), unit, typical)


def impact_text(i: Impact) -> str:
    arrow = "🟢 ▲" if i.direction > 0 else "🔴 ▼" if i.direction < 0 else "🟡 ⇅"
    if i.unit == "bp":
        rng = f"{i.low:.0f}–{i.high:.0f} bp"
    else:
        rng = f"{i.low:.1f}–{i.high:.1f}%"
    sign = "+" if i.direction > 0 else "−" if i.direction < 0 else "±"
    return f"{arrow} **{i.name}** {sign}{rng}"


# ----- learning from outcomes -----

GRADE_AFTER = 24 * 3600
PRIOR = 6  # a typical move's worth of evidence for "the textbook size is right"


class ImpactBook:
    """Remembers each posted call and, a day later, compares it with what the market did. The comparison
    corrects future size estimates per event and market, and feeds the hit rate shown by /record."""

    def __init__(self, store):
        self.store = store  # a StateStore

    def record(self, analysis: Analysis, prices: dict[str, float], now: float | None = None) -> None:
        now = now or time.time()
        for i in analysis.impacts[:4]:
            if i.direction == 0 or i.symbol not in prices or analysis.opinion:
                continue
            key = f"{analysis.headline.id}:{i.target}"
            if self.store.get("news_pending", key):
                continue
            self.store.set("news_pending", key, {
                "event": analysis.events[0].key if analysis.events else "general", "target": i.target,
                "symbol": i.symbol, "unit": i.unit, "dir": i.direction, "pred": i.mid, "typical": i.typical,
                "price": prices[i.symbol], "at": now, "title": analysis.headline.title[:140]})

    def due(self, now: float | None = None) -> list[tuple[str, dict]]:
        now = now or time.time()
        return [(k, v) for k, v in self.store.items("news_pending") if now - v["at"] >= GRADE_AFTER]

    def symbols_due(self, now: float | None = None) -> set[str]:
        return {v["symbol"] for _, v in self.due(now)}

    def grade(self, prices: dict[str, float], now: float | None = None) -> int:
        graded = 0
        with self.store.batch():
            for key, p in self.due(now):
                price = prices.get(p["symbol"])
                if price is None:
                    if (now or time.time()) - p["at"] > 4 * GRADE_AFTER:
                        self.store.delete("news_pending", key)  # never priced: give up
                    continue
                if p["unit"] == "bp":
                    actual = (price - p["price"]) * 100  # yields are quoted in percent
                else:
                    actual = (price / p["price"] - 1) * 100
                stat_key = f"{p['event']}|{p['target'].split(':')[0]}"
                s = self.store.get("news_stats", stat_key) or {"n": 0, "hits": 0, "actual": 0.0, "pred": 0.0}
                s["n"] += 1
                s["hits"] += int((actual > 0) == (p["dir"] > 0) and actual != 0)
                s["actual"] += abs(actual)
                s["pred"] += abs(p["pred"])
                self.store.set("news_stats", stat_key, s)
                self.store.delete("news_pending", key)
                graded += 1
        return graded

    def calibration(self, event: str, target: str) -> float:
        s = self.store.get("news_stats", f"{event}|{target.split(':')[0]}")
        if not s or not s["n"]:
            return 1.0
        typical = s["pred"] / s["n"]
        ratio = (s["actual"] + PRIOR * typical) / (s["pred"] + PRIOR * typical)
        return float(min(max(ratio, 0.4), 2.5))

    def summary(self) -> dict:
        stats = self.store.items("news_stats")
        n = sum(s["n"] for _, s in stats)
        hits = sum(s["hits"] for _, s in stats)
        by_event: dict[str, list[int]] = {}
        for key, s in stats:
            e = key.split("|")[0]
            by_event.setdefault(e, [0, 0])
            by_event[e][0] += s["n"]
            by_event[e][1] += s["hits"]
        pending = len(self.store.items("news_pending"))
        return {"n": n, "hits": hits, "by_event": by_event, "pending": pending}
