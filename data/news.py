"""Crypto news headlines from RSS feeds. Headlines are untrusted text; only the sentiment agent reads them."""
import logging
import re
import time
import xml.etree.ElementTree as ET  # Python's bundled expat refuses entity-expansion bombs
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)
MAX_FEED_BYTES = 2_000_000


def _published(item) -> float | None:
    try:
        return parsedate_to_datetime(item.findtext("pubDate")).timestamp()
    except (TypeError, ValueError):
        return None


def fetch_headlines(settings: dict, now: float | None = None) -> dict[str, list[str]]:
    """Recent headlines per configured symbol, matched by the coin's keywords. A failing feed is skipped."""
    cfg = settings["news"]
    cutoff = (time.time() if now is None else now) - cfg["max_age_hours"] * 3600
    titles = []
    for url in cfg["feeds"]:
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            if len(r.content) > MAX_FEED_BYTES:
                raise ValueError(f"feed larger than {MAX_FEED_BYTES} bytes")
            for item in ET.fromstring(r.content).iter("item"):
                title, published = item.findtext("title"), _published(item)
                if title and published and published >= cutoff:
                    titles.append(" ".join(title.split()))
        except Exception as e:
            log.warning("news: feed %s failed: %r", url, e)
    titles = list(dict.fromkeys(titles))  # the same story often runs on several feeds

    headlines = {}
    for symbol in settings["symbols"]:
        words = cfg["keywords"].get(symbol.split("/")[0], [])
        pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I)
        headlines[symbol] = [t for t in titles if words and pattern.search(t)]
    return headlines
