"""End-to-end integration tests — real OpenAI API, real database.

Each scenario exercises the full request path:
  HTTP route → service layer → LLM (real OpenAI) → DB → response

Fixtures are session-scoped so the DB state from scenario 1 (saved artifact)
is visible in scenario 2 (reuse detection via FTS), which is intentional.

Run with:
  pytest -m e2e -v
"""

import json

import pytest

from ritesmith.workflows.validator import WorkflowValidator

pytestmark = pytest.mark.e2e


# ─────────────────────────────────────────────────────────────────────────────
# Shared state passed between session-scoped scenarios
# ─────────────────────────────────────────────────────────────────────────────


class _State:
    lua_artifact_id: str | None = None
    plan_id: str | None = None
    plan_artifact_id: str | None = None


_state = _State()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1 — Lua generation: LLM produces a valid, runnable script
# ─────────────────────────────────────────────────────────────────────────────


async def test_lua_generation_produces_valid_script(e2e_client):
    """Generate a Lua transform, save it, and verify the artifact structure.

    We use a domain-specific, short intent so the LLM-generated description
    will contain the same keywords and FTS reuse will succeed in test 3.
    """
    resp = await e2e_client.post(
        "/generate/lua",
        json={
            "intent": "Celsius to Fahrenheit temperature conversion",
            "save": True,
            "input_schema": {
                "type": "object",
                "properties": {"celsius": {"type": "number"}},
                "required": ["celsius"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"fahrenheit": {"type": "number"}},
                "required": ["fahrenheit"],
            },
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["reused"] is False
    artifact = data["artifact"]
    assert artifact["artifact_type"] == "lua_script"
    assert artifact["artifact_id"].startswith("art_")
    assert artifact["status"] in ("draft", "validated")
    assert artifact["content"] is not None
    assert "function run" in artifact["content"], "LLM must emit a run() function"

    _state.lua_artifact_id = artifact["artifact_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2 — Lua execution: the generated script runs and returns correct output
# ─────────────────────────────────────────────────────────────────────────────


async def test_lua_execution_returns_correct_output(e2e_client):
    """Execute the artifact from scenario 1 with a known input and check the result."""
    assert _state.lua_artifact_id, "Scenario 1 must run first"

    resp = await e2e_client.post(
        "/executions",
        json={
            "artifact_id": _state.lua_artifact_id,
            "input": {"celsius": 0},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["execution_id"].startswith("exec_")
    assert data["status"] in ("succeeded", "running", "queued"), (
        f"Unexpected status: {data['status']} — error: {data.get('error')}"
    )

    if data["status"] == "succeeded":
        output = data["output"] or {}
        fahrenheit = output.get("fahrenheit")
        assert fahrenheit == 32, f"Expected 0°C = 32°F, got {fahrenheit!r} — output: {output}"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3 — Reuse detection: same intent returns the cached artifact
# ─────────────────────────────────────────────────────────────────────────────


async def test_lua_reuse_returns_existing_artifact(e2e_client):
    """A second request with the same short intent must hit FTS and return reused=True.

    Short domain-specific intents ("Celsius to Fahrenheit temperature conversion")
    produce FTS queries with few, highly-distinctive tokens that reliably appear in
    the LLM-generated description saved in scenario 1.
    """
    resp = await e2e_client.post(
        "/generate/lua",
        json={
            "intent": "Celsius to Fahrenheit temperature conversion",
            "save": False,
            # No force_new → prefer_reuse default should kick in
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["reused"] is True, (
        "Expected reuse=True for identical short intent. "
        f"This usually means the LLM-generated description didn't contain the FTS keywords. "
        f"source_artifact_id={data.get('source_artifact_id')}"
    )
    assert data["source_artifact_id"] == _state.lua_artifact_id


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4 — Plan lifecycle: propose → approve (if needed) → persist artifacts
# ─────────────────────────────────────────────────────────────────────────────


async def test_plan_propose_approve_persist(e2e_client):
    """Full plan lifecycle: intent → plan proposed/approved → artifacts persisted."""
    # 4a. Propose a plan
    resp = await e2e_client.post(
        "/plans",
        json={
            "intent": "Parse a JSON string from input.raw and extract a field named 'amount'",
            "mode": "propose",
        },
    )
    assert resp.status_code == 201, resp.text
    plan = resp.json()

    assert plan["plan_id"].startswith("plan_")
    assert plan["status"] in ("proposed", "approved", "blocked")
    assert len(plan["steps"]) >= 1

    _state.plan_id = plan["plan_id"]

    if plan["status"] == "blocked":
        pytest.skip(f"Plan was blocked by policy: {plan.get('policy_decision')}")

    # 4b. Approve only if still in proposed state (PlanBuilder may auto-approve low-risk)
    if plan["status"] == "proposed":
        resp = await e2e_client.post(f"/plans/{_state.plan_id}/approve", json={})
        assert resp.status_code == 200, resp.text
        approved = resp.json()
        assert approved["status"] == "approved"

    # 4c. Persist artifacts
    resp = await e2e_client.post(f"/plans/{_state.plan_id}/artifacts", json={"status": "validated"})
    assert resp.status_code == 200, resp.text
    persisted = resp.json()

    assert persisted["status"] in ("persisted", "approved")
    if persisted.get("artifacts"):
        _state.plan_artifact_id = persisted["artifacts"][0]["artifact_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5 — Plan artifact execution: run what the plan produced
# ─────────────────────────────────────────────────────────────────────────────


async def test_plan_artifact_execution(e2e_client):
    """Execute an artifact that was created and persisted via the plan flow."""
    if not _state.plan_artifact_id:
        pytest.skip("No artifact persisted in scenario 4 — skipping execution test")

    resp = await e2e_client.post(
        "/executions",
        json={
            "artifact_id": _state.plan_artifact_id,
            "input": {"raw": '{"amount": 42.5, "currency": "USD"}'},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["status"] in ("succeeded", "running", "queued", "failed"), (
        f"Unexpected execution status: {data['status']}"
    )
    if data["status"] == "succeeded":
        output = data.get("output") or {}
        output_values = list(output.values())
        assert any(v == 42.5 or v == "42.5" for v in output_values), (
            f"Expected extracted amount 42.5 in output, got: {output}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 6 — Workflow generation: LLM produces a valid Trama v2 workflow
# ─────────────────────────────────────────────────────────────────────────────


async def test_workflow_generation_produces_valid_trama_structure(e2e_client):
    """Generate a Trama workflow and validate its structural correctness.

    The workflow artifact content is stored as a JSON string (json.dumps); we
    parse it before passing to WorkflowValidator.
    """
    resp = await e2e_client.post(
        "/generate/trama-workflow",
        json={
            "intent": (
                "Call a pricing service at http://pricing-svc/price to get the current price, "
                "then call http://notify-svc/notify with the result. "
                "Both calls are synchronous HTTP POST."
            ),
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    artifact = data["artifact"]
    assert artifact["artifact_type"] == "trama_workflow"

    # Content is stored as a JSON string — parse it
    raw = artifact["content"]
    content = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(content, dict), f"Parsed content must be a dict, got {type(content)}"

    # Must be v2 format: entrypoint + nodes
    assert "nodes" in content, f"Expected Trama v2 'nodes' key, got: {list(content.keys())}"
    assert "entrypoint" in content, "Expected Trama v2 'entrypoint' key"
    assert isinstance(content["nodes"], list)
    assert len(content["nodes"]) >= 1

    # Run through WorkflowValidator — must be structurally valid
    validator = WorkflowValidator()
    errors = validator.validate(content)
    assert errors == [], "Generated workflow failed structural validation:\n" + "\n".join(
        f"  - {e}" for e in errors
    )

    # Verify node structure
    for node in content["nodes"]:
        assert "id" in node, f"Node missing 'id': {node}"
        assert "kind" in node, f"Node missing 'kind': {node}"
        assert node["kind"] in ("task", "switch"), f"Unexpected node kind: {node['kind']}"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 7 — Workflow with branching: switch node is used correctly
# ─────────────────────────────────────────────────────────────────────────────


async def test_workflow_generation_with_branching(e2e_client):
    """Generate a workflow that requires a conditional branch; expect a switch node."""
    resp = await e2e_client.post(
        "/generate/trama-workflow",
        json={
            "intent": (
                "Call http://order-svc/status to get the order status. "
                "If the status is 'confirmed', call http://ship-svc/ship. "
                "If the status is 'cancelled', call http://notify-svc/cancel. "
                "All calls are synchronous HTTP POST."
            ),
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    raw = data["artifact"]["content"]
    content = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(content, dict), f"Parsed content must be a dict, got {type(content)}"
    assert "nodes" in content, "Expected Trama v2 format"

    # Validate structure
    validator = WorkflowValidator()
    errors = validator.validate(content)
    assert errors == [], "Branching workflow failed structural validation:\n" + "\n".join(
        f"  - {e}" for e in errors
    )

    kinds = {n["kind"] for n in content["nodes"]}
    assert "switch" in kinds, (
        f"Expected a 'switch' node for conditional branching, got node kinds: {kinds}. "
        f"Full workflow: {content}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 8 — Validation endpoint: real Lua validates cleanly
# ─────────────────────────────────────────────────────────────────────────────


async def test_validate_endpoint_accepts_correct_lua(e2e_client):
    """POST /validate with a handcrafted correct script must return valid=True."""
    script = """
function run(input)
    local total = 0
    for _, v in ipairs(input.values or {}) do
        total = total + v
    end
    return { result = total }
end
"""
    resp = await e2e_client.post(
        "/validate",
        json={
            "content": script,
            "artifact_type": "lua_script",
            "input_schema": {
                "type": "object",
                "properties": {"values": {"type": "array", "items": {"type": "number"}}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"result": {"type": "number"}},
                "required": ["result"],
            },
            "test_cases": [
                {"input": {"values": [10, 20, 30]}, "expected_output": {"result": 60}},
                {"input": {"values": []}, "expected_output": {"result": 0}},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["valid"] is True, (
        f"Expected valid=True, got errors: {result.get('errors')} "
        f"warnings: {result.get('warnings')}"
    )


async def test_validate_endpoint_rejects_forbidden_calls(e2e_client):
    """POST /validate with os.execute must be rejected by the sandbox checker."""
    malicious = """
function run(input)
    os.execute("rm -rf /")
    return { ok = true }
end
"""
    resp = await e2e_client.post(
        "/validate",
        json={
            "content": malicious,
            "artifact_type": "lua_script",
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["valid"] is False, "Expected valid=False for os.execute usage, but got valid=True"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 9 — Shell generation is always rejected (policy gate)
# ─────────────────────────────────────────────────────────────────────────────


async def test_shell_generation_is_forbidden(e2e_client):
    """POST /generate/shell must return 403 regardless of intent."""
    resp = await e2e_client.post(
        "/generate/shell",
        json={
            "intent": "list files in /tmp",
            "language": "bash",
        },
    )
    assert resp.status_code == 403, (
        f"Expected 403 for shell generation, got {resp.status_code}: {resp.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenarios 10-13 — Finite polling loop workflows
# ─────────────────────────────────────────────────────────────────────────────


def _assert_polling_loop(content: dict) -> None:
    """Shared structural assertions for all finite polling loop tests."""
    assert content.get("max_iterations", 0) >= 1, (
        "max_iterations must be declared at workflow root and be ≥ 1"
    )
    node_ids = {n["id"] for n in content["nodes"]}

    sleep_nodes = [n for n in content["nodes"] if n.get("kind") == "sleep"]
    assert sleep_nodes, (
        "Must have at least one native sleep node (kind='sleep'). "
        f"Node kinds found: {[n.get('kind') for n in content['nodes']]}"
    )
    assert sleep_nodes[0].get("durationSeconds", 0) > 0, "Sleep node must have durationSeconds > 0"
    assert sleep_nodes[0]["next"] in node_ids, (
        f"Sleep node 'next' must loop back to a real node, got: {sleep_nodes[0]['next']!r}"
    )

    switch_nodes = [n for n in content["nodes"] if n.get("kind") == "switch"]
    assert switch_nodes, "Must have at least one switch node for condition + exhaustion check"

    errors = WorkflowValidator().validate(content)
    assert errors == [], "Polling loop workflow failed structural validation:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


async def test_workflow_polling_bitcoin_price_drop(e2e_client):
    """Classic monitoring: Bitcoin price drop 3%, max 6 checks every 5 min."""
    resp = await e2e_client.post(
        "/generate/trama-workflow",
        json={
            "intent": (
                "Monitor Bitcoin price every 5 minutes by calling "
                "https://api.coindesk.com/v1/bpi/currentprice/BTC.json. "
                "If the price drops 3% compared to execution.input.last_price, "
                "POST an alert to http://alerts-svc/notify. "
                "Run up to 6 times, then stop."
            ),
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    content = json.loads(resp.json()["artifact"]["content"])
    _assert_polling_loop(content)


async def test_workflow_polling_api_health_monitor(e2e_client):
    """Periodically check if a payment API is healthy; alert on failure."""
    resp = await e2e_client.post(
        "/generate/trama-workflow",
        json={
            "intent": (
                "Every 2 minutes, call http://payment-svc/health (GET). "
                "If the response status is not 200, POST an alert to http://ops-svc/alert. "
                "Check up to 10 times, then stop."
            ),
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    content = json.loads(resp.json()["artifact"]["content"])
    _assert_polling_loop(content)


async def test_workflow_polling_inventory_threshold(e2e_client):
    """Poll inventory level every 10 min; alert when stock falls below threshold."""
    resp = await e2e_client.post(
        "/generate/trama-workflow",
        json={
            "intent": (
                "Every 10 minutes, call http://inventory-svc/stock (GET) to check stock level. "
                "If stock is below 50 units, POST an alert to http://ops-svc/restock. "
                "Check up to 5 times."
            ),
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    content = json.loads(resp.json()["artifact"]["content"])
    _assert_polling_loop(content)


async def test_workflow_polling_order_fulfilment_wait(e2e_client):
    """Poll order status every 3 min until fulfilled or 8 attempts exhausted."""
    resp = await e2e_client.post(
        "/generate/trama-workflow",
        json={
            "intent": (
                "Check order status by calling http://order-svc/status (POST, "
                "body: {orderId: '{{ execution.input.orderId }}'}) every 3 minutes. "
                "If the status is 'fulfilled', POST a confirmation to http://notify-svc/confirm. "
                "Give up after 8 attempts."
            ),
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200, resp.text
    content = json.loads(resp.json()["artifact"]["content"])
    _assert_polling_loop(content)
