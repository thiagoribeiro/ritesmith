"""Tests for GenerationService and /generate routes."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ritesmith.api.app import create_app
from ritesmith.api.deps import get_llm_provider
from ritesmith.llm.base import LLMCallStats, LLMProvider, LuaGenerationResponse, RepairResponse
from ritesmith.storage.postgres import get_db

# ------------------------------------------------------------------
# Mock LLM helpers
# ------------------------------------------------------------------


def _stats(model="gpt-4.1-mock") -> LLMCallStats:
    return LLMCallStats(model=model, prompt_tokens=10, completion_tokens=50, total_tokens=60)


def _valid_response(name="test_script") -> LuaGenerationResponse:
    return LuaGenerationResponse(
        script="function run(input, context)\n  return {result = input.value * 2}\nend",
        name=name,
        description="Doubles the input value",
        tags=["math", "transform"],
        risk_assessment="low",
        runtime_profile="transform_only",
    )


def _invalid_response() -> LuaGenerationResponse:
    return LuaGenerationResponse(
        script="os.execute('rm -rf /')",  # will fail forbidden token check
        name="evil_script",
        description="Bad script",
        tags=[],
        risk_assessment="high",
        runtime_profile="transform_only",
    )


def _repaired_response() -> LuaGenerationResponse:
    return LuaGenerationResponse(
        script="function run(input, context)\n  return {result = input.value}\nend",
        name="repaired_script",
        description="Fixed script",
        tags=["transform"],
        risk_assessment="low",
        runtime_profile="transform_only",
    )


class MockLLMValidOnFirst(LLMProvider):
    """Always returns a valid script/workflow on the first attempt."""

    async def generate_lua(self, goal: str = "", **kwargs):
        from ritesmith.llm.base import LuaGenerationResponse

        name = goal[:40].lower().replace(" ", "_") if goal else "test_script"
        return LuaGenerationResponse(
            script="function run(input, context)\n  return {result = input.value * 2}\nend",
            name=name,
            description=goal or "Doubles the input value",
            tags=["math", "transform"],
            risk_assessment="low",
            runtime_profile="transform_only",
        ), _stats()

    async def repair_lua(self, **kwargs):
        raise AssertionError("repair_lua should not be called when first attempt is valid")

    async def analyze_intent(self, **kwargs):
        from ritesmith.llm.base import IntentAnalysis

        return IntentAnalysis(
            requires_lua=True,
            requires_workflow=False,
            artifact_types=["lua_script"],
            summary="test",
            domain="general",
            suggested_name="test_cap",
        ), _stats()

    async def generate_workflow(self, goal: str = "", **kwargs):
        from ritesmith.llm.base import WorkflowGenerationResponse

        return WorkflowGenerationResponse(
            definition={
                "name": "test_workflow",
                "version": "2.0.0",
                "entrypoint": "fetch",
                "nodes": [
                    {
                        "id": "fetch",
                        "kind": "task",
                        "action": {
                            "mode": "sync",
                            "request": {
                                "url": "__RS_BASE_URL__/trama/execute",
                                "verb": "POST",
                                "body": {
                                    "capability_name": "web.fetch_json",
                                    "input": {"url": "https://example.com"},
                                },
                            },
                            "successStatusCodes": [200],
                        },
                        "next": "end",
                    },
                ],
            },
            name=f"workflow_{goal[:20].replace(' ', '_')}",
            description=goal or "test workflow",
            required_capabilities=["web.fetch_json"],
        ), _stats()


class MockLLMRepairOnSecond(LLMProvider):
    """Returns invalid on attempt 1, valid repaired on attempt 2."""

    def __init__(self):
        self.repair_call_count = 0
        self.generate_call_count = 0

    async def generate_lua(self, **kwargs):
        self.generate_call_count += 1
        return _invalid_response(), _stats()

    async def repair_lua(self, **kwargs):
        self.repair_call_count += 1
        resp = RepairResponse(
            repaired_content="function run(input, context)\n  return {result = input.value}\nend",
            changes_made="Removed forbidden os.execute call",
        )
        return resp, _stats()

    async def analyze_intent(self, **kwargs):
        raise NotImplementedError


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def mock_client_valid(db_session):
    """Client with MockLLMValidOnFirst injected."""
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_provider] = lambda: MockLLMValidOnFirst()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def repair_llm():
    return MockLLMRepairOnSecond()


@pytest_asyncio.fixture(loop_scope="session")
async def mock_client_repair(db_session, repair_llm):
    """Client with MockLLMRepairOnSecond injected."""
    app = create_app()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_llm_provider] = lambda: repair_llm

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------
# Tests: POST /generate/lua
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_lua_valid_first_attempt(mock_client_valid):
    resp = await mock_client_valid.post(
        "/generate/lua",
        json={
            "intent": "xqz_unique_tokenize_frobulate_zlorp_77",
            "save": False,
            "constraints": {"reuse_policy": "force_new"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reused"] is False
    assert data["artifact"]["content"] is not None
    assert "run" in data["artifact"]["content"]


@pytest.mark.asyncio
async def test_generate_lua_repair_loop(mock_client_repair, repair_llm):
    resp = await mock_client_repair.post(
        "/generate/lua",
        json={
            "intent": "return the input value",
            "save": False,
        },
    )
    assert resp.status_code == 200
    assert repair_llm.generate_call_count >= 1
    assert repair_llm.repair_call_count >= 1


@pytest.mark.asyncio
async def test_generate_lua_save_creates_artifact(mock_client_valid):
    resp = await mock_client_valid.post(
        "/generate/lua",
        json={
            "intent": "multiply input by two and save",
            "save": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reused"] is False
    artifact = data["artifact"]
    assert artifact["artifact_id"].startswith("art_")
    assert artifact["status"] == "draft"


@pytest.mark.asyncio
async def test_generate_lua_reuse(mock_client_valid):
    """Second request with same intent should reuse the artifact."""
    payload = {"intent": "compute hash of the input string for reuse test", "save": True}
    r1 = await mock_client_valid.post("/generate/lua", json=payload)
    assert r1.status_code == 200
    first_id = r1.json()["artifact"]["artifact_id"]

    r2 = await mock_client_valid.post("/generate/lua", json=payload)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["reused"] is True
    assert d2["source_artifact_id"] == first_id


# ------------------------------------------------------------------
# Tests: POST /generate/shell → 403
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_shell_forbidden(mock_client_valid):
    resp = await mock_client_valid.post(
        "/generate/shell",
        json={
            "intent": "delete all files",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "policy_denied"


# ------------------------------------------------------------------
# Tests: POST /generate/trama-workflow → 501
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_workflow_returns_artifact(mock_client_valid):
    resp = await mock_client_valid.post(
        "/generate/trama-workflow",
        json={
            "intent": "monitor price and notify",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["artifact"]["artifact_type"] == "trama_workflow"
    assert data["reused"] is False
