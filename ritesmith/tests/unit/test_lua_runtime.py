"""Testes do Lua runtime e sandbox."""

import pytest

from ritesmith.config import Settings
from ritesmith.runtime.lua import LuaScriptRuntime
from ritesmith.runtime.sandbox import run_in_sandbox


def make_settings(**kwargs) -> Settings:
    defaults = {
        "database_url": "postgresql+asyncpg://x:x@localhost/x",
        "lua_timeout_ms": 500,
        "lua_memory_limit_mb": 32,
    }
    defaults.update(kwargs)
    return Settings.model_validate(defaults)


# ------------------------------------------------------------------
# Testes do sandbox (síncronos — run_in_sandbox é síncrono)
# ------------------------------------------------------------------


def test_basic_execution():
    script = "function run(input, ctx) return {result = input.x * 2} end"
    output, error, timed_out = run_in_sandbox(script, {"x": 21}, {})
    assert error is None
    assert not timed_out
    assert output["result"] == 42


def test_string_manipulation():
    script = """
function run(input, ctx)
    return {upper = string.upper(input.text), len = string.len(input.text)}
end
"""
    output, error, _ = run_in_sandbox(script, {"text": "hello"}, {})
    assert error is None
    assert output["upper"] == "HELLO"
    assert output["len"] == 5


def test_forbidden_global_os_removed():
    script = "function run(input, ctx) os.execute('id') return {} end"
    _output, error, _ = run_in_sandbox(script, {}, {})
    assert error is not None
    assert "os" in error.lower() or "nil" in error.lower() or "attempt" in error.lower()


def test_forbidden_global_io_removed():
    script = "function run(input, ctx) io.open('/etc/passwd', 'r') return {} end"
    _output, error, _ = run_in_sandbox(script, {}, {})
    assert error is not None


def test_require_blocked():
    script = "require('socket') function run(input, ctx) return {} end"
    _output, error, _ = run_in_sandbox(script, {}, {})
    assert error is not None


def test_missing_run_function():
    script = "local x = 1"
    _output, error, _ = run_in_sandbox(script, {}, {})
    assert error is not None
    assert "run" in error.lower()


def test_run_returns_nil():
    script = "function run(input, ctx) return nil end"
    _output, error, _ = run_in_sandbox(script, {}, {})
    assert error is not None
    assert "nil" in error.lower()


def test_timeout_enforced():
    script = "function run(input, ctx) while true do end end"
    _output, error, timed_out = run_in_sandbox(script, {}, {}, timeout_ms=200)
    assert timed_out
    assert error is not None


def test_nested_table_output():
    script = """
function run(input, ctx)
    return {data = {a = 1, b = 2}, status = "ok"}
end
"""
    output, error, _ = run_in_sandbox(script, {}, {})
    assert error is None
    assert output["status"] == "ok"
    assert output["data"]["a"] == 1


def test_host_function_json_encode():
    script = """
function run(input, ctx)
    local encoded = json.encode({x = 1})
    return {result = encoded}
end
"""
    output, error, _ = run_in_sandbox(script, {}, {}, profile="transform_only")
    assert error is None
    assert '"x"' in output["result"] or "x" in output["result"]


def test_host_function_time_now():
    script = """
function run(input, ctx)
    local t = time.now_utc()
    return {time = t}
end
"""
    output, error, _ = run_in_sandbox(script, {}, {}, profile="transform_only")
    assert error is None
    assert "T" in output["time"]  # ISO 8601


def test_http_not_available_in_transform_only():
    script = """
function run(input, ctx)
    local r = http.request("GET", "https://example.com")
    return {status = r.status}
end
"""
    _output, error, _ = run_in_sandbox(script, {}, {}, profile="transform_only")
    # http table não existe em transform_only → LuaError
    assert error is not None


# ------------------------------------------------------------------
# Testes do LuaScriptRuntime (async)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_execute_basic():
    settings = make_settings()
    rt = LuaScriptRuntime(settings)
    result = await rt.execute(
        "function run(input, ctx) return {doubled = input.n * 2} end",
        {"n": 10},
        {},
    )
    assert result.error is None
    assert result.output["doubled"] == 20
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_async_execute_timeout():
    settings = make_settings(lua_timeout_ms=100)
    rt = LuaScriptRuntime(settings)
    result = await rt.execute(
        "function run(input, ctx) while true do end end",
        {},
        {},
        timeout_ms=100,
    )
    assert result.timed_out
    assert result.error is not None


@pytest.mark.asyncio
async def test_async_output_schema_validation():
    settings = make_settings()
    rt = LuaScriptRuntime(settings)
    output_schema = {
        "type": "object",
        "properties": {"price": {"type": "number"}},
        "required": ["price"],
    }
    # Schema satisfeito
    result = await rt.execute(
        "function run(input, ctx) return {price = 50000.0} end",
        {},
        {},
        output_schema=output_schema,
    )
    assert result.error is None

    # Schema violado (campo obrigatório ausente)
    result2 = await rt.execute(
        "function run(input, ctx) return {other = 'x'} end",
        {},
        {},
        output_schema=output_schema,
    )
    assert result2.error is not None
    assert "price" in result2.error


def test_validate_syntax_valid():
    settings = make_settings()
    rt = LuaScriptRuntime(settings)
    errors = rt.validate_syntax("function run(input, ctx) return {} end")
    assert errors == []


def test_validate_syntax_invalid():
    settings = make_settings()
    rt = LuaScriptRuntime(settings)
    errors = rt.validate_syntax("function run( this is not valid lua ~~~~")
    assert len(errors) > 0
