import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from marketbot import ai  # noqa: E402
from marketbot.feeds import FEEDS, Feed, Headline, dedupe, from_yahoo, parse_feed, similar  # noqa: E402
from marketbot.news import ImpactBook, analyse, impact_text  # noqa: E402
from marketbot.storage import StateStore  # noqa: E402


def h(title, summary="", market="stocks", tickers=(), published=None):
    return Headline("id-" + title[:20], title, summary, "https://x.test/a", "Test", published or time.time(), market, 1.0,
                    tuple(tickers))


def impacts(a):
    return {i.target: i.direction for i in a.impacts}


@pytest.mark.parametrize("title, target, direction", [
    ("US CPI rises more than expected in September, core inflation accelerates", "SPX", -1),
    ("Inflation cools more than expected in September", "SPX", 1),
    ("Fed cuts rates by 25 basis points, signals more easing ahead", "SPX", 1),
    ("Powell says Fed in no hurry to cut rates as inflation stays sticky", "SPX", -1),
    ("Weak jobs data cuts odds of an October Fed rate hike", "SPX", 1),
    ("Rate cut bets fade after strong retail sales", "SPX", -1),
    ("Trump announces 50% tariffs on Chinese imports, China vows retaliation", "NDX", -1),
    ("US and China agree to pause tariffs for 90 days", "NDX", 1),
    ("OPEC+ agrees to cut output by 1 million barrels per day", "OIL", 1),
    ("Israel and Iran agree to ceasefire", "OIL", -1),
    ("Bitcoin ETF inflows hit record $1.2 billion in a single day", "BTC", 1),
    ("Crypto exchange halts withdrawals amid insolvency fears", "BTC", -1),
    ("DeFi protocol drained of $120 million in exploit", "ALT", -1),
    ("SEC drops lawsuit against Coinbase", "BTC", 1),
])
def test_events_move_the_right_markets_the_right_way(title, target, direction):
    a = analyse(h(title))
    assert impacts(a).get(target) == direction, (title, impacts(a))


def test_company_news_moves_the_company():
    a = analyse(h("Nvidia beats estimates, raises revenue forecast on AI demand"))
    assert a.impacts[0].target == "TICKER:NVDA" and a.impacts[0].direction == 1
    b = analyse(h("Tesla shares plunge after deliveries miss expectations"))
    assert impacts(b)["TICKER:TSLA"] == -1
    c = analyse(h("Apple to report earnings", tickers=("AAPL",)))
    assert "TICKER:AAPL" in impacts(c)


def test_jobs_news_is_unclear_for_stocks_but_clear_for_yields():
    a = analyse(h("Jobs report: US economy adds 300,000 jobs, far more than expected"))
    assert impacts(a)["SPX"] == 0 and impacts(a)["UST10"] == 1
    assert "±" in impact_text(a.impacts[0])


def test_noise_ranks_low():
    big = analyse(h("US CPI rises more than expected in September, core inflation accelerates"))
    opinion = analyse(h("Is Apple Stock a Buy Now?"))
    listicle = analyse(h("Rate Hikes Are Back. Here Are 3 Industrial Stocks Built to Win Anyway"))
    foreign = analyse(h("Thailand CPI inflation rises 2.82% in September"))
    wrap = analyse(h("Nasdaq closes at record high, as stocks climb despite surging Treasury yields"))
    fed_admin = analyse(h("Federal Reserve Board announces approval of application by Isabella Bank Corporation"))
    assert big.importance >= 80 and big.confidence == "High"
    for weak in (opinion, listicle, foreign, wrap, fed_admin):
        assert weak.importance < 55, weak.headline.title
    assert opinion.opinion and wrap.priced_in
    assert foreign.impacts[0].high < big.impacts[0].high  # another country's data moves US markets less


def test_hedged_and_negated_wording():
    hedged = analyse(h("Fed could cut rates next month, sources say"))
    sure = analyse(h("Fed cuts rates"))
    assert hedged.intensity < sure.intensity and hedged.importance < sure.importance
    assert analyse(h("Inflation does not cool as hoped, stays sticky")).polarity == -1


def test_volatility_scales_the_size():
    calm = analyse(h("Fed cuts rates by 25 basis points"), vol_ratio={"^GSPC": 0.6})
    wild = analyse(h("Fed cuts rates by 25 basis points"), vol_ratio={"^GSPC": 2.0})
    spx = lambda a: next(i for i in a.impacts if i.target == "SPX")
    assert spx(wild).high > spx(calm).high * 3


def test_impact_book_grades_and_recalibrates(tmp_path: Path):
    book = ImpactBook(StateStore(tmp_path / "r.json"))
    a = analyse(h("Fed cuts rates by 25 basis points, signals more easing ahead"))
    now = 1_800_000_000
    book.record(a, {"^GSPC": 100.0, "^IXIC": 100.0, "^TNX": 4.0, "^RUT": 100.0, "DX-Y.NYB": 100.0}, now=now)
    assert not book.due(now + 3600)
    assert "^GSPC" in book.symbols_due(now + 25 * 3600)
    graded = book.grade({"^GSPC": 103.0, "^IXIC": 103.0, "^TNX": 3.9, "^RUT": 103.0, "DX-Y.NYB": 99.0},
                        now=now + 25 * 3600)
    assert graded == 4
    s = book.summary()
    assert s["n"] == 4 and s["hits"] == 4
    # Markets moved more than predicted, so future estimates for this event grow (within limits).
    assert 1.0 < book.calibration("fed", "SPX") <= 2.5
    assert book.calibration("inflation", "SPX") == 1.0


def test_opinion_pieces_are_not_graded(tmp_path: Path):
    book = ImpactBook(StateStore(tmp_path / "r.json"))
    book.record(analyse(h("Is the Fed about to cut rates? Here's what I'd do")), {"^GSPC": 1.0})
    assert book.summary()["pending"] == 0


# ----- feeds -----

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Fed holds rates steady</title><link>https://a.test/1</link><description>&lt;p&gt;The Fed held.&lt;/p&gt;</description>
<pubDate>Tue, 06 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://a.test/2</link><pubDate>Tue, 01 Sep 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Bitcoin jumps</title>
<link href="https://b.test/1"/><updated>2026-10-06T11:00:00Z</updated><summary>BTC up</summary></entry></feed>"""


def test_parse_rss_and_atom():
    now = 1_791_300_000  # 2026-10-06
    items = parse_feed(RSS, Feed("T", "https://a.test/rss", "stocks"), now=now)
    assert [i.title for i in items] == ["Fed holds rates steady"]  # the old story is skipped
    assert items[0].summary == "The Fed held." and items[0].link == "https://a.test/1"
    atom = parse_feed(ATOM, Feed("T", "https://b.test", "crypto"), now=now)
    assert atom[0].link == "https://b.test/1" and atom[0].market == "crypto"
    assert parse_feed("not xml", Feed("T", "u", "stocks")) == []


def test_google_news_titles_carry_the_outlet():
    feed = next(f for f in FEEDS if f.name == "Google News")
    xml = RSS.replace("Fed holds rates steady", "Fed holds rates steady - Reuters")
    [item] = parse_feed(xml, feed, now=1_791_300_000)
    assert item.title == "Fed holds rates steady" and item.source == "Reuters"


def test_dedupe_merges_the_same_story():
    a = h("Fed holds interest rates steady at September meeting")
    b = h("Fed holds interest rates steady at its September meeting", tickers=("SPY",))
    b.source = "Other"
    c = h("Bitcoin rallies")
    out = dedupe([a, b, c])
    assert len(out) == 2
    merged = next(x for x in out if "Fed" in x.title)
    assert merged.also == ["Other"] or merged.source == "Other"
    assert similar(a.title, b.title) and not similar(a.title, c.title)


def test_yahoo_news_keeps_related_tickers():
    [item] = from_yahoo([{"title": "Nvidia rallies", "publisher": "X", "link": "l", "providerPublishTime": 1,
                          "type": "STORY", "relatedTickers": ["NVDA"]}], "stocks")
    assert item.tickers == ("NVDA",)


# ----- the optional Claude reader -----

def test_ai_reader_is_off_without_a_key():
    reader = ai.NewsAI(api_key="")
    assert not reader.enabled
    assert asyncio.run(reader.review([analyse(h("Fed cuts rates"))])) == 0


def test_ai_answer_replaces_the_rules_read():
    a = analyse(h("Fed cuts rates"))
    ai.apply(a, {"id": "0", "relevant": True, "event": "Fed", "takeaway": "Cut was priced in; muted reaction.",
                 "importance": 62, "confidence": "Medium", "polarity": "good",
                 "impacts": [{"asset": "SPX", "ticker": "", "direction": "up", "move_low": 0.4, "move_high": 0.1},
                             {"asset": "TICKER", "ticker": "jpm", "direction": "down", "move_low": 1, "move_high": 2},
                             {"asset": "TICKER", "ticker": "", "direction": "up", "move_low": 1, "move_high": 2}]})
    assert a.source == "ai" and a.note.startswith("Cut was") and a.importance == 62
    assert [(i.target, i.direction, i.low, i.high) for i in a.impacts] == [("SPX", 1, 0.1, 0.4), ("TICKER:JPM", -1, 1, 2)]
    b = analyse(h("Fed cuts rates"))
    ai.apply(b, {"id": "0", "relevant": False, "event": "", "takeaway": "", "importance": 90, "confidence": "Low",
                 "polarity": "mixed", "impacts": []})
    assert b.importance <= 15 and b.impacts == []


def test_ai_review_sends_one_request_per_batch_and_handles_refusals():
    calls = []

    class Messages:
        async def create(self, **kw):
            calls.append(kw)
            if len(calls) == 2:
                return SimpleNamespace(stop_reason="refusal", content=[])
            items = [{"id": str(i), "relevant": True, "event": "x", "takeaway": "t", "importance": 50,
                      "confidence": "Low", "polarity": "good", "impacts": []} for i in range(ai.BATCH)]
            import json
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"items": items}))])

    reader = ai.NewsAI(api_key="")
    reader.client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    reader.status.enabled = True
    batch = [analyse(h(f"Fed cuts rates {i}")) for i in range(ai.BATCH + 3)]
    changed = asyncio.run(reader.review(batch))
    assert changed == ai.BATCH and len(calls) == 2
    assert calls[0]["model"] == ai.DEFAULT_MODEL and calls[0]["fallbacks"] == "default"
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert reader.status.last_error == "declined"
