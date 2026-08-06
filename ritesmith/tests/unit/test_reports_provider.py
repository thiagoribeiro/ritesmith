"""Unit tests for ReportsProvider."""

from pathlib import Path
from unittest.mock import patch

from ritesmith.runtime.providers.reports import (
    ReportsProvider,
    _build_html,
    _list_reports,
    _path_for,
    _sanitize_slug,
    _write_html,
)

# ---------------------------------------------------------------------------
# _sanitize_slug
# ---------------------------------------------------------------------------


def test_sanitize_slug_strips_special_chars():
    assert _sanitize_slug("BTC/USD — 30 days!", 60) == "btcusd__30_days"


def test_sanitize_slug_truncates():
    assert len(_sanitize_slug("a" * 100, 10)) == 10


def test_sanitize_slug_spaces_to_underscore():
    assert _sanitize_slug("hello world", 30) == "hello_world"


# ---------------------------------------------------------------------------
# _path_for
# ---------------------------------------------------------------------------


def test_path_for_no_reports_path():
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=None):
        result = _path_for({"theme": "market", "description": "test"})
    assert result["path"] is None
    assert "not configured" in result["error"]


def test_path_for_invalid_theme():
    # Chars-only junk → empty slug → invalid
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=Path("/reports")):
        result = _path_for({"theme": "!@#$%^", "description": "test"})
    assert result["path"] is None
    assert "invalid theme" in result["error"]


def test_path_for_traversal_sanitized():
    # "../../etc" sanitizes to "etc" — traversal is neutralised, path stays inside root
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=Path("/reports")):
        result = _path_for({"theme": "../../etc", "description": "test"})
    assert result["error"] is None
    assert "/reports/" in result["path"]
    assert "etc" in result["path"]
    assert ".." not in result["path"]


def test_path_for_returns_path(tmp_path):
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path):
        result = _path_for({"theme": "market", "description": "bitcoin_trend"})
    assert result["error"] is None
    p = Path(result["path"])
    assert str(p).startswith(str(tmp_path))
    assert "market" in str(p)
    assert "bitcoin_trend" in p.name
    assert p.suffix == ".html"


# ---------------------------------------------------------------------------
# _write_html
# ---------------------------------------------------------------------------


def test_write_html_no_reports_path():
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=None):
        result = _write_html(
            {"theme": "market", "description": "test", "title": "T", "body": "<v-app/>"}
        )
    assert result["path"] is None
    assert "not configured" in result["error"]


def test_write_html_path_escape_rejected(tmp_path):
    # Only-special-chars theme sanitizes to empty slug → rejected before any file write
    with (
        patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path),
        patch("ritesmith.runtime.providers.reports._send_telegram"),
    ):
        result = _write_html(
            {
                "theme": "!@#$%",
                "description": "passwd",
                "title": "Hack",
                "body": "<v-app/>",
            }
        )
    assert result["path"] is None
    assert "invalid theme" in result["error"]


def test_write_html_creates_file(tmp_path):
    with (
        patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path),
        patch("ritesmith.runtime.providers.reports._send_telegram") as mock_tg,
    ):
        result = _write_html(
            {
                "theme": "market",
                "description": "bitcoin_price",
                "title": "BTC Chart",
                "body": "<v-app><v-main>hello</v-main></v-app>",
            }
        )

    assert result["error"] is None
    p = Path(result["path"])
    assert p.exists()
    content = p.read_text()
    assert "BTC Chart" in content
    assert "chart.js" in content
    assert "vuetify" in content.lower()
    assert "hello" in content
    mock_tg.assert_called_once()


def test_write_html_with_charts(tmp_path):
    charts = {
        "priceChart": {
            "type": "line",
            "data": {
                "labels": ["Jan", "Feb"],
                "datasets": [{"label": "BTC", "data": [40000, 45000]}],
            },
        }
    }
    with (
        patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path),
        patch("ritesmith.runtime.providers.reports._send_telegram"),
    ):
        result = _write_html(
            {
                "theme": "market",
                "description": "btc_line",
                "title": "BTC",
                "body": "<v-app><canvas id='priceChart'></canvas></v-app>",
                "charts": charts,
            }
        )

    assert result["error"] is None
    content = Path(result["path"]).read_text()
    assert "priceChart" in content
    assert "new Chart" in content
    assert '"type": "line"' in content
    assert '"Jan"' in content
    assert "40000" in content


def test_write_html_sends_telegram_title_and_path(tmp_path):
    with (
        patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path),
        patch("ritesmith.runtime.providers.reports._send_telegram") as mock_tg,
    ):
        _write_html(
            {"theme": "test", "description": "demo", "title": "My Report", "body": "<v-app/>"}
        )

    call_args = mock_tg.call_args
    assert call_args[0][0] == "My Report"
    assert call_args[0][1].endswith(".html")


# ---------------------------------------------------------------------------
# _build_html
# ---------------------------------------------------------------------------


def test_build_html_no_charts():
    html = _build_html("Title", "<v-app/>", None)
    assert "Title" in html
    assert "<v-app/>" in html
    assert "chart.js" in html
    assert "new Chart" not in html


def test_build_html_chart_inits_injected():
    charts = {"myChart": {"type": "bar", "data": {}}}
    html = _build_html("T", "<v-app/>", charts)
    assert 'new Chart(document.getElementById("myChart")' in html
    assert '"type": "bar"' in html


# ---------------------------------------------------------------------------
# _list_reports
# ---------------------------------------------------------------------------


def test_list_reports_no_path():
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=None):
        result = _list_reports()
    assert result[0]["error"] == "RITESMITH_REPORTS_PATH is not configured"


def test_list_reports_returns_sorted(tmp_path):
    # create two fake reports
    r1 = tmp_path / "202505" / "market" / "20250501_120000_btc.html"
    r2 = tmp_path / "202505" / "market" / "20250502_120000_eth.html"
    r1.parent.mkdir(parents=True, exist_ok=True)
    r1.write_text("<html/>")
    r2.write_text("<html/>")

    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path):
        result = _list_reports()

    assert len(result) == 2
    # newer file (r2) first
    assert "eth" in result[0]["description"]
    assert result[0]["theme"] == "market"


# ---------------------------------------------------------------------------
# ReportsProvider.is_available
# ---------------------------------------------------------------------------


def test_provider_unavailable_without_path():
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=None):
        assert ReportsProvider().is_available() is False


def test_provider_available_with_path(tmp_path):
    with patch("ritesmith.runtime.providers.reports._reports_root", return_value=tmp_path):
        assert ReportsProvider().is_available() is True
