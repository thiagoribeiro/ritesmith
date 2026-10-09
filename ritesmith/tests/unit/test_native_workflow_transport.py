"""Cross-language transport checks; fixtures contain no model or live-tool calls."""

import os
from copy import deepcopy

import pytest

from ritesmith.config import Settings
from ritesmith.workflows.constructors import semantic_example
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.simulation import HttpFailure, WorkflowSimulator
from ritesmith.workflows.transport import typed_json_workflow


@pytest.fixture
def native():
    if not os.environ.get("TRAMA_ORACLE_CLASSPATH"):
        pytest.skip("Actual compiled Trama oracle required")
    from benchmarks.generation_latency.native_transport import TramaTransportOracle

    with TramaTransportOracle() as oracle:
        yield oracle


def test_transport_is_headerless_and_idempotent_for_tasks_and_compensation():
    assert not Settings().generation_workflow_typed_json
    graph = compile_plan(semantic_example("compensation"), "http://candidate")
    graph["nodes"][0]["compensation"]["body"] = {"id": "{{payload.id}}"}
    original = deepcopy(graph)
    typed = typed_json_workflow(graph)
    assert graph == original
    assert "X-Trama-Template-Mode" not in typed["nodes"][0]["action"]["request"]["headers"]
    assert "X-Trama-Template-Mode" not in typed["nodes"][0]["compensation"]["headers"]
    assert typed_json_workflow(typed) == typed
    malformed = {"nodes": [{"id": "broken", "action": {"request": {"body": '{"input":'}}}]}
    with pytest.raises(ValueError, match="broken.*request.body"):
        typed_json_workflow(malformed)


def test_cached_callback_templates_remain_literal_until_the_future_execution(native):
    body = {
        "intent": "Keep {{payload.label}} literal",
        "constraints": {},
        "context": {
            "workflow_continuation": {"plan": semantic_example("async_callback")},
            "continuation": {"state": "{{payload.state}}"},
        },
    }
    graph = {"nodes": [{"action": {"request": {"url": "http://candidate/plans", "body": body}}}]}
    action = typed_json_workflow(graph)["nodes"][0]["action"]
    result = native.action(action, {"payload": {"state": {"minimum": 7}}})["request"]["body"]
    assert result["intent"] == body["intent"]
    assert result["context"]["workflow_continuation"] == body["context"]["workflow_continuation"]
    assert result["context"]["continuation"]["state"] == {"minimum": 7}


def test_native_body_strings_and_wrappers_match_the_real_serializer(native):
    source = '{"input":{"price":"{{payload.price}}"}}'
    context = {"payload": {"price": 7.5}}
    assert native.render(source, context) == {"input": {"price": 7.5}}
    assert native.render({"value": source}, context) == {"input": {"price": 7.5}}
    graph = {
        "nodes": [
            {"action": {"request": {"url": "http://candidate/trama/execute", "body": source}}}
        ]
    }
    action = typed_json_workflow(graph)["nodes"][0]["action"]
    assert native.action(action, context)["request"]["body"] == {"input": {"price": 7.5}}


@pytest.mark.parametrize(
    "pattern",
    [
        "linear",
        "bounded_polling",
        "fixed_samples",
        "continuation",
        "parallel",
        "state_tracking",
        "content_monitor",
        "async_callback",
        "compensation",
    ],
)
def test_nine_patterns_use_real_native_body_rendering(native, pattern):
    graph = typed_json_workflow(compile_plan(semantic_example(pattern), "http://candidate"))
    seen = []

    class Simulator(WorkflowSimulator):
        def fixture(self, node, capability, args):
            seen.append((capability, deepcopy(args), self.result.clock))
            if capability == "market.coin_price":
                assert isinstance(args["symbol"], str)
                return {"price": 111111 if args["symbol"] == "btc" else 2222}
            if capability == "telegram.send":
                assert isinstance(args["text"], str) and "111111" in args["text"]
                if pattern == "parallel":
                    assert "2222" in args["text"]
                return {"ok": True}
            if capability == "web.search":
                return {"result": "new title"}
            if capability == "llm.evaluate":
                return {"decision": "skip", "message": ""}
            return {"status": "APPROVED", "id": "fixture-order"}

    result = Simulator(graph, payload={"id": "fixture-order"}, transport=native).run()
    if pattern == "continuation":
        state = result.continuations[0]["context"]["continuation"]
        assert isinstance(state["state"], dict) and state["remaining_iterations"] is None
        assert result.calls["market.coin_price"] == 20
    elif pattern == "fixed_samples":
        assert result.calls["market.coin_price"] == 2
        assert result.nodes["monitor_tick"]["response"]["body"]["output"]["state"]["samples"] == [
            {"price": 111111},
            {"price": 111111},
        ]
    elif pattern == "state_tracking":
        assert result.nodes["monitor_tick"]["response"]["body"]["output"]["state"] == {
            "minimum": 111111
        }
    elif pattern == "parallel":
        assert result.calls["market.coin_price"] == 2 and result.calls["telegram.send"] == 1
    elif pattern == "async_callback":
        assert seen[0][1]["callbackUrl"] == "https://callback.example"


def test_weekly_continuation_preserves_typed_remaining_state_and_intervals(native):
    from benchmarks.generation_latency import reliability
    from benchmarks.generation_latency.tasks import BY_ID

    fetch = {
        "kind": "call",
        "id": "fetch",
        "capability_name": "market.coin_price",
        "args": {"symbol": "btc"},
    }
    notify = {
        "kind": "call",
        "id": "notify",
        "capability_name": "telegram.send",
        "args": {
            "text": {"type": "template", "parts": ["BTC ", {"ref": "fetch", "path": "price"}]}
        },
    }
    plan = {
        "name": "week",
        "steps": [
            {
                "kind": "repeat",
                "id": "monitor",
                "count": 28,
                "interval_seconds": 21600,
                "steps": [
                    fetch,
                    {
                        "kind": "condition",
                        "id": "above",
                        "when": {">": [{"ref": "fetch", "path": "price"}, 100000]},
                        "steps": [notify],
                    },
                ],
            }
        ],
    }
    graph = typed_json_workflow(
        compile_plan(plan, "http://candidate", intent=BY_ID["wf_week"]["goal"])
    )
    original = reliability.TRAMA_TRANSPORT
    reliability.TRAMA_TRANSPORT = native
    try:
        reliability.workflow_check(BY_ID["wf_week"], graph)
    finally:
        reliability.TRAMA_TRANSPORT = original


@pytest.mark.parametrize(
    "scenario",
    [
        "wf_threshold",
        "wf_four_samples",
        "wf_minimum",
        "wf_chain",
        "wf_news",
        "wf_parallel_prices",
        "wf_reminder",
        "plan_t1_celsius_to_fahrenheit",
        "plan_t1_slugify",
    ],
)
def test_remaining_stage_scenarios_use_native_conditions_and_transport(native, scenario):
    from benchmarks.generation_latency import reliability
    from benchmarks.generation_latency.tasks import BY_ID
    from ritesmith.core.workflow_generation import _inject_completion_step

    task = BY_ID[scenario]
    fetch = {
        "kind": "call",
        "id": "fetch",
        "capability_name": "market.coin_price",
        "args": {"symbol": "btc" if scenario in ("wf_threshold", "wf_chain") else "eth"},
    }
    reference = lambda ref, path="": {"ref": ref, "path": path}
    notify = lambda parts: {
        "kind": "call",
        "id": "notify",
        "capability_name": "telegram.send",
        "args": {"text": {"type": "template", "parts": parts}},
    }
    if scenario in ("wf_parallel_prices", "wf_news"):
        plan = semantic_example(
            "parallel" if scenario == "wf_parallel_prices" else "content_monitor"
        )
        if scenario == "wf_news":
            plan["steps"][0]["interval_seconds"] = 3600
    elif scenario == "wf_reminder":
        plan = {"name": scenario, "steps": [{"kind": "wait", "seconds": 300}, notify(["Go home"])]}
    elif scenario.startswith("plan_"):
        script_task = task["script_task"]
        steps = [
            {
                "kind": "call",
                "id": "convert",
                "artifact_id": "art_certified_fixture",
                "args": {
                    key: reference("payload", key)
                    for key in script_task["input_schema"]["properties"]
                },
            },
            notify(
                [
                    "Result: ",
                    reference("convert", next(iter(script_task["output_schema"]["properties"]))),
                ]
            ),
        ]
        if scenario.endswith("slugify"):
            steps.insert(0, {"kind": "wait", "seconds": 300})
        plan = {"name": scenario, "steps": steps}
        reliability.BOUND_SCRIPTS["art_certified_fixture"] = script_task["reference"]
    else:
        count = 4 if scenario == "wf_four_samples" else 3
        repeat = {
            "kind": "repeat",
            "id": "monitor",
            "count": count,
            "interval_seconds": 60,
            "steps": [fetch],
        }
        after = []
        if scenario == "wf_threshold":
            repeat["steps"].append(
                {
                    "kind": "condition",
                    "id": "above",
                    "when": {">": [reference("fetch", "price"), 100000]},
                    "steps": [notify(["BTC ", reference("fetch", "price")])],
                }
            )
        elif scenario == "wf_four_samples":
            repeat["steps"].append(
                {
                    "kind": "state",
                    "id": "samples",
                    "capability_name": "stat.collect",
                    "args": {
                        "current": reference("fetch", "price"),
                        "previous_samples": reference("samples", "samples"),
                        "initial_samples": reference("payload", "continuation.state.samples"),
                    },
                }
            )
            repeat["state"] = {"samples": reference("samples", "samples")}
            repeat["initial_state"] = {"samples": []}
            after = [
                notify(["Samples: ", *[reference("samples", f"samples.{i}") for i in range(4)]])
            ]
        else:
            repeat["steps"].append(
                {
                    "kind": "state",
                    "id": "minimum",
                    "capability_name": "stat.min_value",
                    "args": {
                        "current": reference("fetch", "price"),
                        "previous_min": reference("minimum", "min_value"),
                        "initial_min": reference("payload", "continuation.state.minimum"),
                    },
                }
            )
            repeat["state"] = {"minimum": reference("minimum", "min_value")}
            repeat["initial_state"] = {"minimum": None}
            if scenario == "wf_chain":
                repeat.pop("count")
                repeat["continuous"] = True
            else:
                after = [notify(["Minimum: ", reference("minimum", "min_value")])]
        plan = {"name": scenario, "steps": [repeat, *after]}
    graph = compile_plan(plan, "http://candidate", intent=task["goal"])
    if scenario.startswith("plan_"):
        graph = _inject_completion_step(graph)
    graph = typed_json_workflow(graph)
    original = reliability.TRAMA_TRANSPORT
    reliability.TRAMA_TRANSPORT = native
    try:
        reliability.workflow_check(task, graph)
    finally:
        reliability.TRAMA_TRANSPORT = original
        reliability.BOUND_SCRIPTS.pop("art_certified_fixture", None)


def test_native_compensation_only_unwinds_completed_tasks(native):
    plan = semantic_example("compensation")
    plan["steps"][0]["compensation"]["body"] = {"id": {"ref": "payload", "path": "id"}}
    plan["steps"].append(
        {
            "kind": "call",
            "id": "notify",
            "capability_name": "telegram.send",
            "args": {"text": "done"},
        }
    )
    graph = typed_json_workflow(compile_plan(plan, "http://candidate"))
    simulator = WorkflowSimulator(
        graph,
        {"order": {"id": "created"}, "telegram.send": HttpFailure()},
        payload={"id": "order-7"},
        transport=native,
    )
    with pytest.raises(ValueError, match="Task failed: notify"):
        simulator.run()
    assert len(simulator.result.compensations) == 1
    assert simulator.result.compensations[0]["body"] == {"id": "order-7"}
    failed_first = WorkflowSimulator(graph, {"order": {"error": "failed"}}, transport=native)
    with pytest.raises(ValueError):
        failed_first.run()
    assert not failed_first.result.compensations


@pytest.mark.asyncio
async def test_native_body_accepted_by_real_capability_endpoint(native, client):
    from ritesmith.api.deps import get_settings

    client.app.dependency_overrides[get_settings] = lambda: Settings(trama_token="fixture")
    body = native.render(
        {
            "capability_name": "stat.chain_step",
            "input": {
                "iteration": 0,
                "initial_remaining": 8,
                "previous_state": "{{payload.state}}",
            },
        },
        {"payload": {"state": {"minimum": 7.5, "samples": [1, 2]}}, "nodes": {}},
    )
    response = await client.post(
        "/trama/execute", json=body, headers={"Authorization": "Bearer fixture"}
    )
    assert response.status_code == 200
    output = response.json()["output"]
    assert output["remaining_iterations"] == 7 and output["state"] == {
        "minimum": 7.5,
        "samples": [1, 2],
    }
