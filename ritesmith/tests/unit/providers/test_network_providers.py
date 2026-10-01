"""Market, web and telegram providers with mocked HTTP (pytest-httpx)."""

import httpx
import pytest

from ritesmith.config import get_settings
from ritesmith.runtime.providers import market, telegram, web


@pytest.fixture
def _fresh_settings(monkeypatch):
    """Settings read at call time; set env then clear the lru_cache."""

    def apply(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    return apply


# ---------------------------------------------------------------------------
# market
# ---------------------------------------------------------------------------


def test_bitcoin_price_parses_coingecko(httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{market._COINGECKO}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
        ),
        json={"bitcoin": {"usd": 64000.5, "usd_24h_change": -1.2}},
    )
    out = market._bitcoin_price("USD")
    assert out["symbol"] == "BTC" and out["price"] == 64000.5
    assert out["change_24h"] == -1.2 and out["currency"] == "USD"


def test_market_throttles_repeat_fetches(httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{market._COINGECKO}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
        ),
        json={"bitcoin": {"usd": 1}},
    )
    assert "error" not in market._bitcoin_price("usd")
    throttled = market._bitcoin_price("usd")  # same key within _MIN_INTERVAL
    assert throttled["error"] == "rate_limited"


def test_market_reports_http_failure(httpx_mock):
    httpx_mock.add_exception(
        httpx.ConnectError("down"),
        url=httpx.URL(
            f"{market._COINGECKO}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "eur", "include_24hr_change": "true"},
        ),
    )
    assert "error" in market._bitcoin_price("eur")


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------


def test_web_search_without_key_returns_error(_fresh_settings):
    _fresh_settings(RITESMITH_WEB_SEARCH_API_KEY="")
    assert web._web_search("anything")[0]["error"].startswith("WEB_SEARCH_API_KEY")


@pytest.mark.parametrize("url", ["http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.1/x"])
def test_web_fetch_blocks_private_urls(url):
    assert web._web_fetch(url).startswith("[blocked]")
    assert "error" in web._web_fetch_json(url)


def test_web_fetch_json_wraps_errors(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url="http://example.com/api")
    assert "error" in web._web_fetch_json("http://example.com/api")


# ---------------------------------------------------------------------------
# telegram
# ---------------------------------------------------------------------------


def _telegram_url(token="tok"):
    return f"https://api.telegram.org/bot{token}/sendMessage"


def test_telegram_not_configured(_fresh_settings):
    _fresh_settings(RITESMITH_TELEGRAM_BOT_TOKEN="", RITESMITH_TELEGRAM_CHAT_ID="")
    assert telegram._send("hi")["ok"] is False


def test_telegram_send_success(_fresh_settings, httpx_mock):
    _fresh_settings(RITESMITH_TELEGRAM_BOT_TOKEN="tok", RITESMITH_TELEGRAM_CHAT_ID="42")
    httpx_mock.add_response(url=_telegram_url(), json={"ok": True, "result": {"message_id": 7}})
    out = telegram._send("hello")
    assert out == {"ok": True, "message_id": 7}


def test_markdown_rejection_falls_back_to_plain_text(_fresh_settings, httpx_mock):
    _fresh_settings(RITESMITH_TELEGRAM_BOT_TOKEN="tok", RITESMITH_TELEGRAM_CHAT_ID="42")
    # First (MarkdownV2) attempt is rejected, the plain-text retry succeeds.
    httpx_mock.add_response(url=_telegram_url(), json={"ok": False, "description": "bad entities"})
    httpx_mock.add_response(url=_telegram_url(), json={"ok": True, "result": {"message_id": 9}})

    out = telegram._send_markdown("*oops*")
    assert out == {"ok": True, "message_id": 9}

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    import json

    assert json.loads(requests[0].read()).get("parse_mode") == "MarkdownV2"
    assert "parse_mode" not in json.loads(requests[1].read())
