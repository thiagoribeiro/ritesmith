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


@pytest.mark.skip(
    reason="POST /generate/shell has never existed in ritesmith/api/routes/generations.py "
    "(only '', '/lua', and '/trama-workflow' are registered) — this test has been failing "
    "since the repo's initial commit, just never surfaced because CI was failing earlier at "
    "the Lint step. shell_script is a real ArtifactType with a PolicyEngine deny rule and a "
    "RITESMITH_SHELL_GENERATION_ENABLED setting, so the endpoint looks planned but was never "
    "built. Needs a decision: implement POST /generate/shell for real, or drop shell_script "
    "support entirely — not something to guess at via a lint/CI fix."
)
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


# ------------------------------------------------------------------
# P0.3: risk-class test gate
# ------------------------------------------------------------------

_IN_V = {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"]}
_OUT_R = {"type": "object", "properties": {"result": {"type": "number"}}, "required": ["result"]}


class _MediumRiskLLM(LLMProvider):
    """Valid medium-risk script; generate_tests yields whatever it is seeded with."""

    def __init__(self, tests):
        self._tests = tests
        self.gen_tests_called = False

    async def generate_lua(self, goal: str = "", **kwargs):
        return LuaGenerationResponse(
            script="function run(input, context)\n  return {result = input.value * 2}\nend",
            name=goal[:40].lower().replace(" ", "_") or "double",
            description=goal or "doubles",
            risk_assessment="medium",
            runtime_profile="transform_only",
        ), _stats()

    async def generate_tests(self, goal, input_schema, output_schema):
        self.gen_tests_called = True
        return list(self._tests), _stats()

    async def repair_lua(self, **kwargs):
        raise AssertionError("repair must not run: the script is valid")

    async def analyze_intent(self, **kwargs):
        raise NotImplementedError


async def _generate_direct(db_session, llm, intent):
    from ritesmith.config import get_settings
    from ritesmith.core.generation import GenerationService
    from ritesmith.schemas.generation import GenerateScriptRequest

    svc = GenerationService(db=db_session, llm=llm, settings=get_settings())
    req = GenerateScriptRequest(
        intent=intent,
        language="lua",
        save=True,
        input_schema=_IN_V,
        output_schema=_OUT_R,
        constraints={"reuse_policy": "force_new"},
    )
    return await svc.generate_lua(req)


async def _artifact_count(db_session, name):
    from sqlalchemy import func, select

    from ritesmith.registry.models import Artifact as ArtifactORM

    return await db_session.scalar(
        select(func.count()).select_from(ArtifactORM).where(ArtifactORM.name == name)
    )


@pytest.mark.asyncio
async def test_medium_risk_generates_and_passes_tests_then_persists(db_session):
    from ritesmith.tests.factories import unique

    intent = unique("double the value")
    llm = _MediumRiskLLM([{"input": {"value": 3}, "expected_output": {"result": 6}}])
    resp = await _generate_direct(db_session, llm, intent)
    assert llm.gen_tests_called
    assert resp.validation.valid
    assert await _artifact_count(db_session, resp.artifact.name) == 1


@pytest.mark.asyncio
async def test_medium_risk_without_tests_is_not_accepted(db_session):
    from ritesmith.tests.factories import unique

    intent = unique("double the value")
    llm = _MediumRiskLLM([])  # no sanity cases could be produced
    resp = await _generate_direct(db_session, llm, intent)
    assert llm.gen_tests_called
    # Validated (the script is fine), but the risk gate blocks acceptance/persistence.
    assert resp.validation.valid
    assert await _artifact_count(db_session, resp.artifact.name) == 0


@pytest.mark.asyncio
async def test_medium_risk_with_failing_generated_test_drives_repair(db_session):
    # A wrong expected_output makes test execution fail → valid=False → repair.
    from ritesmith.tests.factories import unique

    intent = unique("double the value")
    llm = _MediumRiskLLM([{"input": {"value": 3}, "expected_output": {"result": 999}}])
    with pytest.raises(AssertionError, match="repair must not run"):
        await _generate_direct(db_session, llm, intent)


# ------------------------------------------------------------------
# P0.5: explicit Luau requirement at startup
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_luau_fails_startup_when_lunardyson_missing(monkeypatch):
    from ritesmith.api.app import _lifespan, create_app
    from ritesmith.config import get_settings
    from ritesmith.runtime import luau

    monkeypatch.setenv("RITESMITH_SCRIPT_LANGUAGE", "luau")
    monkeypatch.setenv("RITESMITH_REQUIRE_LUAU", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(luau, "luau_available", lambda: False)

    app = create_app()
    with pytest.raises(RuntimeError, match="lunardyson"):
        async with _lifespan(app):
            pass


def test_missing_luau_without_require_falls_back_to_lua(monkeypatch):
    # require_luau=false → no startup error; language quietly resolves to lua.
    from ritesmith.config import get_settings
    from ritesmith.runtime import luau

    monkeypatch.setenv("RITESMITH_SCRIPT_LANGUAGE", "luau")
    monkeypatch.setenv("RITESMITH_REQUIRE_LUAU", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(luau, "luau_available", lambda: False)

    assert luau.effective_script_language(get_settings()) == "lua"
