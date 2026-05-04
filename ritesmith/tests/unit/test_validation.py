"""Testes da ValidationPipeline e do endpoint POST /validate."""
import pytest

from ritesmith.config import Settings
from ritesmith.core.validation import ValidationPipeline


def make_settings(**kwargs) -> Settings:
    defaults = {
        "database_url": "postgresql+asyncpg://x:x@localhost/x",
        "lua_timeout_ms": 500,
    }
    defaults.update(kwargs)
    return Settings.model_validate(defaults)


@pytest.mark.asyncio
async def test_valid_lua_script_passes():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run(
        "function run(input, ctx) return {result = input.x} end",
        "lua_script",
    )
    assert result.valid is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_forbidden_require_fails():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run(
        "require('socket')\nfunction run(input, ctx) return {} end",
        "lua_script",
    )
    assert result.valid is False
    assert any("require" in e for e in result.errors)


@pytest.mark.asyncio
async def test_forbidden_os_fails():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run(
        "function run(input, ctx) os.execute('id') return {} end",
        "lua_script",
    )
    assert result.valid is False
    assert any("os" in e for e in result.errors)


@pytest.mark.asyncio
async def test_size_limit_lines_fails():
    # Gera script com 70 linhas
    lines = ["-- comment"] * 68 + ["function run(input, ctx) return {} end", ""]
    script = "\n".join(lines)
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run(script, "lua_script")
    assert result.valid is False
    assert any("linhas" in e or "lines" in e.lower() for e in result.errors)


@pytest.mark.asyncio
async def test_missing_run_function_fails():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run("local x = 42", "lua_script")
    assert result.valid is False
    assert any("run" in e for e in result.errors)


@pytest.mark.asyncio
async def test_http_in_transform_only_fails():
    pipeline = ValidationPipeline(make_settings())
    script = """
function run(input, ctx)
    local r = http.request("GET", "https://api.example.com")
    return {data = r.body}
end
"""
    result = await pipeline.run(script, "lua_script", profile="transform_only")
    assert result.valid is False
    assert any("http" in e for e in result.errors)


@pytest.mark.asyncio
async def test_http_in_readonly_network_passes():
    pipeline = ValidationPipeline(make_settings())
    script = """
function run(input, ctx)
    local r = http.request("GET", "https://api.example.com")
    return {status = r.status}
end
"""
    result = await pipeline.run(script, "lua_script", profile="readonly_network")
    # http.request é permitido em readonly_network
    allowed_primitives_check = next(
        (c for c in result.checks if c.name == "allowed_primitives"), None
    )
    assert allowed_primitives_check is not None
    assert allowed_primitives_check.status == "passed"


@pytest.mark.asyncio
async def test_syntax_error_fails():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run("function run( invalid ~~~", "lua_script")
    assert result.valid is False
    assert any(c.name == "syntax" and c.status == "failed" for c in result.checks)


@pytest.mark.asyncio
async def test_json_workflow_valid():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run('{"steps": []}', "trama_workflow")
    assert result.valid is True


@pytest.mark.asyncio
async def test_json_workflow_invalid():
    pipeline = ValidationPipeline(make_settings())
    result = await pipeline.run("not json {{{{", "trama_workflow")
    assert result.valid is False


@pytest.mark.asyncio
async def test_test_cases_pass():
    pipeline = ValidationPipeline(make_settings())
    script = "function run(input, ctx) return {doubled = input.n * 2} end"
    test_cases = [
        {"input": {"n": 5}, "expected_output": {"doubled": 10}},
        {"input": {"n": 0}, "expected_output": {"doubled": 0}},
    ]
    result = await pipeline.run(script, "lua_script", test_cases=test_cases)
    assert result.valid is True
    assert all(c.status == "passed" for c in result.checks if c.name.startswith("test_case_"))


@pytest.mark.asyncio
async def test_test_cases_fail():
    pipeline = ValidationPipeline(make_settings())
    script = "function run(input, ctx) return {doubled = input.n * 3} end"  # bug: *3 not *2
    test_cases = [
        {"input": {"n": 5}, "expected_output": {"doubled": 10}},
    ]
    result = await pipeline.run(script, "lua_script", test_cases=test_cases)
    assert result.valid is False
    assert any(c.name == "test_case_0" and c.status == "failed" for c in result.checks)


# ------------------------------------------------------------------
# Testes da rota POST /validate
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_endpoint_valid(client):
    response = await client.post("/validate", json={
        "content": "function run(input, ctx) return {ok = true} end",
        "artifact_type": "lua_script",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert isinstance(body["checks"], list)
    assert len(body["checks"]) > 0


@pytest.mark.asyncio
async def test_validate_endpoint_forbidden_token(client):
    response = await client.post("/validate", json={
        "content": "require('os')\nfunction run(i, c) return {} end",
        "artifact_type": "lua_script",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert len(body["errors"]) > 0


@pytest.mark.asyncio
async def test_validate_endpoint_workflow(client):
    response = await client.post("/validate", json={
        "content": '{"name": "test_wf", "steps": []}',
        "artifact_type": "trama_workflow",
    })
    assert response.status_code == 200
    assert response.json()["valid"] is True
