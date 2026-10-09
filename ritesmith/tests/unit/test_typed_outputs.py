"""Offline transport, constructor, accounting and provider compatibility checks."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest

from ritesmith.config import Settings
from ritesmith.core.exceptions import LLMError
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.llm.response_contracts import (
    PatternCandidate,
    Plan,
    decode_response,
    operation_transport,
    packed,
    response_model,
)
from ritesmith.llm.spend_budget import SpendBudget, current_spend_budget
from ritesmith.llm.structured_output import strict_schema, unsupported_reason
from ritesmith.observability.generation import GenerationTrace, current_trace
from ritesmith.workflows.constructors import (
    compile_parameters,
    construct,
    pattern_example,
    semantic_example,
)
from ritesmith.workflows.examples import EXAMPLE_IDS
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.simulation import WorkflowSimulator
from ritesmith.workflows.validator import WorkflowValidator


def completion(data, *, finish="stop", refusal=None, usage=True):
    return SimpleNamespace(
        model="gpt-5-mini-2025-08-07",
        choices=[
            SimpleNamespace(
                finish_reason=finish, message=SimpleNamespace(content=packed(data), refusal=refusal)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        )
        if usage
        else None,
    )


@pytest.mark.parametrize(
    "method",
    [
        "luau_gen",
        "lua_repair",
        "proposal_script",
        "workflow_gen",
        "workflow_repair",
        "test_gen",
        "intent",
    ],
)
@pytest.mark.parametrize("patterns", [False, True])
def test_generated_contract_is_supported_json_schema(method, patterns):
    schema = strict_schema(response_model(method, patterns))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert unsupported_reason(schema) is None

    def closed(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert value["required"] == list(value.get("properties", {}))
            assert "default" not in value
            for item in value.values():
                closed(item)
        elif isinstance(value, list):
            for item in value:
                closed(item)

    closed(schema)


@pytest.mark.parametrize("pattern", EXAMPLE_IDS)
def test_pattern_transport_compiles_equivalently(pattern):
    candidate = PatternCandidate.model_validate(pattern_example(pattern))
    full_wire = candidate.model_dump()
    definition = {
        "definition": full_wire,
        "name": pattern,
        "description": "test",
        "required_capabilities": [],
    }
    jsonschema.validate(definition, strict_schema(response_model("workflow_gen", True)))
    decoded = json.loads(decode_response(packed(definition), response_model("workflow_gen", True)))
    graph = compile_parameters(decoded["definition"], "http://test")
    assert graph == compile_plan(semantic_example(pattern), "http://test")
    assert not WorkflowValidator().validate(graph)


def test_null_defaults_do_not_become_unsupported_fields():
    plan = Plan.model_validate(
        {"name": "wait", "steps": [{"kind": "wait", "id": None, "seconds": 2}]}
    )
    assert plan.domain()["steps"] == [{"kind": "wait", "seconds": 2}]
    assert not WorkflowValidator().validate(compile_plan(plan.domain(), "http://test"))
    with pytest.raises(ValueError):
        Plan.model_validate(
            {"name": "bad", "steps": [{"kind": "wait", "seconds": 1, "args_json": "{}"}]}
        )


def test_open_contract_data_is_preserved_and_malformed_json_rejected():
    args = {
        "strange key": {"additionalProperties": {"type": "string"}},
        "unicode": "✓",
        "nullable": None,
    }
    candidate = PatternCandidate.model_validate(
        {
            "format": "pattern_parameters",
            "name": "data",
            "parameters": {
                "pattern": "linear",
                "steps": [
                    {"kind": "call", "capability_name": "custom.tool", "args_json": packed(args)}
                ],
            },
        }
    )
    assert candidate.domain()["parameters"]["steps"][0]["args"] == args
    for invalid in ("not JSON", '{"x":NaN}', '{"x":1,"x":2}'):
        candidate.parameters.steps[0].args_json = invalid
        with pytest.raises(ValueError):
            candidate.domain()


def test_exclusive_termination_and_finite_after():
    fetch = {
        "kind": "call",
        "id": "fetch",
        "capability_name": "market.coin_price",
        "args": {"symbol": "btc"},
    }
    after = {
        "kind": "call",
        "id": "notify",
        "capability_name": "telegram.send",
        "args": {"text": "done"},
    }
    for rule in ({"kind": "samples", "count": 3}, {"kind": "duration", "seconds": 180}):
        plan = construct(
            "bounded_polling",
            name="finite",
            steps=[fetch],
            interval_seconds=60,
            termination=rule,
            after=[after],
        )
        result = WorkflowSimulator(
            compile_plan(plan, "http://test"),
            {"market.coin_price": {"price": 1}, "telegram.send": {"ok": True}},
        ).run()
        assert result.calls["market.coin_price"] == 3
        assert result.calls["telegram.send"] == 1
        assert result.clock == 120
    with pytest.raises(ValueError):
        construct(
            "bounded_polling",
            name="bad",
            steps=[fetch],
            termination={"kind": "samples", "count": 2, "seconds": 30},
        )


def test_continuation_preserves_final_action_and_remaining_work():
    plan = construct(
        "fixed_samples",
        name="finite",
        steps=[
            {
                "kind": "call",
                "id": "fetch",
                "capability_name": "market.coin_price",
                "args": {"symbol": "btc"},
            }
        ],
        count=28,
        interval_seconds=60,
        after=[
            {
                "kind": "call",
                "id": "notify",
                "capability_name": "telegram.send",
                "args": {"text": "done"},
            }
        ],
    )
    first = WorkflowSimulator(
        compile_plan(plan, "http://test"), {"market.coin_price": {"price": 1}}
    ).run()
    assert first.calls["market.coin_price"] == 20
    assert first.calls["telegram.send"] == 0
    continuation = first.continuations[0]["context"]["continuation"]
    assert continuation["remaining_iterations"] == 8
    second = WorkflowSimulator(
        compile_plan(
            plan, "http://test", continuation={**continuation, "_resume_repeat": "monitor"}
        ),
        {"market.coin_price": {"price": 2}, "telegram.send": {"ok": True}},
        now=first.clock,
    ).run()
    assert second.calls["market.coin_price"] == 8
    assert second.calls["telegram.send"] == 1
    assert not second.continuations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,model,mode",
    [
        (False, "gpt-5-mini", "json_object"),
        (True, "gpt-5-mini", "json_schema"),
        (True, "custom-model", "json_object"),
    ],
)
async def test_provider_selects_transport_before_dispatch(enabled, model, mode):
    data = {
        "script": "function run(input,context) return input end",
        "name": "test",
        "description": "test",
    }
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=completion(data)))
        )
    )
    provider = OpenAIProvider(Settings(generation_structured_outputs=enabled), client=client)
    raw, stats = await provider._chat(model, "JSON", "goal", method="luau_gen")
    assert json.loads(raw)["script"] == data["script"]
    assert client.chat.completions.create.call_count == 1
    assert client.chat.completions.create.call_args.kwargs["response_format"]["type"] == mode
    assert bool(stats.schema_fallback_reason) == (model == "custom-model")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish,refusal,category", [("length", None, "truncated"), ("stop", "refused", "refusal")]
)
async def test_refusal_and_truncation_preserve_billed_usage(finish, refusal, category):
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=completion({}, finish=finish, refusal=refusal))
            )
        )
    )
    trace = GenerationTrace()
    token = current_trace.set(trace)
    try:
        with pytest.raises(LLMError) as exc:
            await OpenAIProvider(
                Settings(generation_structured_outputs=True), client=client
            )._chat_with_retry("gpt-5-mini", "JSON", "goal", method="luau_gen")
        assert exc.value.details["category"] == category
        assert client.chat.completions.create.call_count == 1
        assert trace.calls[0]["completion_tokens"] == 50
    finally:
        current_trace.reset(token)


@pytest.mark.asyncio
async def test_spend_budget_covers_parallel_calls_and_stops_unknown_usage(tmp_path):
    ledger = SpendBudget(tmp_path / "ledger.json")
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=completion({}, usage=False)))
        )
    )
    token = current_spend_budget.set(ledger)
    try:
        await OpenAIProvider(Settings(), client=client)._chat("gpt-5-mini", "JSON", "goal")
        with pytest.raises(LLMError):
            await OpenAIProvider(Settings(), client=client)._chat("gpt-5-mini", "JSON", "goal")
        assert client.chat.completions.create.call_count == 1
        assert ledger.state["calls"][0]["status"] == "unknown"
    finally:
        current_spend_budget.reset(token)


def test_spend_budget_reserves_before_calls_and_never_repeats_requests(tmp_path):
    ledger = SpendBudget(tmp_path / "ledger.json", limit_usd="0.001")
    with pytest.raises(LLMError):
        ledger.reserve({"model": "gpt-5-mini", "messages": [], "max_completion_tokens": 6000})
    assert not ledger.state["calls"]
    ledger = SpendBudget(tmp_path / "other.json", max_requests=1)
    ledger.request("cell1")
    with pytest.raises(LLMError):
        ledger.request("cell1")


def test_semantic_transport_round_trip():
    for name in EXAMPLE_IDS:
        native = semantic_example(name)
        wire = Plan.model_validate(
            {
                "name": native["name"],
                "steps": [operation_transport(item) for item in native["steps"]],
            }
        )
        assert compile_plan(wire.domain(), "http://test") == compile_plan(native, "http://test")


def test_operation_scoped_switches_are_independent():
    provider = OpenAIProvider(
        Settings(
            generation_structured_outputs=True,
            generation_structured_output_operations=["test_gen"],
            generation_pattern_parameters=True,
            generation_pattern_parameter_operations=[],
        ),
        client=object(),
    )
    assert provider._enabled("structured_outputs", "test_gen")
    assert not provider._enabled("structured_outputs", "luau_gen")
    assert not provider._enabled("pattern_parameters", "workflow_gen")


@pytest.mark.asyncio
async def test_pattern_rules_are_not_duplicated_in_the_transport_prompt():
    from ritesmith.workflows.constructors import PATTERN_RULES

    data = {"definition": pattern_example("linear"), "name": "linear", "description": "test"}
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=completion(data)))
        )
    )
    provider = OpenAIProvider(
        Settings(generation_structured_outputs=True, generation_pattern_parameters=True),
        client=client,
    )
    await provider._chat("gpt-5-mini", "JSON\n" + PATTERN_RULES, "goal", method="workflow_gen")
    system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert system.count(PATTERN_RULES) == 1


def test_frozen_matrix_and_controlled_workflow_oracles():
    from benchmarks.generation_latency.tasks import BY_ID
    from benchmarks.generation_latency.typed_decisions import (
        CELLS,
        controlled_workflow_check,
        hidden_cases,
    )

    assert len(CELLS) == 12
    assert len({(task, interface, variant) for task, interface, variant, _ in CELLS}) == 12
    parallel = compile_plan(semantic_example("parallel"), "http://test")
    controlled_workflow_check(BY_ID["wf_parallel_prices"], parallel)
    notify = next(node for node in parallel["nodes"] if node["id"] == "notify")
    notify["action"]["request"]["body"]["input"]["text"] = "BTC: 111111"
    with pytest.raises(AssertionError, match="branch results"):
        controlled_workflow_check(BY_ID["wf_parallel_prices"], parallel)
    graph = compile_plan(
        {
            "name": "week",
            "steps": [
                {
                    "kind": "repeat",
                    "id": "monitor",
                    "count": 28,
                    "interval_seconds": 21600,
                    "steps": [
                        {
                            "kind": "call",
                            "id": "fetch",
                            "capability_name": "market.coin_price",
                            "args": {"symbol": "btc"},
                        },
                        {
                            "kind": "condition",
                            "when": {">": [{"ref": "fetch", "path": "price"}, 100000]},
                            "steps": [
                                {
                                    "kind": "call",
                                    "capability_name": "telegram.send",
                                    "args": {"text": {"ref": "fetch", "path": "price"}},
                                }
                            ],
                        },
                    ],
                }
            ],
        },
        "http://test",
    )
    controlled_workflow_check(BY_ID["wf_week"], graph)
    cases = hidden_cases(BY_ID["t3_notify_inactive"])
    assert cases[-1]["expected_calls"]["message.send"] == 10


def test_independent_test_transport_preserves_fixtures_and_assertions():
    raw = packed(
        {
            "test_cases": [
                {
                    "input_json": '{"segment":"dormant"}',
                    "expected_output_json": '{"count":1}',
                    "tool_fixtures": [
                        {
                            "tool": "crm.list",
                            "args_json": '{"segment":"dormant"}',
                            "output_json": '{"ok":true,"customers":[{"id":"c1"}]}',
                            "times": 1,
                        }
                    ],
                    "assertions": [{"path": "count", "op": "equals", "value_json": "1"}],
                    "expected_calls_json": '{"crm.list":1}',
                    "source": "intent",
                }
            ]
        }
    )
    case = json.loads(decode_response(raw, response_model("test_gen")))["test_cases"][0]
    assert case["tool_fixtures"][0]["output"]["customers"][0]["id"] == "c1"
    assert case["assertions"][0]["value"] == 1
    with pytest.raises(ValueError):
        decode_response(
            raw.replace('"value_json":"1"', '"value_json":"bad"'), response_model("test_gen")
        )


def test_spend_restart_keeps_reservations_and_total_cap(tmp_path):
    path = tmp_path / "spend.json"
    budget = SpendBudget(path, limit_usd="0.03")
    kwargs = {"model": "gpt-5-mini", "messages": [], "max_completion_tokens": 6000}
    first = budget.reserve(kwargs)
    second = budget.reserve(kwargs)
    assert sum(float(call["reserved_usd"]) for call in (first, second)) <= 0.03
    with pytest.raises(LLMError):
        budget.reserve(kwargs)
    restored = SpendBudget(path)
    with pytest.raises(LLMError):
        restored.reserve(kwargs)


@pytest.mark.asyncio
async def test_schema_rejection_is_not_retried_and_invalid_candidate_is_preserved():
    invalid = {
        "definition": {"format": "compact_graph", "graph_json": "not JSON"},
        "name": "test",
        "description": "test",
    }
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=completion(invalid)))
        )
    )
    provider = OpenAIProvider(Settings(generation_structured_outputs=True), client=client)
    with pytest.raises(LLMError) as exc:
        await provider._chat_with_retry("gpt-5-mini", "JSON", "goal", method="workflow_gen")
    assert client.chat.completions.create.call_count == 1
    assert exc.value.details["candidate"] == packed(invalid)
    assert exc.value.details["stats"]["completion_tokens"] == 50
