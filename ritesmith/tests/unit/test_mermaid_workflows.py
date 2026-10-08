"""Graph compilation preserves Trama semantics and rejects ambiguous flow."""

import json
from unittest.mock import AsyncMock

import pytest

from ritesmith.config import Settings
from ritesmith.llm.base import LLMCallStats
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.workflows.compact import compile_workflow
from ritesmith.workflows.examples import EXAMPLE_IDS, example, workflow_system
from ritesmith.workflows.mermaid import compile_mermaid, parse_mermaid, render_mermaid
from ritesmith.workflows.validator import WorkflowValidator

BASE = "http://ritesmith:8081"


@pytest.mark.parametrize("id_", EXAMPLE_IDS)
def test_mermaid_native_equivalence(id_):
    definition = example(id_)
    native = compile_mermaid(render_mermaid(definition), BASE)
    assert native == compile_workflow(definition, BASE)
    assert not WorkflowValidator().validate(native)
    assert definition == example(id_)


def test_mermaid_preserves_metadata_callbacks_compensation_and_non_mermaid_ids():
    definition = example("async_callback")
    definition.update(failureHandling={"type": "backoff", "maxAttempts": 4})
    node = definition["nodes"][0]
    node["id"] = definition["entrypoint"] = "external-order"
    node["compensation"] = {"url": "https://service.example/undo", "verb": "POST"}
    assert compile_mermaid(render_mermaid(definition), BASE) == compile_workflow(definition, BASE)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda graph: graph.replace("fetch --> notify", "fetch --> unknown"),
        lambda graph: graph + "\nfetch --> RS_END",
        lambda graph: graph.replace("fetch --> notify", "fetch --> notify --> RS_END"),
        lambda graph: graph + '\nfetch["task"] --> notify["task"]',
        lambda graph: graph.replace("fetch --> notify", ""),
        lambda graph: graph + '\nrogue["task"]',
        lambda graph: graph + '\n%% node fetch {"kind":"join"}',
        lambda graph: graph.replace('"kind":"task"', '"next":"end","kind":"task"', 1),
        lambda graph: graph + '\n%% workflow {"name":"duplicate"}',
    ],
)
def test_mermaid_rejects_undefined_ambiguous_and_unsupported_flow(mutation):
    with pytest.raises(ValueError):
        parse_mermaid(mutation(render_mermaid(example("linear"))))


@pytest.mark.parametrize("id_,label", [("bounded_polling", "default"), ("parallel", "join")])
def test_mermaid_requires_switch_default_and_split_join(id_, label):
    graph = render_mermaid(example(id_))
    graph = "\n".join(line for line in graph.splitlines() if f'|"{label}"|' not in line)
    with pytest.raises(ValueError):
        parse_mermaid(graph)


def test_mermaid_example_selection_is_local_and_explicit():
    prompt, ids = workflow_system(
        "anything",
        {"workflow_examples": ["parallel"]},
        BASE,
        compact=False,
        filtered=False,
        mermaid=True,
    )
    assert ids == ["parallel"]
    assert render_mermaid(example("parallel")) in prompt
    assert "definition as a Mermaid STRING" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["generate", "repair", "propose"])
async def test_mermaid_provider_returns_native_definitions_in_all_paths(operation):
    provider = OpenAIProvider(Settings(generation_mermaid_workflows=True), client=object())
    definition = example("linear")
    candidate = {
        "definition": render_mermaid(definition),
        "name": "linear",
        "description": "Notify",
        "required_capabilities": [],
    }
    payload = (
        {"analysis": {"artifact_types": ["trama_workflow"]}, "workflow": candidate}
        if operation == "propose"
        else {**candidate, "changes_made": "fixed"}
    )
    provider._chat_with_retry = AsyncMock(
        return_value=(json.dumps(payload), LLMCallStats(model="test"))
    )
    if operation == "generate":
        response, _ = await provider.generate_workflow(
            "notify", [], {}, [], BASE, {"workflow_examples": []}
        )
    elif operation == "repair":
        response, _ = await provider.repair_workflow(
            "notify", compile_workflow(definition, BASE), ["error"], 2
        )
    else:
        proposal, _ = await provider.propose(
            goal="notify", constraints={}, context={"workflow_examples": []}
        )
        response = proposal.workflow
    assert response.definition == compile_workflow(definition, BASE)
    call = provider._chat_with_retry.call_args
    system = call.kwargs.get("system", call.args[1] if call.args else "")
    assert "definition as a Mermaid STRING" in system


@pytest.mark.asyncio
async def test_invalid_mermaid_keeps_selected_examples_in_call_stats():
    from ritesmith.core.exceptions import LLMError

    provider = OpenAIProvider(Settings(generation_mermaid_workflows=True), client=object())
    stats = LLMCallStats(model="test")
    provider._chat_with_retry = AsyncMock(return_value=("{}", stats))
    with pytest.raises(LLMError):
        await provider.generate_workflow(
            "notify", [], {}, [], context={"workflow_examples": ["linear"]}
        )
    assert stats.examples == ["linear"]


@pytest.mark.asyncio
async def test_mermaid_can_repair_a_structurally_invalid_native_candidate():
    provider = OpenAIProvider(Settings(generation_mermaid_workflows=True), client=object())
    candidate = example("linear")
    provider._chat_with_retry = AsyncMock(
        return_value=(
            json.dumps({"definition": render_mermaid(candidate), "changes_made": "fixed"}),
            LLMCallStats(model="test"),
        )
    )
    response, _ = await provider.repair_workflow("notify", {"nodes": []}, ["missing name"], 2)
    assert response.definition == compile_workflow(candidate, BASE)


def test_workflow_assessment_accepts_bitcoin_only_for_btc_goal():
    from benchmarks.generation_latency.tasks import BY_ID, workflow_checks

    definition = example("linear")
    definition["nodes"][0]["capability_name"] = "market.bitcoin_price"
    definition["nodes"][0]["input"] = {}
    native = compile_workflow(definition, BASE)
    assert not workflow_checks(BY_ID["wf_quote"], native)
    assert "missing capability market.coin_price" in workflow_checks(
        {**BY_ID["wf_quote"], "goal": "Fetch ETH and notify"}, native
    )


def test_workflow_assessment_rejects_counter_reset_in_controlled_cycle():
    from benchmarks.generation_latency.tasks import workflow_checks

    definition = example("bounded_polling")
    task = {
        "id": "custom",
        "goal": "Check BTC three times",
        "caps": ["market.coin_price"],
        "sleeps": [60],
    }
    assert not workflow_checks(task, compile_workflow(definition, BASE))
    definition["nodes"][1]["input"] = {"state": {}}
    assert workflow_checks(task, compile_workflow(definition, BASE)) == [
        "counter-controlled cycle resets iteration at track"
    ]


def test_assessment_review_preserves_raw_failed_verdict_and_duration(tmp_path):
    from benchmarks.generation_latency.review import review

    definition = example("linear")
    definition["nodes"][0]["capability_name"] = "market.bitcoin_price"
    row = {
        "task": "wf_quote",
        "kind": "trama_workflow",
        "phase": "full",
        "model": "gpt-5-mini",
        "entrypoint": "specialized",
        "concurrency": 1,
        "repetition": 0,
        "duration_s": 12,
        "http_status": 200,
        "calls": [],
        "valid": False,
        "errors": ["missing capability market.coin_price"],
        "response": {
            "validation": {"valid": True},
            "artifact": {
                "artifact_type": "trama_workflow",
                "content": json.dumps(compile_workflow(definition, BASE)),
            },
        },
    }
    raw = tmp_path / "recorded.jsonl"
    raw.write_text(json.dumps(row) + "\n")
    original = raw.read_bytes()
    rows, report = review(tmp_path, tmp_path / "review-v2.json")
    assert raw.read_bytes() == original
    assert rows[0]["valid"] and rows[0]["duration_s"] == 12
    assert report["groups"][0]["valid_under_5s_fraction"] == 0
    assert report["assessment_version"] == 2
    assert report["changes"][0]["original_errors"] == row["errors"]


@pytest.mark.asyncio
async def test_workflow_examples_are_recorded_when_provider_call_fails():
    from ritesmith.core.exceptions import LLMError
    from ritesmith.observability.generation import GenerationTrace, current_trace

    provider = OpenAIProvider(Settings(generation_mermaid_workflows=True), client=object())
    provider._chat = AsyncMock(side_effect=LLMError("provider failure"))
    trace = GenerationTrace()
    token = current_trace.set(trace)
    try:
        with pytest.raises(LLMError):
            await provider.generate_workflow(
                "notify", [], {}, [], context={"workflow_examples": ["linear"]}
            )
        assert trace.calls[0]["examples"] == ["linear"]
    finally:
        current_trace.reset(token)
