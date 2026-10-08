"""Offline regressions for bounded recovery, fixture oracles and typed generation."""

import asyncio
import json
from time import monotonic
from unittest.mock import AsyncMock

import pytest

from ritesmith.config import Settings
from ritesmith.core.exceptions import GenerationFailedError, LLMTimeoutError
from ritesmith.core.generation_budget import GenerationBudget, bounded_generation, current_budget
from ritesmith.core.repair import run_repair_loop
from ritesmith.core.validation import ValidationPipeline
from ritesmith.llm.base import LLMCallStats
from ritesmith.llm.generation_context import prepare_catalog, proposal_kind
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.llm.script_candidate import expand_script
from ritesmith.runtime.luau import luau_available
from ritesmith.runtime.providers.base import HostFunctionDef
from ritesmith.runtime.validation_session import FixtureError, LuauValidationSession, preflight
from ritesmith.schemas.test_spec import functional_tests
from ritesmith.workflows.constructors import semantic_example
from ritesmith.workflows.examples import EXAMPLE_IDS, example
from ritesmith.workflows.mermaid import MermaidError, parse_mermaid, render_mermaid
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.validator import WorkflowValidator

BASE = "http://ritesmith:8081"


@pytest.mark.parametrize("pattern", EXAMPLE_IDS)
def test_semantic_patterns_compile_to_valid_native_graphs(pattern):
    plan = semantic_example(pattern)
    native = compile_plan(plan, BASE)
    assert not WorkflowValidator().validate(native)
    assert all("action" in node for node in native["nodes"] if node["kind"] == "task")
    assert plan == semantic_example(pattern)


def test_parallel_results_are_read_after_join_and_branches_end_locally():
    definition = compile_plan(semantic_example("parallel"), BASE)
    nodes = {node["id"]: node for node in definition["nodes"]}
    assert nodes["fetch"]["next"] == nodes["eth"]["next"] == "end"
    assert "nodes.parallel_join.response.body.branches.0.result.output.price" in json.dumps(
        nodes["notify"]
    )


def test_branch_cannot_read_parent_results():
    plan = {
        "name": "bad",
        "steps": [
            {
                "kind": "call",
                "id": "parent",
                "capability_name": "market.coin_price",
                "args": {"symbol": "btc"},
            },
            {
                "kind": "parallel",
                "id": "fork",
                "branches": [
                    {
                        "kind": "call",
                        "id": "a",
                        "capability_name": "telegram.send",
                        "args": {"text": {"ref": "parent", "path": "price"}},
                    },
                    {"kind": "wait", "seconds": 1},
                ],
            },
        ],
    }
    with pytest.raises(ValueError, match="inaccessible"):
        compile_plan(plan, BASE)


def test_finite_continuation_decrements_instead_of_restarting_duration():
    plan = {
        "name": "samples",
        "steps": [
            {
                "kind": "repeat",
                "id": "samples",
                "count": 28,
                "interval_seconds": 60,
                "steps": [
                    {
                        "kind": "call",
                        "id": "fetch",
                        "capability_name": "market.coin_price",
                        "args": {"symbol": "btc"},
                    }
                ],
            }
        ],
    }
    first = compile_plan(plan, BASE, contract_version="v1")
    assert first["max_iterations"] == 20
    second = compile_plan(
        plan,
        BASE,
        contract_version="v1",
        continuation={
            "remaining_iterations": 8,
            "state": {"previous": 17},
            "_resume_repeat": "samples",
        },
    )
    assert second["max_iterations"] == 8
    tick = next(node for node in second["nodes"] if node["id"] == "samples_tick")
    assert tick["action"]["request"]["body"]["input"]["initial_remaining"] == 8
    assert not any(node["id"] == "samples_continue" for node in second["nodes"])


def test_assembly_preserves_helpers_and_supplies_strict_entrypoint():
    candidate = expand_script(
        {
            "script": {
                "format": "luau_body",
                "helpers": "local function double(x: number): number return x * 2 end",
                "body": "return {result = double(input.value)}",
            }
        }
    )
    assert "function run(input: Input, context: Context): Output" in candidate["script"]
    assert candidate["script"].startswith("local function double")
    with pytest.raises(ValueError, match="redeclare"):
        expand_script({"script": {"format": "luau_body", "body": "function run() end"}})


@pytest.mark.parametrize(
    ("goal", "schemas", "kind"),
    [
        ("Write a Luau function", False, "script"),
        ("Monitor prices every minute", False, "workflow"),
        ("Write a script and schedule every minute", False, "mixed"),
        ("Calculate", True, "script"),
        ("Help with data", False, "mixed"),
    ],
)
def test_local_routing_keeps_composed_requests(goal, schemas, kind):
    assert proposal_kind(goal, {"type": "object"} if schemas else None) == kind


def test_catalog_keeps_inventory_and_explicit_contracts_and_falls_back_on_ambiguity():
    capabilities = [
        {
            "capability_name": f"tool.{i}",
            "description": "Tool",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
        }
        for i in range(40)
    ]
    selected = prepare_catalog(
        "Use tool.35", capabilities + [capabilities[0]], required=["tool.39"]
    )
    assert len(selected.inventory) == 40
    assert {c["capability_name"] for c in selected.contracts} == {"tool.35", "tool.39"}
    assert len(prepare_catalog("do something", capabilities).contracts) == 40


@pytest.mark.asyncio
async def test_workflow_prompt_does_not_include_luau_rules():
    provider = OpenAIProvider(Settings(generation_semantic_workflows=True), client=object())
    response = {
        "analysis": {"artifact_types": ["trama_workflow"]},
        "workflow": {
            "name": "monitor",
            "description": "monitor",
            "definition": {"format": "semantic_plan", "plan": semantic_example("linear")},
        },
    }
    provider._chat_with_retry = AsyncMock(
        return_value=(json.dumps(response), LLMCallStats(model="test"))
    )
    proposal, _ = await provider.propose(goal="Monitor prices every minute", constraints={})
    system = provider._chat_with_retry.call_args.args[1]
    assert "function run" not in system
    assert "semantic_plan" in system
    assert not WorkflowValidator().validate(proposal.workflow.definition)


@pytest.mark.asyncio
async def test_transport_retry_consumes_artifact_recovery_allowance():
    provider = OpenAIProvider(Settings(), client=object())
    provider._chat = AsyncMock(
        side_effect=[LLMTimeoutError("temporary"), ("{}", LLMCallStats(model="test"))]
    )
    calls = []

    async def generate(attempt):
        calls.append(attempt)
        await provider._chat_with_retry("gpt-5-mini", "system", "user", method="luau_gen")
        return False

    performed = await run_repair_loop("luau_script", 5, generate, settings=Settings())
    assert calls == [1] and performed == 1
    assert provider._chat.await_count == 2


@pytest.mark.asyncio
async def test_deadline_is_shared_by_nested_services():
    class Service:
        settings = Settings(generation_deadline_seconds=0.02)

        @bounded_generation
        async def inner(self):
            await asyncio.sleep(0.1)

        @bounded_generation
        async def outer(self):
            return await self.inner()

    with pytest.raises(GenerationFailedError, match="deadline"):
        await Service().outer()
    assert current_budget.get() is None
    budget = GenerationBudget(monotonic() + 1, minimum_recovery_seconds=3)
    assert not budget.recover("script")


def test_input_only_cases_do_not_certify_functionality():
    assert not functional_tests([{"input": {"value": 1}}])
    assert functional_tests(
        [{"input": {}, "assertions": [{"path": "count", "op": "equals", "value": 0}]}]
    )


INPUT = {"type": "object", "required": ["segment"], "properties": {"segment": {"type": "string"}}}
TOOL_OUTPUT = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
        }
    },
}
OUTPUT = {
    "type": "object",
    "required": ["notified", "failed", "count"],
    "properties": {
        "notified": {"type": "array", "items": {"type": "string"}},
        "failed": {"type": "array", "items": {"type": "string"}},
        "count": {"type": "integer"},
    },
}
SCRIPT = """function run(input: Input, context: Context): Output
local result = tools.crm.list({segment = input.segment})
return {notified = {result.items[1].id}, failed = {}, count = 1}
end"""


def crm_tools():
    def never_live(**kwargs):
        raise AssertionError("Live tools must never execute")

    return {
        "crm.list": HostFunctionDef(
            "crm.list", "notification", never_live, input_schema=INPUT, output_schema=TOOL_OUTPUT
        )
    }


def case(segment="dormant", id_="c1"):
    return {
        "input": {"segment": segment},
        "source": "intent",
        "expected_output": {"notified": [id_], "failed": [], "count": 1},
        "tool_fixtures": [
            {"tool": "crm.list", "args": {"segment": segment}, "output": {"items": [{"id": id_}]}}
        ],
        "expected_calls": {"crm.list": 1},
    }


def test_generated_oracle_cannot_invent_customers_absent_from_fixture():
    invalid = case()
    invalid["expected_output"]["notified"] = ["invented"]
    with pytest.raises(FixtureError, match="absent"):
        preflight([invalid], crm_tools(), INPUT, OUTPUT)


@pytest.mark.skipif(not luau_available(), reason="lunardyson not installed")
def test_lunardyson_fixture_session_isolates_cases_and_never_calls_live_tools(monkeypatch):
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.luau_tools_for_profile", lambda profile: crm_tools()
    )
    session = LuauValidationSession(Settings(), "notification", INPUT, OUTPUT)
    try:
        assert not session.check(SCRIPT)["strict"]
        for spec in preflight([case(), case("new", "c2")], session.tools, INPUT, OUTPUT):
            result, fixture_error, errors = session.execute(SCRIPT, spec)
            assert result.ok and not fixture_error and not errors
            assert session.counts["crm.list"] == 1
        missing = preflight(
            [{**case(), "tool_fixtures": [], "source": "client"}], session.tools, INPUT, OUTPUT
        )[0]
        _, fixture_error, _ = session.execute(SCRIPT, missing)
        assert "Missing fixture" in fixture_error
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not luau_available(), reason="lunardyson not installed")
async def test_fixture_failure_is_nonrepairable_validation_diagnostic(monkeypatch):
    from ritesmith.core.diagnostics import validation_diagnostics

    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.luau_tools_for_profile", lambda profile: crm_tools()
    )
    monkeypatch.setattr(
        "ritesmith.runtime.luau.luau_tools_for_profile", lambda profile: crm_tools()
    )
    invalid = case()
    invalid["expected_output"]["notified"] = ["invented"]
    result = await ValidationPipeline(Settings()).run(
        SCRIPT,
        "luau_script",
        profile="notification",
        input_schema=INPUT,
        output_schema=OUTPUT,
        test_cases=[invalid],
    )
    assert not result.valid
    diagnostics = validation_diagnostics(result)
    assert any(d.category == "fixture" and not d.repairable for d in diagnostics)


def test_mermaid_accepts_unquoted_visual_labels_and_reports_line():
    graph = render_mermaid(example("linear"))
    assert parse_mermaid(graph.replace('["task"]', "[task]")) == example("linear") | {
        "version": "2.0.0"
    }
    with pytest.raises(MermaidError) as error:
        parse_mermaid(graph + "\nunsupported junk")
    assert error.value.line is not None


@pytest.mark.parametrize("pattern", ["bounded_polling", "fixed_samples", "state_tracking"])
def test_offline_simulation_preserves_counts_and_sample_state(pattern):
    from ritesmith.workflows.simulation import WorkflowSimulator

    count = 2 if pattern == "fixed_samples" else 3
    definition = compile_plan(semantic_example(pattern), BASE)
    result = WorkflowSimulator(
        definition, {"market.coin_price": [{"price": 8}, {"price": 4}, {"price": 6}]}
    ).run()
    assert result.calls["market.coin_price"] == count
    assert result.clock == (count - 1) * 60
    if pattern == "fixed_samples":
        assert result.nodes["samples"]["response"]["body"]["output"]["samples"] == [
            {"price": 8},
            {"price": 4},
        ]
    if pattern == "state_tracking":
        assert result.nodes["minimum"]["response"]["body"]["output"]["min_value"] == 4


def test_offline_finite_chain_executes_exactly_requested_samples_across_chunks():
    from ritesmith.workflows.simulation import WorkflowSimulator

    plan = {
        "name": "samples",
        "steps": [
            {
                "kind": "repeat",
                "id": "samples",
                "count": 28,
                "interval_seconds": 60,
                "steps": [
                    {
                        "kind": "call",
                        "id": "fetch",
                        "capability_name": "market.coin_price",
                        "args": {"symbol": "btc"},
                    }
                ],
            }
        ],
    }
    first = WorkflowSimulator(
        compile_plan(plan, BASE, contract_version="contracts", intent="Collect 28 prices"),
        {"market.coin_price": {"price": 10}},
    ).run()
    assert first.calls["market.coin_price"] == 20
    assert first.continuations[0]["intent"] == "Collect 28 prices"
    state = first.continuations[0]["context"]["continuation"]
    assert state["remaining_iterations"] == 8
    second = WorkflowSimulator(
        compile_plan(plan, BASE, continuation=state),
        {"market.coin_price": {"price": 11}},
        now=first.clock,
    ).run()
    assert second.calls["market.coin_price"] == 8
    assert not second.continuations
    assert second.clock == 27 * 60


def test_offline_duration_keeps_absolute_deadline_across_continuation():
    from ritesmith.workflows.simulation import WorkflowSimulator

    plan = {
        "name": "weekly",
        "steps": [
            {
                "kind": "repeat",
                "id": "monitor",
                "duration_seconds": 28 * 60,
                "interval_seconds": 60,
                "steps": [
                    {
                        "kind": "call",
                        "id": "fetch",
                        "capability_name": "market.coin_price",
                        "args": {"symbol": "btc"},
                    }
                ],
            }
        ],
    }
    first = WorkflowSimulator(
        compile_plan(plan, BASE), {"market.coin_price": {"price": 10}}, now=1000
    ).run()
    state = first.continuations[0]["context"]["continuation"]
    assert state["deadline_unix"] == 1000 + 28 * 60
    second = WorkflowSimulator(
        compile_plan(plan, BASE, continuation=state),
        {"market.coin_price": {"price": 10}},
        now=state["deadline_unix"] + 1,
    ).run()
    assert second.calls["market.coin_price"] == 0


def test_callback_and_compensation_are_checked_with_controlled_responses():
    from ritesmith.workflows.simulation import WorkflowSimulator

    callback = compile_plan(semantic_example("async_callback"), BASE)
    WorkflowSimulator(callback, {"callback": {"status": "APPROVED"}}).run()
    with pytest.raises(ValueError, match="successWhen"):
        WorkflowSimulator(callback, {"callback": {"status": "DENIED"}}).run()
    simulator = WorkflowSimulator(
        compile_plan(semantic_example("compensation"), BASE), {"order": {"error": "failed"}}
    )
    with pytest.raises(ValueError, match="Task failed"):
        simulator.run()
    assert simulator.result.compensations[0]["url"].endswith("/rollback")


def test_content_monitor_notifies_only_changed_snapshots():
    from ritesmith.workflows.simulation import WorkflowSimulator

    result = WorkflowSimulator(
        compile_plan(semantic_example("content_monitor"), BASE),
        {
            "web.search": {"result": ["one"]},
            "llm.evaluate": [
                {"decision": "skip"},
                {"decision": "notify", "message": "new"},
                {"decision": "skip"},
            ],
            "telegram.send": {"ok": True},
        },
    ).run()
    assert result.calls["telegram.send"] == 1
    graph = compile_plan(semantic_example("content_monitor"), BASE)
    notify = next(node for node in graph["nodes"] if node["id"] == "notify")
    assert notify["action"]["request"]["body"]["input"]["text"].endswith("output.message }}")
    assert result.nodes["monitor_tick"]["response"]["body"]["output"]["state"] == {
        "previous": ["one"]
    }


@pytest.mark.asyncio
async def test_semantic_continuation_materializes_without_a_model_call(db_session):
    from ritesmith.core.workflow_generation import WorkflowGenerationService
    from ritesmith.runtime.host_functions import get_available_provider_capabilities
    from ritesmith.schemas.generation import GenerateWorkflowRequest
    from ritesmith.tests.fakes.llm import ScriptedLLM

    llm = ScriptedLLM()
    settings = Settings(generation_semantic_workflows=True)
    service = WorkflowGenerationService(db_session, llm, settings)
    capabilities = get_available_provider_capabilities() + await service._get_capabilities()
    plan = {
        "name": "wait",
        "steps": [
            {
                "kind": "repeat",
                "id": "waits",
                "count": 28,
                "interval_seconds": 1,
                "steps": [{"kind": "wait", "seconds": 1}],
            }
        ],
    }
    definition = compile_plan(
        plan, BASE, contract_version=prepare_catalog("wait", capabilities).version, intent="wait"
    )
    from ritesmith.workflows.simulation import WorkflowSimulator

    request = WorkflowSimulator(definition).run().continuations[0]
    response = await service.generate_workflow(
        GenerateWorkflowRequest(intent="wait", context=request["context"])
    )
    assert response.validation.valid and llm.calls == []
    assert json.loads(response.artifact.content)["max_iterations"] == 8


def test_known_fixtures_override_synthetic_tool_data():
    from ritesmith.schemas.test_spec import bind_known_fixtures

    generated = case(id_="invented")
    bound = bind_known_fixtures([generated], case()["tool_fixtures"])
    with pytest.raises(FixtureError, match="absent"):
        preflight(bound, crm_tools(), INPUT, OUTPUT)


def test_notification_oracle_must_have_consistent_counts():
    invalid = case()
    invalid["expected_output"]["count"] = 99
    with pytest.raises(FixtureError, match="count contradicts"):
        preflight([invalid], crm_tools(), INPUT, OUTPUT)


def test_mermaid_edge_diagnostic_retains_line_node_and_edge():
    graph = render_mermaid(example("linear"))
    with pytest.raises(MermaidError) as error:
        parse_mermaid(graph + "\n  fetch --> missing")
    assert error.value.node == "fetch"
    assert error.value.edge == ("fetch", "", "missing")
    assert error.value.line == len(graph.splitlines()) + 1


def test_literal_contract_failure_and_undefined_switch_reference_are_rejected():
    from ritesmith.workflows.semantic_validation import validate_semantics

    graph = compile_plan(semantic_example("linear"), BASE)
    registry = {
        "market.coin_price": {
            "input_schema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {"symbol": {"type": "string"}},
            }
        }
    }
    graph["nodes"][0]["action"]["request"]["body"]["input"]["symbol"] = 123
    assert any("input contract" in error for error in validate_semantics(graph, registry))
    graph["nodes"].append(
        {
            "id": "bad",
            "kind": "switch",
            "cases": [
                {
                    "name": "bad",
                    "when": {"var": "nodes.missing.response.body.output.price"},
                    "target": "end",
                }
            ],
            "default": "end",
        }
    )
    assert any("undefined data" in error for error in validate_semantics(graph, registry))


@pytest.mark.asyncio
async def test_retry_cost_cannot_treat_failed_transport_as_free():
    from ritesmith.observability.costs import estimated_cost
    from ritesmith.observability.generation import GenerationTrace, current_trace

    provider = OpenAIProvider(Settings(), client=object())
    provider._chat = AsyncMock(
        side_effect=[
            LLMTimeoutError("temporary"),
            ("{}", LLMCallStats(model="gpt-5-mini", prompt_tokens=10, completion_tokens=20)),
        ]
    )
    trace = GenerationTrace()
    token = current_trace.set(trace)
    try:
        await provider._chat_with_retry("gpt-5-mini", "system", "user", method="luau_gen")
        assert len(trace.calls) == 2
        assert sum(call["api_attempts"] for call in trace.calls) == 2
        assert estimated_cost(trace.calls) is None
    finally:
        current_trace.reset(token)


@pytest.mark.asyncio
@pytest.mark.skipif(not luau_available(), reason="lunardyson not installed")
async def test_fixture_failure_does_not_repair_the_generated_script(db_session, monkeypatch):
    from ritesmith.core.generation import GenerationService
    from ritesmith.schemas.generation import GenerateScriptRequest, ScriptConstraints
    from ritesmith.tests.fakes.llm import ScriptedLLM, script_response

    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.luau_tools_for_profile", lambda profile: crm_tools()
    )
    monkeypatch.setattr(
        "ritesmith.runtime.luau.luau_tools_for_profile", lambda profile: crm_tools()
    )
    llm = ScriptedLLM(supports_luau=True).queue(
        "generate_luau", script_response(SCRIPT, risk_assessment="medium")
    )
    invalid = {**case(), "tool_fixtures": [], "source": "client"}
    response = await GenerationService(db_session, llm, Settings()).generate_lua(
        GenerateScriptRequest(
            intent="Notify dormant customers",
            input_schema=INPUT,
            output_schema=OUTPUT,
            constraints=ScriptConstraints(runtime_profile="notification", reuse_policy="force_new"),
            context={"test_cases": [invalid]},
        )
    )
    assert not response.validation.valid
    assert [method for method, _ in llm.calls] == ["generate_luau"]


def test_recovery_is_not_admitted_when_observed_generation_does_not_fit():
    from ritesmith.core.repair import RecoveryAllowance

    budget = GenerationBudget(monotonic() + 4, minimum_recovery_seconds=3)
    budget.observed_seconds["proposal"] = 6
    token = current_budget.set(budget)
    try:
        assert not budget.recover("proposal")
        assert not RecoveryAllowance(remaining=1, expected_seconds=6).claim()
    finally:
        current_budget.reset(token)


def test_semantic_operations_cannot_silently_drop_fields_from_another_kind():
    with pytest.raises(ValueError, match="unsupported"):
        compile_plan(
            {
                "name": "bad",
                "steps": [
                    {
                        "kind": "wait",
                        "seconds": 1,
                        "steps": [
                            {
                                "kind": "call",
                                "capability_name": "telegram.send",
                                "args": {"text": "must not disappear"},
                            }
                        ],
                    }
                ],
            },
            BASE,
        )
    assert "branches" not in semantic_example("linear")["steps"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/generate", "/generate/lua", "/plans"])
async def test_http_rejects_malformed_test_specs_without_a_model_call(path, make_client):
    from ritesmith.tests.fakes.llm import ScriptedLLM

    llm = ScriptedLLM()
    async with make_client(llm=llm) as client:
        response = await client.post(
            path, json={"intent": "generate", "context": {"test_cases": "invalid"}}
        )
    assert response.status_code == 422 and llm.calls == []


@pytest.mark.skipif(not luau_available(), reason="lunardyson not installed")
def test_notification_fixtures_cover_eligibility_limits_send_failure_and_listing_error(monkeypatch):
    input_schema = {
        "type": "object",
        "required": ["segment", "limit"],
        "properties": {"segment": {"type": "string"}, "limit": {"type": "integer"}},
    }
    list_schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "eligible"],
                    "properties": {"id": {"type": "string"}, "eligible": {"type": "boolean"}},
                },
            },
            "error": {"type": ["string", "null"]},
        },
    }
    output_schema = {**OUTPUT, "properties": {**OUTPUT["properties"], "error": {"type": "string"}}}

    def never_live(**kwargs):
        raise AssertionError("Validation must not access external customers or send messages")

    tools = {
        "crm.list": HostFunctionDef(
            "crm.list", "notification", never_live, input_schema=INPUT, output_schema=list_schema
        ),
        "crm.send": HostFunctionDef(
            "crm.send",
            "notification",
            never_live,
            input_schema={
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
        ),
    }
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.luau_tools_for_profile", lambda profile: tools
    )
    script = """function run(input: Input, context: Context): Output
local listing = tools.crm.list({segment = input.segment})
if listing.error then
    return {notified = {}, failed = {}, count = 0, error = listing.error}
end
local notified: {string} = {}
local failed: {string} = {}
local attempts = 0
for _, customer in ipairs(listing.items) do
    if attempts >= input.limit then break end
    if customer.eligible then
        attempts += 1
        local sent = tools.crm.send({id = customer.id})
        if sent.ok then table.insert(notified, customer.id)
        else table.insert(failed, customer.id) end
    end
end
return {notified = notified, failed = failed, count = #notified}
end"""
    normal = {
        "input": {"segment": "dormant", "limit": 2},
        "source": "intent",
        "expected_output": {"notified": ["c1"], "failed": ["c2"], "count": 1},
        "tool_fixtures": [
            {
                "tool": "crm.list",
                "args": {"segment": "dormant"},
                "output": {
                    "items": [
                        {"id": "c0", "eligible": False},
                        {"id": "c1", "eligible": True},
                        {"id": "c2", "eligible": True},
                        {"id": "c3", "eligible": True},
                    ]
                },
            },
            {"tool": "crm.send", "args": {"id": "c1"}, "output": {"ok": True}},
            {"tool": "crm.send", "args": {"id": "c2"}, "output": {"ok": False}},
        ],
        "expected_calls": {"crm.list": 1, "crm.send": 2},
    }
    failed_listing = {
        "input": {"segment": "dormant", "limit": 2},
        "source": "intent",
        "expected_output": {"notified": [], "failed": [], "count": 0, "error": "unavailable"},
        "tool_fixtures": [
            {
                "tool": "crm.list",
                "args": {"segment": "dormant"},
                "output": {"items": [], "error": "unavailable"},
            }
        ],
        "expected_calls": {"crm.list": 1, "crm.send": 0},
    }
    session = LuauValidationSession(Settings(), "notification", input_schema, output_schema)
    try:
        diagnostics = session.check(script)["strict"]
        assert not diagnostics, json.dumps(diagnostics)
        for spec in preflight([normal, failed_listing], tools, input_schema, output_schema):
            result, fixture_error, errors = session.execute(script, spec)
            assert result.ok and fixture_error is None and errors == []
    finally:
        session.close()


@pytest.mark.asyncio
async def test_mcp_bridge_labels_generation_transport(httpx_mock):
    from ritesmith.api.mcp_handler import _post

    httpx_mock.add_response(json={"valid": True})
    assert await _post("/generate", {"intent": "test"}) == {"valid": True}
    assert httpx_mock.get_request().headers["X-Ritesmith-Interface"] == "mcp"


@pytest.mark.asyncio
async def test_mcp_generation_metrics_are_separate_from_http(make_client):
    from ritesmith.observability.metrics import generation_requests_total
    from ritesmith.tests.fakes.llm import ScriptedLLM

    metric = generation_requests_total.labels(
        entrypoint="mcp:/generate", artifact_type="unknown", outcome="invalid"
    )
    before = metric._value.get()
    llm = ScriptedLLM()
    async with make_client(llm=llm) as client:
        response = await client.post(
            "/generate",
            headers={"X-Ritesmith-Interface": "mcp"},
            json={"intent": "test", "context": {"test_cases": "invalid"}},
        )
    assert response.status_code == 422 and llm.calls == []
    assert metric._value.get() == before + 1


def test_fixture_oracle_checks_do_not_invent_requirements_for_pure_transformations():
    specs = preflight(
        [{"input": {"ids": ["c1"]}, "expected_output": {"ids": ["c1"]}, "source": "intent"}],
        {},
    )
    assert specs[0].functional
    # Count consistency applies only when the contract/test includes a count.
    minimal = case()
    minimal["expected_output"] = {"notified": ["c1"]}
    assert preflight([minimal], crm_tools())[0].functional
