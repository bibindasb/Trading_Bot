import httpx
import pytest

from data import news

NOW = 1_800_000_000  # 2027-01-15 08:00 UTC
SETTINGS = {"symbols": ["BTC/USDT", "ETH/USDT"],
            "news": {"feeds": ["https://a.test/rss", "https://b.test/rss"], "max_age_hours": 24,
                     "keywords": {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth", "ether"]}}}


def rss(*items):
    body = "".join(f"<item><title>{t}</title><pubDate>{d}</pubDate></item>" for t, d in items)
    return f"<rss><channel><title>Bitcoin News Daily</title>{body}</channel></rss>".encode()


RECENT, OLD = "Fri, 15 Jan 2027 07:00:00 +0000", "Tue, 12 Jan 2027 07:00:00 +0000"


@pytest.fixture
def feeds(monkeypatch):
    feeds = {}

    def get(url, **kwargs):
        content = feeds[url]
        if isinstance(content, Exception):
            raise content
        return httpx.Response(200, content=content, request=httpx.Request("GET", url))

    monkeypatch.setattr(news.httpx, "get", get)
    return feeds


def test_headlines_are_matched_to_coins_by_whole_words(feeds):
    feeds["https://a.test/rss"] = rss(("Bitcoin tops $70k", RECENT), ("ETH staking grows", RECENT),
                                      ("Ether and BTC both slide", RECENT), ("New method for together-ness", RECENT),
                                      ("Solana outage", RECENT))
    feeds["https://b.test/rss"] = rss()
    result = news.fetch_headlines(SETTINGS, now=NOW)
    assert result["BTC/USDT"] == ["Bitcoin tops $70k", "Ether and BTC both slide"]
    assert result["ETH/USDT"] == ["ETH staking grows", "Ether and BTC both slide"]


def test_old_undated_and_duplicate_items_are_dropped(feeds):
    feeds["https://a.test/rss"] = rss(("Bitcoin old news", OLD), ("Bitcoin undated", "not a date"),
                                      ("Bitcoin   rises", RECENT))
    feeds["https://b.test/rss"] = rss(("Bitcoin rises", RECENT))
    assert news.fetch_headlines(SETTINGS, now=NOW)["BTC/USDT"] == ["Bitcoin rises"]


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), b"<rss><broken", b"x" * (news.MAX_FEED_BYTES + 1)])
def test_failing_feed_is_skipped(feeds, failure):
    feeds["https://a.test/rss"] = failure
    feeds["https://b.test/rss"] = rss(("Bitcoin holds", RECENT))
    assert news.fetch_headlines(SETTINGS, now=NOW)["BTC/USDT"] == ["Bitcoin holds"]


def test_coin_without_keywords_gets_no_headlines(feeds):
    feeds["https://a.test/rss"] = rss(("Bitcoin holds", RECENT))
    feeds["https://b.test/rss"] = rss()
    settings = {**SETTINGS, "symbols": ["SOL/USDT"]}
    assert news.fetch_headlines(settings, now=NOW) == {"SOL/USDT": []}
