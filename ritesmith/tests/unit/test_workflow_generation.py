"""Tests for WorkflowValidator, WorkflowGenerationService, and /generate/trama-workflow."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ritesmith.api.app import create_app
from ritesmith.api.deps import get_llm_provider
from ritesmith.llm.base import (
    IntentAnalysis,
    LLMCallStats,
    LLMProvider,
    LuaGenerationResponse,
    WorkflowGenerationResponse,
    WorkflowRepairResponse,
)
from ritesmith.storage.postgres import get_db
from ritesmith.workflows.validator import WorkflowValidator


# ------------------------------------------------------------------
# WorkflowValidator — unit tests (no DB needed)
# ------------------------------------------------------------------

def _make_definition(steps: list[dict]) -> dict:
    return {"name": "test_workflow", "steps": steps}


def test_validator_valid_linear_workflow():
    v = WorkflowValidator()
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "capability_id": "cap.a", "depends_on": []},
        {"step_id": "s2", "step_type": "capability", "capability_id": "cap.b", "depends_on": ["s1"]},
    ])
    assert v.validate(defn) == []


def test_validator_missing_dependency():
    v = WorkflowValidator()
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "capability_id": "cap.a", "depends_on": ["nonexistent"]},
    ])
    errors = v.validate(defn)
    assert any("nonexistent" in e for e in errors)


def test_validator_cycle_detection():
    v = WorkflowValidator()
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "capability_id": "cap.a", "depends_on": ["s2"]},
        {"step_id": "s2", "step_type": "capability", "capability_id": "cap.b", "depends_on": ["s1"]},
    ])
    errors = v.validate(defn)
    assert any("cycle" in e.lower() for e in errors)


def test_validator_invalid_step_type():
    v = WorkflowValidator()
    defn = _make_definition([
        {"step_id": "s1", "step_type": "invalid_type", "depends_on": []},
    ])
    errors = v.validate(defn)
    assert any("invalid_type" in e for e in errors)


def test_validator_capability_without_capability_id():
    v = WorkflowValidator()
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "depends_on": []},
    ])
    errors = v.validate(defn)
    assert any("capability_id" in e for e in errors)


def test_validator_sleep_step_no_capability_id():
    v = WorkflowValidator()
    defn = _make_definition([
        {"step_id": "s1", "step_type": "sleep", "depends_on": [], "config": {"seconds": 5}},
    ])
    assert v.validate(defn) == []


def test_validator_layer2_unknown_capability():
    cap_registry = {"cap.known": {"input_schema": {}, "output_schema": {}}}
    v = WorkflowValidator(cap_registry)
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "capability_id": "cap.unknown", "depends_on": []},
    ])
    errors = v.validate(defn)
    assert any("not found in registry" in e for e in errors)


def test_validator_layer2_invalid_from_field():
    cap_registry = {
        "cap.a": {
            "input_schema": {},
            "output_schema": {"properties": {"price": {"type": "number"}}},
        },
        "cap.b": {
            "input_schema": {"required": ["amount"]},
            "output_schema": {},
        },
    }
    v = WorkflowValidator(cap_registry)
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "capability_id": "cap.a", "depends_on": []},
        {
            "step_id": "s2",
            "step_type": "capability",
            "capability_id": "cap.b",
            "depends_on": ["s1"],
            "inputs": {"amount": {"from_step": "s1", "from_field": "nonexistent_field"}},
        },
    ])
    errors = v.validate(defn)
    assert any("nonexistent_field" in e for e in errors)


# ------------------------------------------------------------------
# WorkflowValidator — v2 format (Trama native: entrypoint + nodes)
# ------------------------------------------------------------------

def _v2(nodes: list[dict], entrypoint: str = "n1") -> dict:
    return {"name": "wf", "version": "2.0.0", "entrypoint": entrypoint, "nodes": nodes}


def test_validator_v2_valid_linear():
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://svc/a", "verb": "POST"}, "successStatusCodes": [200]},
         "next": "n2"},
        {"id": "n2", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://svc/b", "verb": "GET"}, "successStatusCodes": [200]},
         "next": "end"},
    ])
    assert v.validate(defn) == []


def test_validator_v2_switch_valid():
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://svc", "verb": "POST"}, "successStatusCodes": [200]},
         "next": "decide"},
        {"id": "decide", "kind": "switch",
         "cases": [{"name": "a", "when": {"==": [{"var": "execution.input.type"}, "a"]}, "target": "do_a"}],
         "default": "end"},
        {"id": "do_a", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://svc/a", "verb": "POST"}, "successStatusCodes": [200]},
         "next": "end"},
    ])
    assert v.validate(defn) == []


def test_validator_v2_switch_missing_default():
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "switch",
         "cases": [{"name": "a", "when": {"==": [{"var": "x"}, "a"]}, "target": "end"}]},
    ])
    errors = v.validate(defn)
    assert any("default" in e for e in errors)


def test_validator_v2_bad_next_reference():
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://svc", "verb": "GET"}, "successStatusCodes": [200]},
         "next": "ghost"},
    ])
    errors = v.validate(defn)
    assert any("ghost" in e for e in errors)


def test_validator_v2_missing_entrypoint():
    v = WorkflowValidator()
    defn = {"name": "wf", "nodes": [
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://x", "verb": "GET"}, "successStatusCodes": [200]},
         "next": "end"},
    ]}
    errors = v.validate(defn)
    assert any("entrypoint" in e for e in errors)


def test_validator_v2_async_callback_missing_fields():
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "async-http-callback",
                    "request": {"url": "http://svc", "verb": "POST"},
                    "acceptedStatusCodes": [202],
                    "callback": {"timeoutMillis": 60000}},  # missing successWhen
         "next": "end"},
    ])
    errors = v.validate(defn)
    assert any("successWhen" in e for e in errors)


def test_validator_v2_cycle():
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://x", "verb": "GET"}, "successStatusCodes": [200]},
         "next": "n2"},
        {"id": "n2", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://y", "verb": "GET"}, "successStatusCodes": [200]},
         "next": "n1"},  # cycle back
    ])
    errors = v.validate(defn)
    assert any("cycle" in e.lower() for e in errors)


def test_validator_v2_layer2_hallucinated_capability_name():
    """Layer 2 catches an unknown capability_name in action.request.body (regression: was dead code)."""
    cap_registry = {"web.fetch_json": {}, "market.bitcoin_price": {}}
    v = WorkflowValidator(cap_registry)
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {
             "url": "http://ritesmith:8081/trama/execute", "verb": "POST",
             "body": {"capability_name": "web.search_semantic", "input": {"query": "test"}},
         }, "successStatusCodes": [200]},
         "next": "end"},
    ])
    errors = v.validate(defn)
    assert any("web.search_semantic" in e for e in errors)
    assert any("web.fetch_json" in e for e in errors), "error should list valid alternatives"


def test_validator_v2_layer2_valid_capability_name_passes():
    cap_registry = {"web.fetch_json": {}, "market.bitcoin_price": {}}
    v = WorkflowValidator(cap_registry)
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {
             "url": "http://ritesmith:8081/trama/execute", "verb": "POST",
             "body": {"capability_name": "web.fetch_json", "input": {"url": "https://api.example.com"}},
         }, "successStatusCodes": [200]},
         "next": "end"},
    ])
    assert v.validate(defn) == []


def test_validator_v2_layer2_template_undefined_node():
    """Layer 2 catches {{ nodes.X.* }} references to node IDs that don't exist."""
    cap_registry = {"web.fetch_json": {}}
    v = WorkflowValidator(cap_registry)
    defn = _v2([
        {"id": "fetch", "kind": "task",
         "action": {"mode": "sync", "request": {
             "url": "http://ritesmith:8081/trama/execute", "verb": "POST",
             "body": {"capability_name": "web.fetch_json",
                      "input": {"url": "{{ nodes.typo_node.response.body.output.url }}"}},
         }, "successStatusCodes": [200]},
         "next": "end"},
    ], entrypoint="fetch")
    errors = v.validate(defn)
    assert any("typo_node" in e for e in errors)


def test_validator_v2_layer2_template_valid_node_passes():
    cap_registry = {"web.fetch_json": {}}
    v = WorkflowValidator(cap_registry)
    defn = _v2([
        {"id": "step1", "kind": "task",
         "action": {"mode": "sync", "request": {
             "url": "http://ritesmith:8081/trama/execute", "verb": "POST",
             "body": {"capability_name": "web.fetch_json", "input": {"url": "https://api.example.com"}},
         }, "successStatusCodes": [200]},
         "next": "step2"},
        {"id": "step2", "kind": "task",
         "action": {"mode": "sync", "request": {
             "url": "http://ritesmith:8081/trama/execute", "verb": "POST",
             "body": {"capability_name": "web.fetch_json",
                      "input": {"url": "{{ nodes.step1.response.body.output.next_url }}"}},
         }, "successStatusCodes": [200]},
         "next": "end"},
    ], entrypoint="step1")
    assert v.validate(defn) == []


def test_validator_layer2_valid_wiring():
    cap_registry = {
        "cap.a": {
            "input_schema": {},
            "output_schema": {"properties": {"price": {"type": "number"}}},
        },
        "cap.b": {
            "input_schema": {},
            "output_schema": {},
        },
    }
    v = WorkflowValidator(cap_registry)
    defn = _make_definition([
        {"step_id": "s1", "step_type": "capability", "capability_id": "cap.a", "depends_on": []},
        {
            "step_id": "s2",
            "step_type": "capability",
            "capability_id": "cap.b",
            "depends_on": ["s1"],
            "inputs": {"value": {"from_step": "s1", "from_field": "price"}},
        },
    ])
    assert v.validate(defn) == []


# ------------------------------------------------------------------
# WorkflowValidator — polling loop / max_iterations
# ------------------------------------------------------------------

def _polling_loop_defn(max_iterations=6) -> dict:
    """Minimal valid finite polling loop: fetch → switch → (alert | sleep→fetch)."""
    defn: dict = {
        "name": "btc_monitor",
        "version": "2.0.0",
        "entrypoint": "fetch",
        "nodes": [
            {"id": "fetch", "kind": "task",
             "action": {"mode": "sync", "request": {"url": "http://price-svc/btc", "verb": "GET"},
                        "successStatusCodes": [200]},
             "next": "check"},
            {"id": "check", "kind": "switch",
             "cases": [
                 {"name": "dropped", "when": {"<=": [{"var": "nodes.fetch.response.body.price"}, 90]},
                  "target": "alert"},
                 {"name": "exhausted", "when": {">=": [{"var": "execution.loop_count"}, max_iterations]},
                  "target": "end"},
             ],
             "default": "wait"},
            {"id": "alert", "kind": "task",
             "action": {"mode": "sync", "request": {"url": "http://alerts/notify", "verb": "POST"},
                        "successStatusCodes": [200]},
             "next": "end"},
            {"id": "wait", "kind": "sleep", "durationSeconds": 300, "next": "fetch"},
        ],
    }
    if max_iterations is not None:
        defn["max_iterations"] = max_iterations
    return defn


def test_validator_v2_allows_cycle_with_max_iterations():
    """A back-edge (sleep → fetch) is accepted when max_iterations is declared."""
    v = WorkflowValidator()
    assert v.validate(_polling_loop_defn(max_iterations=6)) == []


def test_validator_v2_rejects_cycle_without_max_iterations():
    """Without max_iterations, the same back-edge is a hard cycle error."""
    v = WorkflowValidator()
    defn = _polling_loop_defn(max_iterations=None)
    defn.pop("max_iterations", None)
    errors = v.validate(defn)
    assert any("cycle" in e.lower() for e in errors), f"Expected cycle error, got: {errors}"


def test_validator_v2_rejects_invalid_max_iterations():
    """max_iterations=0 is invalid (must be a positive integer)."""
    v = WorkflowValidator()
    defn = _polling_loop_defn(max_iterations=6)
    defn["max_iterations"] = 0
    errors = v.validate(defn)
    assert any("max_iterations" in e for e in errors), f"Expected max_iterations error, got: {errors}"


def test_validator_v2_sleep_node_missing_duration():
    """Sleep node without durationSeconds is a structural error."""
    v = WorkflowValidator()
    defn = _v2([
        {"id": "n1", "kind": "task",
         "action": {"mode": "sync", "request": {"url": "http://svc", "verb": "GET"}, "successStatusCodes": [200]},
         "next": "wait"},
        {"id": "wait", "kind": "sleep", "next": "end"},  # missing durationSeconds
    ])
    errors = v.validate(defn)
    assert any("durationSeconds" in e for e in errors), f"Expected durationSeconds error, got: {errors}"


def test_validator_v2_polling_loop_full_structure():
    """Complete Bitcoin monitor definition with native sleep node passes validation."""
    v = WorkflowValidator()
    defn = {
        "name": "bitcoin_price_monitor",
        "version": "2.0.0",
        "entrypoint": "fetch_price",
        "max_iterations": 6,
        "nodes": [
            {"id": "fetch_price", "kind": "task",
             "action": {"mode": "sync",
                        "request": {"url": "https://api.coindesk.com/v1/bpi/currentprice/BTC.json", "verb": "GET"},
                        "successStatusCodes": [200]},
             "next": "check"},
            {"id": "check", "kind": "switch",
             "cases": [
                 {"name": "price_dropped_3pct",
                  "when": {"<=": [{"/": [{"var": "nodes.fetch_price.response.body.bpi.USD.rate_float"},
                                         {"var": "execution.input.last_price"}]}, 0.97]},
                  "target": "send_alert"},
                 {"name": "loop_exhausted",
                  "when": {">=": [{"var": "execution.loop_count"}, 6]},
                  "target": "end"},
             ],
             "default": "sleep"},
            {"id": "send_alert", "kind": "task",
             "action": {"mode": "sync",
                        "request": {"url": "{{ execution.input.alert_url }}", "verb": "POST",
                                    "body": {"message": "Bitcoin dropped"}},
                        "successStatusCodes": [200, 201]},
             "next": "end"},
            {"id": "sleep", "kind": "sleep", "durationSeconds": 300, "next": "fetch_price"},
        ],
    }
    assert v.validate(defn) == []


# ------------------------------------------------------------------
# Mock LLM for workflow generation tests
# ------------------------------------------------------------------

def _stats() -> LLMCallStats:
    return LLMCallStats(model="mock", prompt_tokens=5, completion_tokens=30, total_tokens=35)


def _trama_node(node_id: str, cap_name: str, next_id: str, input_: dict | None = None) -> dict:
    return {
        "id": node_id, "kind": "task",
        "action": {"mode": "sync", "request": {
            "url": "http://ritesmith:8081/trama/execute", "verb": "POST",
            "body": {"capability_name": cap_name, "input": input_ or {}},
        }, "successStatusCodes": [200]},
        "next": next_id,
    }


_VALID_WORKFLOW = {
    "name": "mock_workflow", "version": "2.0.0", "entrypoint": "fetch",
    "nodes": [
        _trama_node("fetch", "web.fetch_json", "process", {"url": "https://api.example.com"}),
        _trama_node("process", "market.bitcoin_price", "end"),
    ],
}

_INVALID_WORKFLOW = {
    "name": "bad_workflow", "version": "2.0.0", "entrypoint": "s1",
    "nodes": [
        {"id": "s1", "kind": "task", "next": "ghost",  # references non-existent node
         "action": {"mode": "sync", "request": {"url": "http://x", "verb": "GET"}, "successStatusCodes": [200]}},
    ],
}

_REPAIRED_WORKFLOW = {
    "name": "bad_workflow", "version": "2.0.0", "entrypoint": "s1",
    "nodes": [
        _trama_node("s1", "web.fetch_json", "end", {"url": "https://api.example.com"}),
    ],
}


class MockWorkflowLLMValidOnFirst(LLMProvider):
    async def generate_lua(self, **kwargs) -> tuple[LuaGenerationResponse, LLMCallStats]:
        raise NotImplementedError

    async def repair_lua(self, **kwargs):
        raise NotImplementedError

    async def analyze_intent(self, goal: str = "", **kwargs):
        return IntentAnalysis(
            requires_lua=False, requires_workflow=True, artifact_types=["trama_workflow"],
            summary=f"Workflow for: {goal[:40]}", domain="general",
            suggested_name="workflow_cap",
        ), _stats()

    async def generate_workflow(self, goal: str = "", **kwargs):
        return WorkflowGenerationResponse(
            definition=_VALID_WORKFLOW,
            name=f"workflow_{goal[:20].replace(' ', '_')}",
            description=goal,
            required_capabilities=["http.get", "transform.apply"],
        ), _stats()

    async def repair_workflow(self, **kwargs):
        raise AssertionError("repair_workflow should not be called")


class MockWorkflowLLMRepairOnSecond(LLMProvider):
    def __init__(self):
        self.repair_call_count = 0

    async def generate_lua(self, **kwargs):
        raise NotImplementedError

    async def repair_lua(self, **kwargs):
        raise NotImplementedError

    async def analyze_intent(self, **kwargs):
        return IntentAnalysis(
            requires_workflow=True, artifact_types=["trama_workflow"],
            summary="test", domain="general", suggested_name="wf",
        ), _stats()

    async def generate_workflow(self, **kwargs):
        return WorkflowGenerationResponse(
            definition=_INVALID_WORKFLOW,
            name="invalid_workflow",
            description="has errors",
            required_capabilities=[],
        ), _stats()

    async def repair_workflow(self, **kwargs):
        self.repair_call_count += 1
        return WorkflowRepairResponse(
            definition=_REPAIRED_WORKFLOW,
            changes_made="Added capability_id to step",
        ), _stats()


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def wf_client_valid(db_session):
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_provider] = lambda: MockWorkflowLLMValidOnFirst()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def repair_llm():
    return MockWorkflowLLMRepairOnSecond()


@pytest_asyncio.fixture(loop_scope="session")
async def wf_client_repair(db_session, repair_llm):
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_provider] = lambda: repair_llm

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------
# Tests: POST /generate/trama-workflow
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_workflow_valid_first_attempt(wf_client_valid):
    resp = await wf_client_valid.post("/generate/trama-workflow", json={
        "intent": "monitor price and notify if it drops",
        "save": False,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reused"] is False
    assert data["artifact"]["artifact_type"] == "trama_workflow"
    content = data["artifact"]["content"]
    assert "nodes" in content


@pytest.mark.asyncio
async def test_generate_workflow_repair_loop(wf_client_repair, repair_llm):
    resp = await wf_client_repair.post("/generate/trama-workflow", json={
        "intent": "fetch data and process it",
        "save": False,
    })
    assert resp.status_code == 200, resp.text
    assert repair_llm.repair_call_count >= 1


@pytest.mark.asyncio
async def test_generate_workflow_save(wf_client_valid):
    resp = await wf_client_valid.post("/generate/trama-workflow", json={
        "intent": "workflow save test xqz_zlorp_unique",
        "save": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["artifact"]["artifact_id"].startswith("art_")
    assert data["artifact"]["status"] == "draft"


# ------------------------------------------------------------------
# Tests: POST /plans with trama_workflow intent
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_with_workflow_artifact_type(wf_client_valid):
    resp = await wf_client_valid.post("/plans", json={
        "intent": "orchestrate price monitoring workflow",
        "requested_artifact_types": ["trama_workflow"],
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["plan_id"].startswith("plan_")
    assert len(data["steps"]) == 1
    assert "workflow" in data["steps"][0]["title"].lower()
