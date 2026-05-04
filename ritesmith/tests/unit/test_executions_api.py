"""Tests for ExecutionService and /executions routes."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ritesmith.api.app import create_app
from ritesmith.api.deps import get_execution_service
from ritesmith.registry.models import Artifact as ArtifactORM, ArtifactVersion as ArtifactVersionORM
from ritesmith.storage.postgres import get_db


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def exec_client(db_session):
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def lua_artifact_id(exec_client, db_session):
    """Creates a real persisted Lua artifact for execution tests."""
    resp = await exec_client.post("/artifacts", json={
        "name": "exec_test_double",
        "artifact_type": "lua_script",
        "content": "function run(input, context)\n  return {result = (input.value or 0) * 2}\nend",
        "description": "Doubles input value",
        "risk_level": "low",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["artifact_id"]


@pytest_asyncio.fixture(loop_scope="session")
async def high_risk_artifact_id(exec_client):
    """Creates a high-risk artifact for policy denial tests."""
    resp = await exec_client.post("/artifacts", json={
        "name": "exec_test_high_risk",
        "artifact_type": "lua_script",
        "content": "function run(input, context)\n  return {ok = true}\nend",
        "risk_level": "high",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["artifact_id"]


@pytest_asyncio.fixture(loop_scope="session")
async def critical_artifact_id(exec_client):
    """Creates a critical artifact that should always be denied."""
    resp = await exec_client.post("/artifacts", json={
        "name": "exec_test_critical",
        "artifact_type": "lua_script",
        "content": "function run(input, context)\n  return {ok = true}\nend",
        "risk_level": "critical",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["artifact_id"]


# ------------------------------------------------------------------
# Tests: successful Lua execution
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_lua_succeeds(exec_client, lua_artifact_id):
    resp = await exec_client.post("/executions", json={
        "artifact_id": lua_artifact_id,
        "input": {"value": 21},
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "succeeded"
    assert data["output"]["result"] == 42
    assert data["execution_id"].startswith("exec_")
    assert data["artifact_id"] == lua_artifact_id


@pytest.mark.asyncio
async def test_execute_lua_dry_run(exec_client, lua_artifact_id):
    resp = await exec_client.post("/executions", json={
        "artifact_id": lua_artifact_id,
        "input": {"value": 5},
        "dry_run": True,
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "succeeded"
    assert data["output"]["dry_run"] is True


# ------------------------------------------------------------------
# Tests: idempotency
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_returns_same_execution(exec_client, lua_artifact_id):
    key = "idem-test-key-001"
    r1 = await exec_client.post("/executions", json={
        "artifact_id": lua_artifact_id,
        "input": {"value": 7},
        "idempotency_key": key,
    })
    r2 = await exec_client.post("/executions", json={
        "artifact_id": lua_artifact_id,
        "input": {"value": 99},  # different input — should be ignored
        "idempotency_key": key,
    })
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["execution_id"] == r2.json()["execution_id"]


# ------------------------------------------------------------------
# Tests: policy rejection
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critical_artifact_rejected(exec_client, critical_artifact_id):
    resp = await exec_client.post("/executions", json={
        "artifact_id": critical_artifact_id,
        "input": {},
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "rejected"
    assert "critical" in data["error"]["reason"].lower()


@pytest.mark.asyncio
async def test_high_risk_without_approval_token_waits(exec_client, high_risk_artifact_id):
    resp = await exec_client.post("/executions", json={
        "artifact_id": high_risk_artifact_id,
        "input": {},
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "waiting"
    assert data["error"]["requires_approval"] is True


# ------------------------------------------------------------------
# Tests: GET /executions/{id}
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_execution_by_id(exec_client, lua_artifact_id):
    create_resp = await exec_client.post("/executions", json={
        "artifact_id": lua_artifact_id,
        "input": {"value": 3},
    })
    exec_id = create_resp.json()["execution_id"]

    get_resp = await exec_client.get(f"/executions/{exec_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["execution_id"] == exec_id


@pytest.mark.asyncio
async def test_get_execution_not_found(exec_client):
    resp = await exec_client.get("/executions/exec_doesnotexist")
    assert resp.status_code == 404


# ------------------------------------------------------------------
# Tests: POST /policies/evaluate
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_policy_evaluate_low_risk_allowed(exec_client):
    resp = await exec_client.post("/policies/evaluate", json={
        "operation": "execute",
        "artifact_type": "lua_script",
        "risk_level": "low",
    })
    assert resp.status_code == 200
    assert resp.json()["decision"] == "allow"


@pytest.mark.asyncio
async def test_policy_evaluate_critical_denied(exec_client):
    resp = await exec_client.post("/policies/evaluate", json={
        "operation": "execute",
        "artifact_type": "lua_script",
        "risk_level": "critical",
    })
    assert resp.status_code == 200
    assert resp.json()["decision"] == "deny"


@pytest.mark.asyncio
async def test_policy_evaluate_shell_denied(exec_client):
    resp = await exec_client.post("/policies/evaluate", json={
        "operation": "execute",
        "artifact_type": "shell_script",
        "risk_level": "low",
    })
    assert resp.status_code == 200
    assert resp.json()["decision"] == "deny"
