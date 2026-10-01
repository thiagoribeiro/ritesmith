"""Trama HTTP adapter, definition normalization, DelegationService and the shared repair loop."""

import httpx
import pytest

from ritesmith.core.delegation import DelegationService
from ritesmith.core.exceptions import LLMError, LLMRateLimitError, RiteSmithError
from ritesmith.core.repair import run_repair_loop
from ritesmith.observability.metrics import generation_attempts_total
from ritesmith.workflows.adapters.http_workflow_engine import (
    HttpWorkflowEngineAdapter,
    _normalize_definition,
)

BASE = "http://trama.test"


# ---------------------------------------------------------------------------
# _normalize_definition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["end", "done", "finish", "terminal", "stop", "complete"])
def test_terminal_keywords_become_null(terminal):
    out = _normalize_definition({"nodes": [{"id": "a", "kind": "task", "next": terminal}]})
    assert out["nodes"][0]["next"] is None


def test_regular_next_is_kept():
    out = _normalize_definition({"nodes": [{"id": "a", "kind": "task", "next": "b"}]})
    assert out["nodes"][0]["next"] == "b"


def test_sleep_seconds_become_millis():
    out = _normalize_definition(
        {"nodes": [{"id": "s", "kind": "sleep", "durationSeconds": 2.5, "next": "a"}]}
    )
    node = out["nodes"][0]
    assert node["durationMillis"] == 2500
    assert "durationSeconds" not in node


def test_sleep_with_millis_is_untouched():
    node = {"id": "s", "kind": "sleep", "durationSeconds": 9, "durationMillis": 100, "next": "a"}
    assert _normalize_definition({"nodes": [node]})["nodes"][0]["durationMillis"] == 100


def test_default_failure_handling_injected_only_when_missing():
    assert "failureHandling" in _normalize_definition({"nodes": []})
    custom = {"type": "backoff", "maxAttempts": 3}
    assert (
        _normalize_definition({"nodes": [], "failureHandling": custom})["failureHandling"] == custom
    )


def test_normalization_does_not_mutate_input():
    definition = {"nodes": [{"id": "a", "kind": "task", "next": "end"}]}
    _normalize_definition(definition)
    assert definition["nodes"][0]["next"] == "end"
    assert "failureHandling" not in definition


@pytest.mark.parametrize(
    "weird", [None, "text", 42, {"nodes": "not-a-list"}, {"nodes": ["not-a-dict"]}]
)
def test_odd_inputs_pass_through(weird):
    _normalize_definition(weird)  # must not raise


# ---------------------------------------------------------------------------
# HttpWorkflowEngineAdapter
# ---------------------------------------------------------------------------


async def test_submit_posts_normalized_definition_and_returns_id(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{BASE}/workflows/run", json={"id": "trama-123"})
    adapter = HttpWorkflowEngineAdapter(BASE + "/", api_key="k")
    execution_id = await adapter.submit_workflow(
        {"nodes": [{"id": "a", "kind": "task", "next": "end"}]}, {"x": 1}
    )
    assert execution_id == "trama-123"

    request = httpx_mock.get_request()
    assert request.headers["Authorization"] == "Bearer k"
    body = request.read()
    import json

    sent = json.loads(body)
    assert sent["payload"] == {"x": 1}
    assert sent["definition"]["nodes"][0]["next"] is None
    assert "failureHandling" in sent["definition"]


async def test_submit_without_api_key_sends_no_auth_header(httpx_mock):
    httpx_mock.add_response(method="POST", url=f"{BASE}/workflows/run", json={"id": "x"})
    await HttpWorkflowEngineAdapter(BASE).submit_workflow({"nodes": []})
    assert "Authorization" not in httpx_mock.get_request().headers


async def test_submit_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(
        method="POST", url=f"{BASE}/workflows/run", status_code=422, json={"error": "bad"}
    )
    with pytest.raises(httpx.HTTPStatusError):
        await HttpWorkflowEngineAdapter(BASE).submit_workflow({"nodes": []})


async def test_get_status(httpx_mock):
    httpx_mock.add_response(method="GET", url=f"{BASE}/workflows/abc", json={"status": "SUCCEEDED"})
    assert await HttpWorkflowEngineAdapter(BASE).get_status("abc") == {"status": "SUCCEEDED"}


# ---------------------------------------------------------------------------
# DelegationService
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.submitted: list[tuple[dict, dict | None]] = []

    async def submit_workflow(self, definition, input_data=None):
        if self.fail:
            raise ConnectionError("engine down")
        self.submitted.append((definition, input_data))
        return "ext-1"

    async def get_status(self, external_execution_id):
        if self.fail:
            raise ConnectionError("engine down")
        return {"status": "RUNNING", "id": external_execution_id}


async def test_delegation_passes_through():
    adapter = _FakeAdapter()
    service = DelegationService(adapter)
    assert await service.delegate({"nodes": []}, {"a": 1}) == "ext-1"
    assert adapter.submitted == [({"nodes": []}, {"a": 1})]
    assert (await service.get_status("ext-1"))["status"] == "RUNNING"


async def test_delegation_wraps_engine_errors():
    service = DelegationService(_FakeAdapter(fail=True))
    with pytest.raises(RiteSmithError, match="Workflow delegation failed"):
        await service.delegate({"nodes": []})
    with pytest.raises(RiteSmithError, match="Failed to get workflow status"):
        await service.get_status("x")


# ---------------------------------------------------------------------------
# run_repair_loop
# ---------------------------------------------------------------------------


def _outcome_count(outcome: str) -> float:
    return generation_attempts_total.labels(artifact_type="loop_test", outcome=outcome)._value.get()


async def _run(results, max_attempts=5):
    attempts: list[int] = []
    queue = list(results)

    async def attempt_fn(n: int) -> bool:
        attempts.append(n)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    await run_repair_loop("loop_test", max_attempts, attempt_fn)
    return attempts


async def test_stops_at_first_success():
    before = _outcome_count("success")
    assert await _run([False, False, True, False]) == [1, 2, 3]
    assert _outcome_count("success") == before + 1


async def test_exhausts_attempts():
    before = _outcome_count("exhausted")
    assert await _run([False] * 5) == [1, 2, 3, 4, 5]
    assert _outcome_count("exhausted") == before + 1


async def test_three_consecutive_parse_failures_abort_early():
    assert await _run([LLMError("x"), LLMError("x"), LLMError("x"), True]) == [1, 2, 3]


async def test_parse_failure_counter_resets_after_a_parsed_attempt():
    attempts = await _run([LLMError("x"), LLMError("x"), False, LLMError("x"), True])
    assert attempts == [1, 2, 3, 4, 5]


async def test_rate_limit_backs_off_exponentially(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("ritesmith.core.repair.asyncio.sleep", fake_sleep)
    assert await _run([LLMRateLimitError("slow"), LLMRateLimitError("slow"), True]) == [1, 2, 3]
    assert sleeps == [2, 4]


async def test_no_backoff_after_the_last_attempt(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("ritesmith.core.repair.asyncio.sleep", fake_sleep)
    await _run([LLMRateLimitError("slow")] * 2, max_attempts=2)
    assert sleeps == [2]
