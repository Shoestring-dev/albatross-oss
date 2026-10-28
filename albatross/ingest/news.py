"""Headline ingestion from public RSS feeds. (CryptoPanic has its own collector: albatross/deepend/discovery.py.)

Assets are tagged with a cheap text match here; the News Scout agent does the real
classification (impact, sentiment, horizon) later.
"""

import asyncio
import html
import logging
import re
from calendar import timegm
from datetime import UTC, datetime

import feedparser

from albatross import db
from albatross.http import client, retrying

log = logging.getLogger(__name__)

FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock": "https://www.theblock.co/rss.xml",
    "decrypt": "https://decrypt.co/feed",
    "bitcoinmagazine": "https://bitcoinmagazine.com/.rss/full/",
    "cryptoslate": "https://cryptoslate.com/feed/",
    "blockworks": "https://blockworks.co/feed",
    "dlnews": "https://www.dlnews.com/arc/outboundfeeds/rss/",
}

# Tickers that are also common words / too ambiguous to match on bare text.
AMBIGUOUS = {"ONE", "TON", "GAS", "SUN", "MOVE", "HOT", "BIG", "ACT", "AI", "OP", "ME", "IO", "S",
             "USD", "ETF", "SEC", "CEO", "NEW", "ALL", "ANY", "FUN", "HIGH", "TRUMP", "PEOPLE", "FORM"}
_TAG_RE = re.compile(r"<[^>]+>")


class Tagger:
    def __init__(self, assets: list[tuple[str, str | None]]):
        self.symbols = {s for s, _ in assets if len(s) >= 3 and s not in AMBIGUOUS}
        self.names = {n.lower(): s for s, n in assets if n and len(n) >= 4}
        self.sym_re = re.compile(r"(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9]{2,9})(?![A-Za-z0-9])")
        self.name_re = (
            re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(self.names, key=len, reverse=True)) + r")\b",
                       re.IGNORECASE)
            if self.names else None
        )

    def tag(self, text: str) -> list[str]:
        found = {m for m in self.sym_re.findall(text) if m in self.symbols}
        if self.name_re:
            found |= {self.names[m.lower()] for m in self.name_re.findall(text)}
        return sorted(found)


async def _tagger() -> Tagger:
    pool = await db.pool()
    rows = await pool.fetch("SELECT symbol, name FROM assets WHERE active")
    return Tagger([(r["symbol"], r["name"]) for r in rows])


@retrying
async def _fetch(url: str) -> bytes:
    r = await client().get(url)
    r.raise_for_status()
    return r.content


def _clean(markup: str | None) -> str | None:
    if not markup:
        return None
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", markup))).strip()[:2000]


def _title(raw: str) -> str:
    """Some feeds escape twice, so the parsed title still reads "S&amp;P"; titles are plain text."""
    return html.unescape(raw).strip()


async def _rss(source: str, url: str, tagger: Tagger) -> list[tuple]:
    feed = feedparser.parse(await _fetch(url))
    rows = []
    for e in feed.entries:
        if not e.get("link") or not e.get("title"):
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime.fromtimestamp(timegm(t), UTC) if t else datetime.now(UTC)
        summary = _clean(e.get("summary"))
        rows.append((source, e.link, _title(e.title), summary, published,
                     tagger.tag(f"{e.title} {summary or ''}")))
    return rows


async def ingest() -> int:
    tagger = await _tagger()
    results = await asyncio.gather(*(_rss(s, u, tagger) for s, u in FEEDS.items()), return_exceptions=True)
    rows: list[tuple] = []
    for source, res in zip(FEEDS, results, strict=True):
        if isinstance(res, Exception):
            log.warning("feed %s failed: %s: %s", source, type(res).__name__, res)
        else:
            rows.extend(res)

    if not rows:
        raise RuntimeError("no news fetched from any source")
    # First writer wins per URL; re-seen headlines are ignored.
    return await db.upsert("news_items", ["source", "url", "title", "summary", "published_at", "assets"],
                           rows, ["url"], update=[])
