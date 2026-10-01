"""Host functions: the SSRF URL guard, the HTTP host function, and the pure helpers."""

import pytest

from ritesmith.runtime import host_functions as hf
from ritesmith.runtime.host_functions import (
    _is_url_allowed,
    _text_slugify,
    get_functions_for_profile,
    list_names_for_profile,
)

BLOCKED = [
    "http://localhost/x",
    "https://LOCALHOST/x",
    "http://127.0.0.1/x",
    "http://127.1.2.3/x",
    "http://10.0.0.1/x",
    "http://172.16.0.1/x",
    "http://172.31.255.1/x",
    "http://192.168.1.1/x",
    "http://0.0.0.0/x",
    "http://[::1]/x",
]
ALLOWED = [
    "http://example.com/x",
    "https://api.github.com/repos",
    "http://172.15.0.1/x",  # just outside the private /12
    "http://172.32.0.1/x",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls(url):
    assert _is_url_allowed(url) is False


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    assert _is_url_allowed(url) is True


def test_http_request_refuses_blocked_url():
    out = hf._http_request("GET", "http://127.0.0.1:8081/health")
    assert out["error"] == "blocked_url"


def test_http_request_refuses_bad_method():
    out = hf._http_request("CONNECT", "http://example.com")
    assert out["error"] == "invalid_method"


def test_http_request_success(httpx_mock):
    httpx_mock.add_response(method="GET", url="http://example.com/j", json={"hello": "world"})
    out = hf._http_request("GET", "http://example.com/j")
    assert out["ok"] is True and out["status"] == 200 and out["body"] == {"hello": "world"}


def test_http_request_non_json_body_is_text(httpx_mock):
    httpx_mock.add_response(
        method="GET", url="http://example.com/t", text="plain text", status_code=200
    )
    out = hf._http_request("GET", "http://example.com/t")
    assert out["body"] == "plain text"


def test_http_request_caps_response_size(httpx_mock):
    httpx_mock.add_response(method="GET", url="http://example.com/big", content=b"x" * (600 * 1024))
    out = hf._http_request("GET", "http://example.com/big")
    assert len(out["body"]) <= hf._MAX_RESPONSE_BYTES


def test_http_request_timeout(httpx_mock):
    import httpx

    httpx_mock.add_exception(
        httpx.TimeoutException("slow"), method="GET", url="http://example.com/slow"
    )
    out = hf._http_request("GET", "http://example.com/slow")
    assert out["error"] == "timeout"


# ---------------------------------------------------------------------------
# Pure helpers and profile gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Hello, World!", "hello-world"),
        ("  a__b  ", "a-b"),
        ("Á Ê Î", "á-ê-î"),
        ("  --Hi!--  ", "hi"),
    ],
)
def test_slugify(text, slug):
    assert _text_slugify(text) == slug


def test_transform_only_profile_has_no_network():
    names = list_names_for_profile("transform_only")
    assert "json.encode" in names
    assert not any(n.startswith("http.") for n in names)


def test_readonly_network_profile_exposes_http():
    assert "http.request" in list_names_for_profile("readonly_network")


def test_unknown_profile_falls_back_to_transform_only():
    assert set(get_functions_for_profile("nonsense")) == set(
        get_functions_for_profile("transform_only")
    )
