"""Tests for PlanBuilder and /plans routes."""

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
    RepairResponse,
)
from ritesmith.storage.postgres import get_db

# ------------------------------------------------------------------
# Mock LLM for plan tests
# ------------------------------------------------------------------


def _stats() -> LLMCallStats:
    return LLMCallStats(model="mock", prompt_tokens=5, completion_tokens=20, total_tokens=25)


class MockPlanLLM(LLMProvider):
    """Returns intent analysis + valid Lua script on first attempt."""

    async def analyze_intent(self, goal: str = "", **kwargs) -> tuple[IntentAnalysis, LLMCallStats]:
        return IntentAnalysis(
            requires_lua=True,
            requires_workflow=False,
            requires_network=False,
            domain="general",
            suggested_name="plan_capability",
            summary=f"Plan for: {goal[:60]}",
            artifact_types=["lua_script"],
        ), _stats()

    async def generate_lua(
        self, goal: str = "", **kwargs
    ) -> tuple[LuaGenerationResponse, LLMCallStats]:
        name = goal[:40].lower().replace(" ", "_") if goal else "plan_script"
        return LuaGenerationResponse(
            script="function run(input, context)\n  return {ok = true}\nend",
            name=name,
            description=goal or "Plan capability",
            tags=["plan"],
            risk_assessment="low",
            runtime_profile="transform_only",
        ), _stats()

    async def repair_lua(self, **kwargs) -> tuple[RepairResponse, LLMCallStats]:
        raise AssertionError("repair should not be called in plan tests")


class MockHighRiskLLM(MockPlanLLM):
    """Returns high-risk artifact to trigger require_approval policy."""

    async def generate_lua(
        self, goal: str = "", **kwargs
    ) -> tuple[LuaGenerationResponse, LLMCallStats]:
        return LuaGenerationResponse(
            script="function run(input, context)\n  return {ok = true}\nend",
            name="high_risk_script",
            description="High risk capability",
            tags=[],
            risk_assessment="high",
            runtime_profile="transform_only",
        ), _stats()


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def plan_client(db_session):
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_provider] = lambda: MockPlanLLM()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def high_risk_client(db_session):
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_provider] = lambda: MockHighRiskLLM()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------
# Tests: POST /plans
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_plan_propose_returns_approved(plan_client):
    """Low-risk artifact → plan status should be approved automatically."""
    resp = await plan_client.post("/plans", json={"intent": "transform the input data"})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["plan_id"].startswith("plan_")
    assert data["status"] == "approved"
    assert data["intent"] == "transform the input data"
    assert len(data["steps"]) == 1
    assert data["steps"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_create_plan_high_risk_requires_approval(high_risk_client):
    """High-risk artifact → plan status should be proposed (requires approval)."""
    resp = await high_risk_client.post("/plans", json={"intent": "do something risky"})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "proposed"
    assert data["policy_decision"]["decision"] == "require_approval"


@pytest.mark.asyncio
async def test_create_plan_persist_mode_saves_artifacts(plan_client):
    """mode=persist should save artifacts and return artifact_ids."""
    resp = await plan_client.post(
        "/plans",
        json={
            "intent": "persist mode test capability",
            "mode": "persist",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "approved"
    assert len(data["artifacts"]) == 1
    assert data["artifacts"][0]["artifact_id"].startswith("art_")
    assert data["artifacts"][0]["status"] == "draft"


@pytest.mark.asyncio
async def test_create_plan_force_new_skips_reuse(plan_client):
    """reuse_policy=force_new should always generate even if similar exists."""
    payload = {"intent": "force new plan test", "reuse_policy": "force_new"}
    r1 = await plan_client.post("/plans", json={**payload, "mode": "persist"})
    r2 = await plan_client.post("/plans", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    d2 = r2.json()
    # With force_new the step should not say "Reuse"
    assert "Reuse" not in d2["steps"][0]["title"]


# ------------------------------------------------------------------
# Tests: GET /plans/{plan_id}
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_plan_returns_persisted_plan(plan_client):
    create_resp = await plan_client.post("/plans", json={"intent": "get plan test"})
    assert create_resp.status_code == 201
    plan_id = create_resp.json()["plan_id"]

    get_resp = await plan_client.get(f"/plans/{plan_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_get_plan_not_found(plan_client):
    resp = await plan_client.get("/plans/plan_doesnotexist")
    assert resp.status_code == 404


# ------------------------------------------------------------------
# Tests: approve / reject transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_proposed_plan(high_risk_client):
    create_resp = await high_risk_client.post("/plans", json={"intent": "approve transition test"})
    assert create_resp.status_code == 201
    plan_id = create_resp.json()["plan_id"]
    assert create_resp.json()["status"] == "proposed"

    approve_resp = await high_risk_client.post(
        f"/plans/{plan_id}/approve", json={"comment": "looks good"}
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_reject_proposed_plan(high_risk_client):
    create_resp = await high_risk_client.post("/plans", json={"intent": "reject transition test"})
    assert create_resp.status_code == 201
    plan_id = create_resp.json()["plan_id"]

    reject_resp = await high_risk_client.post(
        f"/plans/{plan_id}/reject", json={"reason": "too risky"}
    )
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_approve_already_approved_fails(plan_client):
    create_resp = await plan_client.post("/plans", json={"intent": "double approve test"})
    plan_id = create_resp.json()["plan_id"]
    assert create_resp.json()["status"] == "approved"

    # Cannot approve an already-approved plan
    resp = await plan_client.post(f"/plans/{plan_id}/approve")
    assert resp.status_code == 409
