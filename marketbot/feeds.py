"""Market news from free RSS feeds (MarketWatch, WSJ, FT, Nasdaq, Investing.com, Seeking Alpha, the Fed, the SEC,
CoinDesk, Cointelegraph, The Block, Decrypt, Google News) and Yahoo's per-ticker news."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp

from .yahoo import HEADERS, YahooClient

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 3_000_000
MAX_AGE = 36 * 3600  # older stories are ignored


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    market: str  # "stocks", "crypto" or "macro": where its stories usually matter
    weight: float = 1.0  # how much its stories count when ranking importance


GOOGLE = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
FEEDS = [
    Feed("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "stocks"),
    Feed("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "stocks", 1.1),
    Feed("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_bulletins", "stocks", 1.3),
    Feed("WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "stocks", 1.2),
    Feed("Nasdaq", "https://www.nasdaq.com/feed/rssoutbound?category=Markets", "stocks"),
    Feed("Investing.com", "https://www.investing.com/rss/news_25.rss", "stocks"),
    Feed("Investing.com", "https://www.investing.com/rss/news_95.rss", "macro"),
    Feed("Seeking Alpha", "https://seekingalpha.com/market_currents.xml", "stocks"),
    Feed("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "macro", 1.6),
    Feed("SEC", "https://www.sec.gov/news/pressreleases.rss", "stocks", 1.1),
    Feed("Google News", GOOGLE + "stock+market+OR+S%26P+500+OR+Nasdaq+OR+Dow+when:1d", "stocks", 0.9),
    Feed("Google News", GOOGLE + "Fed+OR+inflation+OR+CPI+OR+jobs+report+OR+tariffs+when:1d", "macro", 0.9),
    Feed("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto", 1.1),
    Feed("Cointelegraph", "https://cointelegraph.com/rss", "crypto"),
    Feed("The Block", "https://www.theblock.co/rss.xml", "crypto", 1.1),
    Feed("Decrypt", "https://decrypt.co/feed", "crypto"),
    Feed("Investing.com", "https://www.investing.com/rss/news_301.rss", "crypto"),
    Feed("Google News", GOOGLE + "bitcoin+OR+crypto+OR+ethereum+when:1d", "crypto", 0.9),
]


@dataclass
class Headline:
    id: str
    title: str
    summary: str
    link: str
    source: str
    published: float
    market: str
    weight: float = 1.0
    tickers: tuple[str, ...] = ()
    also: list[str] = field(default_factory=list)  # other outlets carrying the same story

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}"


_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")


def clean(text: str | None, limit: int = 600) -> str:
    text = html.unescape(_TAGS.sub(" ", html.unescape(text or "")))
    return _SPACE.sub(" ", text).strip()[:limit]


def headline_id(title: str) -> str:
    words = " ".join(_WORD.findall(title.lower()))
    return hashlib.sha1(words.encode()).hexdigest()[:16]


def _when(text: str | None) -> float | None:
    if not text:
        return None
    text = text.strip()
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml: str, feed: Feed, now: float | None = None) -> list[Headline]:
    """Items from an RSS 2.0 or Atom feed. Google News titles end in " - Outlet", which becomes the source."""
    now = now or time.time()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in item:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href"))
            elif child.text and name not in fields:
                fields[name] = child.text
        title = clean(fields.get("title"), 300)
        if not title:
            continue
        source = feed.name
        if feed.url.startswith(GOOGLE) and " - " in title:
            title, source = title.rsplit(" - ", 1)
        published = (_when(fields.get("pubDate")) or _when(fields.get("published")) or _when(fields.get("updated"))
                     or _when(fields.get("date")) or now)
        if now - published > MAX_AGE:
            continue
        summary = clean(fields.get("description") or fields.get("summary") or fields.get("encoded"))
        if summary.lower().startswith(title.lower()[:40]):
            summary = ""  # Google News repeats the title as the description
        out.append(Headline(headline_id(title), title, summary, (fields.get("link") or "").strip(), source,
                            min(published, now), feed.market, feed.weight))
    return out


def from_yahoo(items: list[dict], market: str) -> list[Headline]:
    out = []
    for n in items:
        title = clean(n.get("title"), 300)
        if not title or n.get("type") not in (None, "STORY", "VIDEO"):
            continue
        out.append(Headline(headline_id(title), title, "", n.get("link", ""), n.get("publisher") or "Yahoo Finance",
                            float(n.get("providerPublishTime") or time.time()), market, 1.0,
                            tuple(n.get("relatedTickers") or ())))
    return out


class NewsFetcher:
    def __init__(self, yahoo: YahooClient, session: aiohttp.ClientSession | None = None, feeds=FEEDS):
        self.yahoo = yahoo
        self.feeds = list(feeds)
        self._session = session
        self.failures: dict[str, str] = {}

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=25))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _feed(self, feed: Feed) -> list[Headline]:
        try:
            session = await self.session()
            async with session.get(feed.url, allow_redirects=True) as resp:
                resp.raise_for_status()
                body = await resp.content.read(MAX_FEED_BYTES)
            self.failures.pop(feed.url, None)
            return parse_feed(body.decode("utf-8", errors="replace"), feed)
        except Exception as exc:
            self.failures[feed.url] = f"{type(exc).__name__}: {exc}"[:200]
            log.debug("News feed %s failed: %r", feed.url, exc)
            return []

    async def latest(self, markets: set[str] | None = None) -> list[Headline]:
        """Every feed's recent stories (newest first), one copy per story."""
        feeds = [f for f in self.feeds if markets is None or f.market in markets
                 or (f.market == "macro" and "stocks" in markets)]
        found = await asyncio.gather(*(self._feed(f) for f in feeds))
        return dedupe([h for batch in found for h in batch])

    async def for_symbol(self, symbol: str, market: str, count: int = 12) -> list[Headline]:
        try:
            _, news = await self.yahoo.search(symbol, news=count, quotes=0)
        except Exception:
            log.warning("Yahoo news for %s failed", symbol, exc_info=True)
            return []
        return dedupe(from_yahoo(news, market))


def _tokens(title: str) -> set[str]:
    return {w for w in _WORD.findall(title.lower()) if len(w) > 2}


def similar(a: str, b: str, threshold: float = 0.55) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def dedupe(items: list[Headline]) -> list[Headline]:
    """Newest first; the same story from several outlets becomes one item that lists the others."""
    out: list[Headline] = []
    for h in sorted(items, key=lambda h: h.published, reverse=True):
        twin = next((o for o in out if o.id == h.id or similar(o.title, h.title)), None)
        if twin is None:
            out.append(h)
        else:
            if h.source != twin.source and h.source not in twin.also:
                twin.also.append(h.source)
            twin.tickers = tuple(dict.fromkeys(twin.tickers + h.tickers))
            twin.weight = max(twin.weight, h.weight)
            if not twin.summary and h.summary:
                twin.summary = h.summary
    return out
