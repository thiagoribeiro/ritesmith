"""Explicit first-pass seeds and continuation behavior against the real renderer."""

import os
from copy import deepcopy

import pytest

from ritesmith.workflows.constructors import semantic_example
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.simulation import WorkflowSimulator
from ritesmith.workflows.transport import typed_json_workflow
from ritesmith.workflows.validator import WorkflowValidator


@pytest.fixture
def native():
    if not os.environ.get("TRAMA_ORACLE_CLASSPATH"):
        pytest.skip("Actual compiled Trama renderer required")
    from benchmarks.generation_latency.native_transport import TramaTransportOracle

    with TramaTransportOracle() as oracle:
        yield oracle


def graph(plan, **kwargs):
    result = typed_json_workflow(compile_plan(plan, "http://candidate", **kwargs))
    assert not WorkflowValidator().validate(result)
    return result


def with_final_minimum(*, count=28, deadline=None):
    plan = semantic_example("state_tracking")
    repeat = plan["steps"][0]
    repeat["count"] = count
    if deadline is not None:
        repeat.pop("count")
        repeat["deadline_unix"] = deadline
    plan["steps"].append(
        {
            "kind": "call",
            "id": "summary",
            "capability_name": "telegram.send",
            "args": {
                "text": {
                    "type": "template",
                    "parts": [
                        "Minimum: ",
                        {"ref": "minimum", "path": "min_value"},
                    ],
                }
            },
        }
    )
    return plan


def test_missing_first_pass_seed_has_actionable_diagnostic():
    plan = semantic_example("state_tracking")
    plan["steps"][0].pop("initial_state")
    with pytest.raises(ValueError, match="monitor.*initial_state.*minimum"):
        compile_plan(plan, "http://candidate")


@pytest.mark.parametrize("state", [None, [], "invalid"])
def test_invalid_continuation_state_is_rejected(state):
    with pytest.raises(TypeError, match="continuation.state"):
        compile_plan(
            semantic_example("continuation"), "http://candidate", continuation={"state": state}
        )


def test_remaining_work_cannot_exceed_the_original_total():
    with pytest.raises(ValueError, match="exceeds the requested total"):
        compile_plan(
            semantic_example("fixed_samples"),
            "http://candidate",
            continuation={"remaining_iterations": 3},
        )


def test_duration_continuation_requires_its_original_deadline():
    plan = semantic_example("bounded_polling")
    repeat = plan["steps"][0]
    repeat.pop("count")
    repeat["duration_seconds"] = 3600
    with pytest.raises(ValueError, match="original deadline_unix"):
        compile_plan(plan, "http://candidate", continuation={"remaining_iterations": 8})


def test_explicit_first_pass_and_prior_results_keep_the_running_minimum(native):
    plan = semantic_example("state_tracking")
    compiled = graph(plan)
    seen = []

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            seen.append(self.result.clock)
            return super().fixture(node_id, capability, args)

    result = Simulator(
        compiled,
        {"market.coin_price": [{"price": 8}, {"price": 4}, {"price": 6}]},
        transport=native,
    ).run()
    assert seen == [0, 60, 120]
    assert result.nodes["minimum"]["response"]["body"]["output"]["min_value"] == 4
    assert result.nodes["monitor_tick"]["response"]["body"]["output"]["remaining_iterations"] == 0
    for node in compiled["nodes"]:
        assert "X-Trama-Template-Mode" not in node.get("action", {}).get("request", {}).get(
            "headers", {}
        )


def test_weekly_continuation_preserves_minimum_and_has_28_samples(native):
    plan = with_final_minimum()
    plan["steps"][0]["interval_seconds"] = 21600
    original = deepcopy(plan)
    first = WorkflowSimulator(
        graph(plan), {"market.coin_price": {"price": 7}}, transport=native
    ).run()
    assert first.calls["market.coin_price"] == 20
    assert first.calls["telegram.send"] == 0
    state = first.continuations[0]["context"]["continuation"]
    assert state["remaining_iterations"] == 8 and state["state"] == {"minimum": 7}
    assert first.clock == 20 * 21600
    messages = []

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            if capability == "telegram.send":
                messages.append(args["text"])
                return {"ok": True}
            return {"price": 20}

    second = Simulator(graph(plan, continuation=state), now=first.clock, transport=native).run()
    assert second.calls["market.coin_price"] == 8
    assert second.clock == 27 * 21600
    assert messages == ["Minimum: 7.0"]
    assert not second.continuations
    assert plan == original


def test_deadline_during_a_pass_uses_the_completed_pass_for_final_actions(native):
    plan = with_final_minimum(deadline=110)
    messages = []

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            if capability == "telegram.send":
                messages.append(args["text"])
                return {"ok": True}
            self.result.clock += 12
            return {"price": 8}

    result = Simulator(graph(plan), now=100, transport=native).run()
    assert result.calls["market.coin_price"] == 1
    assert messages == ["Minimum: 8.0"]
    assert not result.continuations


def test_expired_continuation_uses_retained_state_without_another_sample(native):
    plan = with_final_minimum(deadline=110)
    messages = []

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            assert capability == "telegram.send"
            messages.append(args["text"])
            return {"ok": True}

    result = Simulator(
        graph(plan, continuation={"deadline_unix": 110, "state": {"minimum": 3}}),
        now=120,
        transport=native,
    ).run()
    assert result.calls["market.coin_price"] == 0
    assert messages == ["Minimum: 3"]


def test_completed_count_uses_retained_state_for_final_actions(native):
    plan = with_final_minimum(count=3)
    compiled = graph(plan, continuation={"remaining_iterations": 0, "state": {"minimum": 3}})
    result = WorkflowSimulator(compiled, {"telegram.send": {"ok": True}}, transport=native).run()
    assert result.calls["market.coin_price"] == 0 and result.calls["telegram.send"] == 1


def test_seeded_strings_are_not_reinterpreted_as_templates(native):
    plan = semantic_example("continuation")
    plan["steps"][0]["initial_state"] = {"literal": "{{nodes.not_present.response.body.output}}"}
    result = WorkflowSimulator(
        graph(plan), {"market.coin_price": {"price": 7}}, transport=native
    ).run()
    state = result.continuations[0]["context"]["continuation"]["state"]
    assert state == plan["steps"][0]["initial_state"]


@pytest.mark.parametrize(
    "expression", ["{{#payload}}x{{/payload}}", "{{{payload.x}}}", "{{nodes.fetch.response..body}}"]
)
def test_transport_rejects_invalid_source_before_publication(native, expression):
    body = {"input": {"value": expression}}
    candidate = {
        "nodes": [
            {
                "id": "invalid",
                "action": {
                    "request": {
                        "url": "http://candidate/trama/execute",
                        "body": body,
                    }
                },
            }
        ]
    }
    with pytest.raises(ValueError, match="invalid.*request.body.input.value"):
        typed_json_workflow(candidate)
    with pytest.raises(ValueError):
        native.render(body, {"payload": {"x": 3}, "nodes": {}})


def test_existing_but_unexecuted_node_reference_is_not_accepted(native):
    from ritesmith.workflows.semantic_validation import validate_semantics

    plan = semantic_example("linear")
    compiled = graph(plan)
    compiled["nodes"][0]["action"]["request"]["body"]["input"]["symbol"] = (
        "{{nodes.fetch.response.body.output.price}}"
    )
    errors = validate_semantics(compiled, {}, check_graph=False)
    assert any("fetch" in error and "first-pass seed" in error for error in errors)
    with pytest.raises(ValueError, match="missing key 'fetch'"):
        native.action(compiled["nodes"][0]["action"], {"payload": {}, "nodes": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("old_switch", [False, True])
async def test_continuation_preparation_does_not_select_a_header_mode(db_session, old_switch):
    import json

    from ritesmith.config import Settings
    from ritesmith.core.workflow_generation import WorkflowGenerationService
    from ritesmith.llm.generation_context import prepare_catalog
    from ritesmith.runtime.host_functions import get_available_provider_capabilities
    from ritesmith.schemas.generation import GenerateWorkflowRequest
    from ritesmith.tests.fakes.llm import ScriptedLLM

    llm = ScriptedLLM()
    service = WorkflowGenerationService(
        db_session,
        llm,
        Settings(
            generation_semantic_workflows=True,
            generation_workflow_typed_json=old_switch,
        ),
    )
    intent = "Collect 48 BTC samples"
    plan = semantic_example("continuation")
    repeat = plan["steps"][0]
    repeat.pop("continuous")
    repeat["count"] = 48
    capabilities = get_available_provider_capabilities() + await service._get_capabilities()
    version = prepare_catalog(intent, capabilities).version
    first = WorkflowSimulator(
        graph(plan, contract_version=version, intent=intent), {"market.coin_price": {"price": 7}}
    ).run()
    request = first.continuations[0]
    response = await service.generate_workflow(
        GenerateWorkflowRequest(intent=intent, context=request["context"], save=False)
    )
    assert response.validation.valid and llm.calls == []
    content = json.loads(response.artifact.content)
    assert "X-Trama-Template-Mode" not in response.artifact.content
    continuation = next(node for node in content["nodes"] if node["id"] == "monitor_continue")
    cached = continuation["action"]["request"]["body"]["context"]["workflow_continuation"]
    assert set(cached) == {"__trama_literal_json__"}


@pytest.mark.parametrize("enabled", [False, True])
def test_guarded_optional_result_preserves_conditional_behavior(native, enabled):
    from ritesmith.workflows.semantic_validation import validate_semantics

    plan = {
        "name": "optional",
        "steps": [
            {
                "kind": "condition",
                "id": "requested",
                "when": {"var": "payload.enabled"},
                "steps": [
                    {
                        "kind": "call",
                        "id": "fetch",
                        "capability_name": "market.coin_price",
                        "args": {"symbol": "btc"},
                    },
                ],
            },
            {
                "kind": "condition",
                "id": "available",
                "when": {"!=": [{"ref": "fetch", "path": "price"}, None]},
                "steps": [
                    {
                        "kind": "call",
                        "id": "notify",
                        "capability_name": "telegram.send",
                        "args": {
                            "text": {
                                "type": "template",
                                "parts": ["BTC ", {"ref": "fetch", "path": "price"}],
                            }
                        },
                    },
                ],
            },
        ],
    }
    compiled = graph(plan)
    assert not validate_semantics(compiled, {})
    result = WorkflowSimulator(
        compiled,
        {"market.coin_price": {"price": 0}, "telegram.send": {"ok": True}},
        payload={"enabled": enabled},
        transport=native,
    ).run()
    assert result.calls["market.coin_price"] == int(enabled)
    assert result.calls["telegram.send"] == int(enabled)


@pytest.mark.parametrize(
    "source,expected",
    [
        ('"{{payload.x}}"', 7),
        ("false", False),
        ("null", None),
        ('["{{payload.x}}"]', [7]),
        ('{"value":"{{payload.x}}","other":true}', {"value": 7, "other": True}),
    ],
)
def test_serialized_json_preserves_scalars_and_value_fields(native, source, expected):
    candidate = {
        "nodes": [
            {
                "id": "call",
                "action": {
                    "request": {
                        "url": "http://candidate/endpoint",
                        "headers": {"Content-Type": "application/json"},
                        "body": source,
                    }
                },
            }
        ]
    }
    prepared = typed_json_workflow(candidate)
    assert typed_json_workflow(prepared) == prepared
    assert (
        native.action(prepared["nodes"][0]["action"], {"payload": {"x": 7}})["request"]["body"]
        == expected
    )


def test_incomplete_continuation_does_not_reset_counter_or_state():
    plan = semantic_example("state_tracking")
    with pytest.raises(ValueError, match="requires remaining_iterations"):
        compile_plan(plan, "http://candidate", continuation={"state": {"minimum": 3}})
    with pytest.raises(ValueError, match="missing retained keys.*minimum"):
        compile_plan(
            plan, "http://candidate", continuation={"remaining_iterations": 1, "state": {}}
        )


def test_seeded_structures_are_data_even_with_compiler_reserved_keys(native):
    plan = semantic_example("continuation")
    value = {"__rs_reference": {"ref": "not_a_node", "path": "value"}, "ref": "plain data"}
    plan["steps"][0]["initial_state"] = {"opaque": value}
    first = WorkflowSimulator(
        graph(plan), {"market.coin_price": {"price": 7}}, transport=native
    ).run()
    state = first.continuations[0]["context"]["continuation"]
    assert state["state"] == {"opaque": value}
    second = WorkflowSimulator(
        graph(plan, continuation=state),
        {"market.coin_price": {"price": 8}},
        now=first.clock,
        transport=native,
    ).run()
    assert second.continuations[0]["context"]["continuation"]["state"] == {"opaque": value}
