"""Replay paid failures without provider calls and enforce truthful acceptance."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from ritesmith.config import Settings
from ritesmith.core.exceptions import LLMError
from ritesmith.core.generation import _is_strict_clean
from ritesmith.core.pending_tests import finish_pending_tests
from ritesmith.llm import prompts
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.llm.response_contracts import decode_response, response_model
from ritesmith.llm.script_candidate import ScriptCandidateError, expand_script
from ritesmith.registry.models import Artifact as ArtifactORM
from ritesmith.registry.models import GenerationJob
from ritesmith.runtime.lua import LuaScriptRuntime
from ritesmith.schemas.artifact import ValidationCheck, ValidationResult
from ritesmith.tests.fakes.llm import ScriptedLLM
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.semantic_validation import reference_contract_errors
from ritesmith.workflows.simulation import UnsupportedSimulation, logic
from ritesmith.workflows.validator import WorkflowValidator

EVIDENCE = (
    Path(__file__).resolve().parents[3]
    / "benchmarks/generation_latency/results/typed-decisions-20261008/spending.json"
)
RAW_CALLS = json.loads(EVIDENCE.read_text())["calls"]


def scripts():
    cases = []
    for call in RAW_CALLS:
        if call["status"] != "settled":
            continue
        raw = call["response_content"]
        if call["cell"].endswith(":B"):
            raw = decode_response(raw, response_model(call["method"]))
        data = json.loads(raw)
        script = data.get("script")
        if isinstance(script, dict) and "script" in script:
            data = script
        if isinstance(data.get("script"), dict):
            cases.append((hashlib.sha256(call["response_content"].encode()).hexdigest(), data))
    return cases


@pytest.mark.parametrize(
    "sha,candidate", scripts(), ids=lambda item: item[:12] if isinstance(item, str) else None
)
def test_preserved_run_redeclarations_are_rejected_with_field_diagnostics(sha, candidate):
    assert len(sha) == 64
    with pytest.raises(ScriptCandidateError) as exc:
        expand_script(candidate)
    assert exc.value.location == "script.body"


def test_body_prompts_do_not_require_a_function_inside_body():
    system = prompts.luau_generation_system(True)
    user = prompts.luau_generation_user(
        "Convert Celsius", {}, {}, [], "type Input = {}", [], {}, "{}", assembled=True
    )
    assert "annotate the entry point exactly as" not in system
    assert "Annotate the entry point exactly as" not in user
    assert "statements only" in system and "statements only" in user
    assert "Define exactly ONE global function" in prompts.luau_generation_system()
    assert "Keep the entry point" in prompts.luau_repair_system()


def test_lua_syntax_check_cannot_execute_top_level_code():
    assert LuaScriptRuntime(Settings()).validate_syntax("error('must never run')") == []


def test_strict_certification_requires_a_passed_check():
    assert not _is_strict_clean(ValidationResult(valid=True, checks=[]))
    assert not _is_strict_clean(
        ValidationResult(
            valid=True, checks=[ValidationCheck(name="strict_type_check", status="warning")]
        )
    )
    assert _is_strict_clean(
        ValidationResult(
            valid=True, checks=[ValidationCheck(name="strict_type_check", status="passed")]
        )
    )


@pytest.mark.asyncio
async def test_pending_independent_tests_are_drained_without_another_call():
    marker = []

    async def pending():
        await asyncio.sleep(0.01)
        marker.append("settled")

    task = asyncio.create_task(pending())
    await finish_pending_tests(task)
    assert marker == ["settled"] and not task.cancelled()


def test_unknown_operators_and_business_var_shorthand_fail_before_execution():
    for condition in (
        {"greater": [{"ref": "price", "path": "price"}, 100]},
        {">": [{"var": "price.price"}, 100]},
    ):
        with pytest.raises(ValueError):
            compile_plan(
                {"name": "bad", "steps": [{"kind": "condition", "when": condition, "steps": []}]},
                "http://test",
            )


def test_supported_but_unsimulated_expression_is_not_claimed_invalid():
    with pytest.raises(UnsupportedSimulation):
        logic({"cat": ["a", "b"]}, {})
    assert not WorkflowValidator().validate(
        {
            "name": "cat",
            "entrypoint": "check",
            "nodes": [
                {
                    "id": "check",
                    "kind": "switch",
                    "cases": [{"when": {"cat": ["a", "b"]}, "target": "end"}],
                    "default": "end",
                }
            ],
        }
    )


def test_wrong_join_output_envelope_is_rejected_against_capability_contract():
    from ritesmith.workflows.constructors import semantic_example

    graph = compile_plan(semantic_example("parallel"), "http://test")
    registry = {
        "market.coin_price": {
            "output_schema": {
                "type": "object",
                "properties": {"price": {"type": "number"}},
                "additionalProperties": False,
            }
        }
    }
    assert not reference_contract_errors(graph, registry)
    notify = next(node for node in graph["nodes"] if node["id"] == "notify")
    notify["action"]["request"]["body"]["input"]["text"] = (
        "{{ nodes.parallel_join.response.body.branches[0].result.price }}"
    )
    assert any("result.output" in error for error in reference_contract_errors(graph, registry))


@pytest.mark.asyncio
async def test_provider_preserves_raw_script_candidate_and_location():
    raw = json.dumps(
        {
            "script": {"format": "luau_body", "body": "function run() end"},
            "name": "bad",
            "description": "bad",
        }
    )
    from ritesmith.llm.base import LLMCallStats

    provider = OpenAIProvider(Settings(), client=SimpleNamespace())
    provider._chat_with_retry = AsyncMock(return_value=(raw, LLMCallStats(model="mock")))
    with pytest.raises(LLMError) as exc:
        await provider.generate_luau("bad", None, None, [], "", [], {})
    assert exc.value.details["candidate"] == raw
    assert exc.value.details["location"] == "script.body"


@pytest.mark.asyncio
@pytest.mark.parametrize("save", [False, True])
async def test_exhausted_script_returns_422_and_retains_failed_attempts(
    make_client, db_session, save
):
    error = LLMError(
        "script.body: repeated run",
        details={
            "candidate": '{"script":{"body":"function run() end"}}',
            "location": "script.body",
        },
    )
    llm = ScriptedLLM(supports_luau=True).queue("generate_luau", error, error)
    async with make_client(
        llm=llm, settings=Settings(generation_luau_assembly=True, require_tests_min_risk="off")
    ) as client:
        response = await client.post(
            "/generate/lua",
            json={
                "intent": "Convert Celsius",
                "save": save,
                "constraints": {"reuse_policy": "force_new"},
            },
        )
    assert response.status_code == 422 and response.json()["error"] == "generation_failed"
    assert "artifact" not in response.json()
    assert await db_session.scalar(select(func.count()).select_from(ArtifactORM)) == 0
    assert (await db_session.scalar(select(GenerationJob))).status == "failed"
    assert len(llm.called("generate_luau")) == 2
    assert (
        llm.called("generate_luau")[1]["constraints"]["generation_repair"]["location"]
        == "script.body"
    )


def test_entrypoint_count_ignores_literals_and_rejects_real_duplicates():
    from ritesmith.core.validation import ValidationPipeline

    pipeline = ValidationPipeline(Settings())
    source = 'function run(input, context) return {text="function run()"} end -- function run()'
    assert pipeline._check_schema_presence(source)[0].status == "passed"
    assert pipeline._check_schema_presence(source + "\nfunction run() end")[0].status == "failed"


def test_parallel_text_assembly_produces_a_string_and_container_contracts_still_apply():
    from ritesmith.workflows.constructors import semantic_example
    from ritesmith.workflows.semantic_validation import validate_semantics

    graph = compile_plan(semantic_example("parallel"), "http://test")
    notify = next(node for node in graph["nodes"] if node["id"] == "notify")
    args = notify["action"]["request"]["body"]["input"]
    assert isinstance(args["text"], str) and "result.output.price" in args["text"]
    args["text"] = {"type": "template", "parts": ["{{ nodes.fetch.response.body.output.price }}"]}
    errors = validate_semantics(
        graph,
        {
            "telegram.send": {
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                }
            }
        },
    )
    assert any("not of type 'string'" in error for error in errors)


def test_native_transport_does_not_claim_typed_json_or_indexed_mustache_support():
    from ritesmith.workflows.simulation import render

    context = {
        "nodes": {
            "join": {
                "response": {"body": {"branches": [{"result": {"output": {"price": 111111}}}]}}
            }
        }
    }
    with pytest.raises(UnsupportedSimulation, match="indexed list"):
        render("{{ nodes.join.response.body.branches.0.result.output.price }}", context, True)
    with pytest.raises(UnsupportedSimulation, match="not typed JSON"):
        render("{{ nodes.join.response.body.branches }}", context, True)
    assert render("{{ payload.value }}", {"payload": {"value": 12}}, True) == "12"
    assert render("{{ payload.absent }}", {"payload": {}}, True) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/generate/trama-workflow", "/generate", "/plans"])
async def test_failed_workflow_is_not_a_successful_artifact_or_ready_plan(
    make_client, db_session, endpoint
):
    from ritesmith.tests.fakes.llm import intent

    llm = ScriptedLLM().queue(
        "analyze_intent", intent(artifact_types=["trama_workflow"], requires_workflow=True)
    )
    llm.queue("generate_workflow", LLMError("invalid graph"), LLMError("invalid graph"))
    body = {
        "intent": "invalid workflow failure unique",
        "context": {},
        "constraints": {"reuse_policy": "force_new"},
    }
    if endpoint == "/plans":
        body["requested_artifact_types"] = ["trama_workflow"]
        body["reuse_policy"] = "force_new"
        body["constraints"].pop("reuse_policy")
    async with make_client(llm=llm) as client:
        response = await client.post(endpoint, json=body)
    assert response.status_code == 422 and response.json()["error"] == "generation_failed"
    assert await db_session.scalar(select(func.count()).select_from(ArtifactORM)) == 0


@pytest.mark.asyncio
async def test_mcp_generation_failure_preserves_the_422_diagnostic_envelope(monkeypatch):
    import httpx

    from ritesmith.api import mcp_handler

    envelope = {
        "error": "generation_failed",
        "message": "No candidate",
        "details": {"diagnostics": ["script.body: run redeclared"]},
    }
    response = httpx.Response(
        422, json=envelope, request=httpx.Request("POST", "http://test/generate")
    )
    monkeypatch.setattr(
        mcp_handler,
        "_post",
        AsyncMock(
            side_effect=httpx.HTTPStatusError("failed", request=response.request, response=response)
        ),
    )
    result = await mcp_handler._call_tool("ritesmith_generate", {"intent": "test"})
    assert result.isError and json.loads(result.content[0].text) == envelope


def test_progressive_matrix_and_twelve_eligible_notification_fixtures_are_fixed():
    from collections import Counter

    from benchmarks.generation_latency.reliability import (
        STAGE_1,
        STAGE_2,
        hidden_cases,
        notification_fixtures,
    )
    from benchmarks.generation_latency.tasks import BY_ID

    assert len(STAGE_1) == 3 and len(STAGE_2) == 24
    groups = Counter(
        "plan" if endpoint == "plans" else BY_ID[task]["kind"] for task, endpoint, _ in STAGE_2
    )
    assert groups == {"luau_script": 8, "trama_workflow": 13, "plan": 3}
    listing = next(f for f in notification_fixtures() if f["args"] == {"segment": "many"})
    assert sum(c["last_seen_days"] >= 30 for c in listing["output"]["customers"]) >= 12
    limit_case = hidden_cases(BY_ID["t3_notify_inactive"])[-1]
    assert limit_case["expected_calls"]["message.send"] == 10
    assert limit_case["expected_output"]["count"] == 9


@pytest.mark.asyncio
async def test_failed_offline_qualification_freezes_both_stages_without_paid_calls(
    tmp_path, monkeypatch
):
    from benchmarks.generation_latency import reliability

    frozen = {
        "source_sha256": "fixture",
        "stages": [
            {"stage": 1, "max_requests": 3, "cells": [{"cell": f"stage1:{i}"} for i in range(3)]},
            {"stage": 2, "max_requests": 24, "cells": [{"cell": f"stage2:{i}"} for i in range(24)]},
        ],
    }
    monkeypatch.setattr(reliability, "manifest", lambda: frozen)
    (tmp_path / "verification.json").write_text(
        json.dumps(
            {"passed": False, "source_sha256": "fixture", "reason": "native_transport_mismatch"}
        )
    )
    await reliability.run(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["stages"][0]["executed"] == report["stages"][1]["executed"] == 0
    assert report["recommendation"] == "retain_restored_production"
    assert not (tmp_path / "stage1-spending.json").exists()
    with pytest.raises(RuntimeError, match="resubmission"):
        await reliability.run(tmp_path)


def test_notification_limit_counts_failed_attempts_with_twelve_eligible_customers(monkeypatch):
    from benchmarks.generation_latency.reliability import hidden_cases
    from benchmarks.generation_latency.tasks import BY_ID
    from ritesmith.runtime.providers.base import HostFunctionDef
    from ritesmith.runtime.validation_session import LuauValidationSession, preflight

    task = BY_ID["t3_notify_inactive"]
    tools = {
        tool["name"]: HostFunctionDef(
            tool["name"],
            "notification",
            lambda **args: (_ for _ in ()).throw(AssertionError("Live tool forbidden")),
        )
        for tool in task["tools"]
    }
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.luau_tools_for_profile", lambda profile: tools
    )
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.script_type_declarations",
        lambda *args: "type Context = {[string]: any}\n" + task["types"],
    )
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.tool_signature",
        lambda name, definition: (
            next(tool["signature"] for tool in task["tools"] if tool["name"] == name),
            None,
        ),
    )
    body = """local listing = tools.crm.list({segment = input.segment})
if not listing.ok then return {error = listing.error, message = listing.message} end
local notified: {string} = {}
local failed: {string} = {}
local attempts = 0
for _, customer in ipairs(listing.customers) do
    if attempts >= 10 then break end
    if customer.last_seen_days >= input.inactive_days then
        attempts += 1
        local sent = tools.message.send({user = customer.id, text = "Hi " .. customer.name .. ", we miss you!"})
        if sent.ok then table.insert(notified, customer.id)
        else table.insert(failed, customer.id) end
    end
end
return {notified = notified, failed = failed, count = #notified}"""
    script = expand_script({"script": {"format": "luau_body", "body": body}})["script"]
    session = LuauValidationSession(
        Settings(), "notification", task["input_schema"], task["output_schema"]
    )
    try:
        assert not session.check(script)["strict"]
        for spec in preflight(
            hidden_cases(task), tools, task["input_schema"], task["output_schema"]
        ):
            result, fixture_error, errors = session.execute(script, spec)
            assert result.ok and not fixture_error and not errors
        bad = script.replace("attempts += 1", "").replace(
            "if sent.ok then table.insert", "if sent.ok then attempts += 1; table.insert"
        )
        spec = preflight(
            hidden_cases(task)[-1:], tools, task["input_schema"], task["output_schema"]
        )[0]
        _, _, errors = session.execute(bad, spec)
        assert any("expected 10 calls" in error for error in errors)
    finally:
        session.close()
