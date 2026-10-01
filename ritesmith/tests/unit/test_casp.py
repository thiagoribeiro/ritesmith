"""CASP client: provider selection by priority/resource type, discovery cache, query/resolve/execute."""

import json

import httpx
import pytest

from ritesmith.config import get_settings
from ritesmith.runtime.casp import client as casp

PROVIDERS = [
    {"url": "http://switches.casp/", "resource_types": ["smart-switch"], "priority": 10},
    {"url": "http://any.casp", "resource_types": [], "priority": 50},
    {"url": "http://fast.casp", "resource_types": ["smart-switch"], "priority": 1},
]


@pytest.fixture
def casp_env(monkeypatch):
    def apply(providers=PROVIDERS):
        monkeypatch.setenv("RITESMITH_CASP_PROVIDERS", json.dumps(providers))
        get_settings.cache_clear()

    return apply


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_lowest_priority_int_wins(casp_env):
    casp_env()
    assert casp._provider_url("smart-switch") == "http://fast.casp"


def test_empty_resource_types_is_a_catch_all(casp_env):
    casp_env()
    assert casp._provider_url("ac-unit") == "http://any.casp"


def test_no_provider_for_unmatched_type(casp_env):
    casp_env([{"url": "http://only.casp", "resource_types": ["smart-switch"], "priority": 1}])
    assert casp._provider_url("presence-sensor") is None


def test_no_providers_configured(casp_env):
    casp_env([])
    assert casp._provider_url("smart-switch") is None


# ---------------------------------------------------------------------------
# Discovery cache
# ---------------------------------------------------------------------------


def test_discovery_is_cached(casp_env, httpx_mock):
    casp_env()
    httpx_mock.add_response(
        url="http://fast.casp/casp/v1/types", json={"resourceTypes": ["smart-switch"]}
    )
    first = casp.get_types("http://fast.casp")
    second = casp.get_types("http://fast.casp")  # served from cache, no 2nd request
    assert first == second == {"resourceTypes": ["smart-switch"]}
    assert len(httpx_mock.get_requests()) == 1


def test_discovery_failure_returns_empty(casp_env, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("down"), url="http://x.casp/casp/v1/types")
    assert casp.get_types("http://x.casp") == {"resourceTypes": []}


def test_invalidate_discovery(casp_env, httpx_mock):
    httpx_mock.add_response(url="http://fast.casp/casp/v1/types", json={"resourceTypes": ["a"]})
    httpx_mock.add_response(url="http://fast.casp/casp/v1/types", json={"resourceTypes": ["b"]})
    assert casp.get_types("http://fast.casp")["resourceTypes"] == ["a"]
    casp.invalidate_discovery("http://fast.casp")
    assert casp.get_types("http://fast.casp")["resourceTypes"] == ["b"]


# ---------------------------------------------------------------------------
# query / resolve / execute
# ---------------------------------------------------------------------------


def test_query_posts_and_returns_body(casp_env, httpx_mock):
    casp_env()
    httpx_mock.add_response(
        url="http://fast.casp/casp/v1/resources/query",
        json={"resources": [{"id": "sw-1", "type": "smart-switch"}]},
    )
    out = casp.query("smart-switch", {"room": "cozinha"}, ["turn-on"])
    assert out["resources"][0]["id"] == "sw-1"
    sent = json.loads(httpx_mock.get_request().read())
    assert sent == {
        "resourceType": "smart-switch",
        "filters": {"room": "cozinha"},
        "requiresCapabilities": ["turn-on"],
    }


def test_query_without_provider_returns_error(casp_env):
    casp_env([])
    assert casp.query("smart-switch", {}, [])["error"] == "no_provider"


def test_resolve_request_failure(casp_env, httpx_mock):
    casp_env()
    httpx_mock.add_exception(
        httpx.ConnectError("down"), url="http://fast.casp/casp/v1/resources/resolve"
    )
    out = casp.resolve("smart-switch", "turn-on", "led do painel")
    assert out["status"] == "invalid"


def test_execute_success(casp_env, httpx_mock):
    casp_env()
    httpx_mock.add_response(
        url="http://fast.casp/casp/v1/capabilities/execute", json={"status": "ok"}
    )
    out = casp.execute("sw-1", "smart-switch", "turn-on", {})
    assert out["status"] == "ok"


def test_execute_without_provider(casp_env):
    casp_env([])
    out = casp.execute("sw-1", "smart-switch", "turn-on", {})
    assert out["status"] == "failed" and out["errorCode"] == "NO_PROVIDER"
