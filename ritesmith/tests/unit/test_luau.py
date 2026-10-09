"""luau_script: LunarDyson runtime, type conversion, validation, generation and execution."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ritesmith.api.app import create_app
from ritesmith.api.deps import get_llm_provider
from ritesmith.config import get_settings
from ritesmith.core.execution import ExecutionService
from ritesmith.core.validation import ValidationPipeline
from ritesmith.llm.base import LLMCallStats, LLMProvider, LuaGenerationResponse, RepairResponse
from ritesmith.runtime import luau
from ritesmith.schemas.execution import CreateExecutionRequest, ExecutionStatus
from ritesmith.storage.postgres import get_db

pytestmark = pytest.mark.skipif(not luau.luau_available(), reason="lunardyson not installed")

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"prices": {"type": "array", "items": {"type": "number"}}},
    "required": ["prices"],
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"min_price": {"type": "number"}},
    "required": ["min_price"],
}
TYPED_MIN = """
function run(input: Input, context: Context): Output
    local best: number? = nil
    for _, p in input.prices do
        if best == nil or p < best then
            best = p
        end
    end
    if best == nil then
        return { error = "empty", message = "no prices" }
    end
    return { min_price = best }
end
"""
# Correct at runtime, but the untyped comparator is a strict-mode type error.
STRICT_ONLY = """
function run(input, context)
    local vals = {}
    for _, p in input.prices do table.insert(vals, p) end
    table.sort(vals, function(a, b) return a < b end)
    return { min_price = vals[1] }
end
"""


# ---------------------------------------------------------------------------
# JSON Schema → Luau
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "string"}, "string"),
        ({"type": "integer"}, "number"),
        ({"type": "array", "items": {"type": "boolean"}}, "{ boolean }"),
        ({"type": ["string", "null"]}, "string?"),
        ({"enum": ["up", "down"]}, '"up" | "down"'),
        ({"type": "object"}, "{ [string]: any }"),
        ({"type": "object", "additionalProperties": {"type": "number"}}, "{ [string]: number }"),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
                "required": ["a"],
            },
            "{ a: string, b: number? }",
        ),
        ({"type": "object", "properties": {"end": {"type": "string"}}}, "{ [string]: any }"),
        ({"anyOf": [{"type": "string"}, {"type": "number"}]}, "number | string"),
        (None, "any"),
    ],
)
def test_schema_to_luau(schema, expected):
    assert luau.schema_to_luau(schema) == expected


def test_tool_results_treat_properties_as_present():
    schema = {"type": "object", "properties": {"price": {"type": "number"}}}
    assert luau.schema_to_luau(schema, fields_present=True) == "{ price: number }"


@pytest.mark.parametrize(
    "profile",
    sorted(__import__("ritesmith.runtime.host_functions", fromlist=["PROFILES"]).PROFILES),
)
def test_every_profile_tool_signature_is_valid_luau(profile):
    rt = luau.build_runtime(profile, 1000, 16, type_decls=luau.script_type_declarations(None, None))
    try:
        diags = rt.check("function run(input: Input, context: Context): Output return {} end")
    finally:
        rt.close()
    assert [d for d in diags if d["severity"] == "error"] == []


def test_json_namespace_is_not_exposed():
    assert not any(
        name.startswith("json.") for name in luau.luau_tools_for_profile("transform_only")
    )


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime():
    return luau.LuauScriptRuntime(get_settings())


async def test_execute_with_host_tool(runtime):
    source = "function run(input) return { slug = tools.text.slugify({ s = input.title }) } end"
    result = await runtime.execute(source, {"title": "Hello World!"}, {})
    assert result.error is None
    assert result.output == {"slug": "hello-world"}


async def test_output_schema_violation_uses_contract_prefix(runtime):
    result = await runtime.execute(
        "function run() return { min_price = 'cheap' } end", {}, {}, output_schema=OUTPUT_SCHEMA
    )
    assert result.error.startswith("Output schema validation failed:")


@pytest.mark.parametrize(
    ("source", "prefix"),
    [
        ("function run( end", "Syntax/load error:"),
        ("function run() return tools.payment.refund({}) end", "Runtime error:"),
        ("function run() return { n = #string.rep('x', 2^30) } end", "Runtime error:"),
        ("function run() error('boom') end", "Runtime error:"),
    ],
)
async def test_errors_map_to_self_heal_prefixes(runtime, source, prefix):
    result = await runtime.execute(source, {}, {})
    assert result.error.startswith(prefix), result.error


async def test_missing_run_is_not_a_crash(runtime):
    result = await runtime.execute("local function run() return {} end", {}, {})
    assert not result.error.startswith(("Runtime error:", "Syntax/load error:"))


async def test_timeout_is_enforced(runtime):
    result = await runtime.execute("function run() while true do end end", {}, {}, timeout_ms=100)
    assert result.timed_out
    assert 90 <= result.duration_ms < 1000


# ---------------------------------------------------------------------------
# ValidationPipeline
# ---------------------------------------------------------------------------


async def _validate(source: str, **kwargs):
    return await ValidationPipeline(get_settings()).run(
        source,
        "luau_script",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        **kwargs,
    )


async def test_validation_accepts_typed_script_with_tests():
    result = await _validate(
        TYPED_MIN,
        test_cases=[{"input": {"prices": [3, 1, 2]}, "expected_output": {"min_price": 1}}],
    )
    assert result.valid, result.errors


async def test_strict_only_errors_are_warnings_not_blocking():
    # Strict errors never block `valid` — they are reported as warnings and the caller
    # (GenerationService) turns them into the nonstrict certification tier.
    result = await _validate(STRICT_ONLY)
    assert result.valid
    assert any(w.startswith("Strict-mode type errors") for w in result.warnings)
    assert any(c.name == "strict_type_check" and c.status == "warning" for c in result.checks)


async def test_nonstrict_type_errors_always_block():
    result = await _validate("function run() return { t = os.time() } end")
    assert not result.valid
    assert any(c.name == "forbidden_tokens" and c.status == "failed" for c in result.checks)


async def test_tool_outside_profile_is_a_type_error():
    source = "function run() return tools.http.get_json({ url = 'https://example.com' }) end"
    result = await _validate(source, profile="transform_only")
    assert not result.valid


# ---------------------------------------------------------------------------
# GenerationService (mock LLM)
# ---------------------------------------------------------------------------


def _stats() -> LLMCallStats:
    return LLMCallStats(model="mock", prompt_tokens=10, completion_tokens=50, total_tokens=60)


class MockLuauLLM(LLMProvider):
    """First Luau attempt only fails strict mode; the repair fixes it."""

    supports_luau = True

    def __init__(self):
        self.calls: list[str] = []
        self.repair_errors: list[str] = []

    async def generate_luau(self, goal="", **kwargs):
        self.calls.append("generate_luau")
        assert "tools.text.slugify" in "\n".join(kwargs["tool_descriptions"])
        assert "type Input" in kwargs["type_declarations"]
        return LuaGenerationResponse(
            script=STRICT_ONLY, name="min_price_luau", description=goal
        ), _stats()

    async def repair_luau(self, validation_errors=(), **kwargs):
        self.calls.append("repair_luau")
        self.repair_errors = list(validation_errors)
        return RepairResponse(repaired_content=TYPED_MIN, changes_made="typed"), _stats()

    async def generate_lua(self, **kwargs):
        raise AssertionError("lua path must not be used")

    async def repair_lua(self, **kwargs):
        raise AssertionError("lua path must not be used")

    async def analyze_intent(self, goal="", constraints=None, context=None):
        raise NotImplementedError


class MockLuaOnlyLLM(MockLuauLLM):
    supports_luau = False

    async def generate_lua(self, goal="", **kwargs):
        self.calls.append("generate_lua")
        script = "function run(input, context) return { min_price = 1 } end"
        return LuaGenerationResponse(
            script=script, name="min_price_lua", description=goal
        ), _stats()


class MockNonstrictLLM(MockLuauLLM):
    """Never reaches strict-clean: both generation and repair stay in STRICT_ONLY."""

    async def repair_luau(self, validation_errors=(), **kwargs):
        self.calls.append("repair_luau")
        self.repair_errors = list(validation_errors)
        return RepairResponse(
            repaired_content=STRICT_ONLY, changes_made="still nonstrict"
        ), _stats()


@pytest_asyncio.fixture(loop_scope="session")
async def make_client(db_session):
    async def factory(llm):
        app = create_app()

        async def override_db():
            yield db_session

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_llm_provider] = lambda: llm
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return factory


GEN_BODY = {
    "intent": "zqx_luau_minimum_price_of_list_991",
    "language": "luau",
    "save": True,
    "constraints": {"reuse_policy": "force_new"},
    "input_schema": INPUT_SCHEMA,
    "output_schema": OUTPUT_SCHEMA,
}


async def test_generation_in_luau_with_strict_repair(make_client):
    llm = MockLuauLLM()
    async with await make_client(llm) as client:
        resp = await client.post("/generate/lua", json=GEN_BODY)
    assert resp.status_code == 200, resp.text
    artifact = resp.json()["artifact"]
    assert artifact["artifact_type"] == "luau_script"
    assert artifact["content"].strip() == TYPED_MIN.strip()
    assert llm.calls == ["generate_luau", "repair_luau"]
    assert any("Strict-mode type errors" in e for e in llm.repair_errors)


async def test_generation_persists_nonstrict_when_strict_unreachable(make_client):
    # Repair never clears strict, so the loop keeps trying toward strict until
    # max_attempts, then accepts the nonstrict-valid script and persists it tagged
    # certification="nonstrict" (default require_strict_typecheck=false).
    llm = MockNonstrictLLM()
    async with await make_client(llm) as client:
        resp = await client.post(
            "/generate/lua", json={**GEN_BODY, "intent": "zqx_luau_nonstrict_persist_777"}
        )
    assert resp.status_code == 200, resp.text
    artifact = resp.json()["artifact"]
    assert artifact["artifact_id"]
    assert artifact["metadata"]["certification"] == "nonstrict"
    assert llm.calls[0] == "generate_luau"
    assert llm.calls.count("repair_luau") == get_settings().generation_max_attempts - 1


async def test_require_strict_typecheck_fails_generation(db_session):
    # With require_strict_typecheck, a script that only clears nonstrict is a
    # generation failure: validated but not accepted, so nothing is persisted.
    from sqlalchemy import func, select

    from ritesmith.core.exceptions import GenerationFailedError
    from ritesmith.core.generation import GenerationService
    from ritesmith.registry.models import Artifact as ArtifactORM
    from ritesmith.schemas.generation import GenerateScriptRequest

    settings = get_settings().model_copy(update={"require_strict_typecheck": True})
    llm = MockNonstrictLLM()
    svc = GenerationService(db=db_session, llm=llm, settings=settings)
    req = GenerateScriptRequest(
        intent="zqx_require_strict_fail_778",
        language="luau",
        save=True,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        constraints={"reuse_policy": "force_new"},
    )
    with pytest.raises(GenerationFailedError, match="No acceptable script"):
        await svc.generate_lua(req)
    count = await db_session.scalar(select(func.count()).select_from(ArtifactORM))
    assert count == 0


async def test_generation_falls_back_to_lua_when_llm_lacks_luau(make_client):
    llm = MockLuaOnlyLLM()
    async with await make_client(llm) as client:
        resp = await client.post(
            "/generate/lua", json={**GEN_BODY, "intent": "zqx_lua_fallback_minimum_993"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["artifact"]["artifact_type"] == "lua_script"
    assert llm.calls == ["generate_lua"]


# ---------------------------------------------------------------------------
# ExecutionService routing
# ---------------------------------------------------------------------------


async def test_luau_artifact_runs_on_lunardyson(db_session):
    artifact = MagicMock(artifact_id="art-luau", artifact_type="luau_script")
    version = MagicMock(
        version=1,
        risk_level="low",
        content=TYPED_MIN,
        output_schema=OUTPUT_SCHEMA,
        input_schema=INPUT_SCHEMA,
        metadata_={"runtime_profile": "transform_only"},
    )
    svc = ExecutionService(db=db_session, settings=get_settings())
    svc.registry.get_artifact_with_version = AsyncMock(return_value=(artifact, version))
    svc.lua_runtime.execute = AsyncMock(side_effect=AssertionError("lupa must not run luau_script"))

    result = await svc.create_execution(
        CreateExecutionRequest(artifact_id="art-luau", input={"prices": [5, 2, 9]})
    )
    assert result.status == ExecutionStatus.succeeded
    assert result.output == {"min_price": 2}


# ---------------------------------------------------------------------------
# P1.2: tool timeout + wall-clock budgets
# ---------------------------------------------------------------------------


def test_tool_timeout_raises_sentinel(monkeypatch):
    import time as _t

    from ritesmith.config import get_settings
    from ritesmith.runtime import luau as L

    monkeypatch.setenv("RITESMITH_LUAU_TOOL_TIMEOUT_MS", "50")
    get_settings.cache_clear()

    def slow(**kwargs):
        _t.sleep(0.4)
        return {"ok": True}

    wrapped = L._adapter(slow, None)
    with pytest.raises(TimeoutError, match="RiteSmith tool timeout"):
        wrapped({"x": 1})


def test_run_luau_maps_tool_timeout_to_timeout_not_self_heal(monkeypatch):
    from ritesmith.runtime import luau as L

    class _Stats:
        peak_memory_bytes = 123

    class _Result:
        ok = False
        error_kind = "runtime"
        message = "tools.x.y: TimeoutError: RiteSmith tool timeout: exceeded 50ms"
        stats = _Stats()

    monkeypatch.setattr(
        L,
        "_pooled_runtime",
        lambda *a, **k: type("RT", (), {"execute": lambda self, *a, **k: _Result()})(),
    )
    output, error, timed_out, _peak = L.run_luau("x", {}, {}, "transform_only", 1000, 16)
    assert output is None
    assert timed_out is True
    assert "Tool call timed out" in error
    # Must not look like a contract crash, or the self-heal would deprecate the artifact.
    assert not error.startswith(("Runtime error:", "Syntax/load error:"))


async def test_wall_clock_abandons_slow_execution(monkeypatch):
    from ritesmith.config import get_settings
    from ritesmith.runtime import luau as L

    def slow_run(*args, **kwargs):
        import time as _t

        _t.sleep(0.5)
        return ({"ok": 1}, None, False, 0)

    monkeypatch.setattr(L, "run_luau", slow_run)
    rt = L.LuauScriptRuntime(get_settings())
    rt.wall_clock_ms = 100
    result = await rt.execute("function run() return {} end", {}, {})
    assert result.timed_out
    assert "wall-clock" in result.error
