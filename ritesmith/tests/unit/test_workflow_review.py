"""Regression checks for malformed candidates and opaque data in repeat exits."""

import json
import os
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from ritesmith.config import Settings
from ritesmith.core.exceptions import LLMError
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.registry.models import Artifact as ArtifactORM
from ritesmith.tests.fakes.llm import (
    ScriptedLLM,
    intent,
    stats,
    workflow_repair_response,
    workflow_response,
)
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


@pytest.mark.parametrize("termination", ["early_deadline", "expired_deadline", "completed_count"])
def test_final_actions_preserve_literal_business_and_tail_references(native, termination):
    plan = semantic_example("state_tracking")
    repeat = plan["steps"][0]
    literal = [
        {"ref": "fetch", "path": "price"},
        {"__rs_reference": {"ref": "summary", "path": "output"}},
        "{{nodes.fetch.response.body.output.price}}",
    ]
    plan["steps"].append(
        {
            "kind": "call",
            "id": "summary",
            "capability_name": "fixture.echo",
            "args": {"data": {"__trama_literal_json__": literal}},
        }
    )
    continuation = None
    if termination == "completed_count":
        continuation = {"remaining_iterations": 0, "state": {"minimum": 3}}
        now = 0
    else:
        repeat.pop("count")
        repeat["deadline_unix"] = 110
        now = 100 if termination == "early_deadline" else 120
    seen = []

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            if capability == "fixture.echo":
                seen.append(deepcopy(args["data"]))
                return {"ok": True}
            self.result.clock += 12
            return {"price": 8}

    graph = typed_json_workflow(compile_plan(plan, "http://candidate", continuation=continuation))
    assert not WorkflowValidator().validate(graph)
    result = Simulator(graph, now=now, transport=native).run()
    assert seen == [literal]
    assert result.calls["market.coin_price"] == int(termination == "early_deadline")
    assert not result.continuations


BAD_NODES = [
    None,
    {"id": []},
    {"kind": []},
    {"action": None},
    {"action": {"request": None}},
    {"action": {"request": {"headers": None}}},
    {"action": {"request": {"headers": {"Content-Type": 42}}}},
    {"action": {"request": {"url": []}}},
    {"action": {"callback": None}},
    {"cases": {}},
    {"cases": [None]},
    {"cases": [{"target": []}]},
    {"branches": {}},
    {"branches": [[]]},
    {"next": []},
]


def malformed_graph(bad_node):
    node = {
        "id": "bad",
        "kind": "task",
        "next": "end",
        "action": {"request": {"url": "http://candidate", "body": {"input": {}}}},
    }
    return {"entrypoint": "bad", "nodes": [None if bad_node is None else {**node, **bad_node}]}


@pytest.mark.parametrize("bad_node", BAD_NODES)
def test_malformed_container_has_a_located_diagnostic(bad_node):
    graph = malformed_graph(bad_node)
    assert any("definition.nodes[0]" in error for error in WorkflowValidator().validate(graph))
    with pytest.raises(ValueError, match=r"definition.nodes\[0\]"):
        typed_json_workflow(graph)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_node", [None, {"action": {"request": {"headers": None}}}, {"cases": {}}]
)
@pytest.mark.parametrize("endpoint", ["/generate/trama-workflow", "/generate", "/plans"])
async def test_malformed_graph_returns_422_after_one_repair(
    make_client, db_session, endpoint, bad_node
):
    graph = malformed_graph(bad_node)
    llm = ScriptedLLM().queue(
        "analyze_intent", intent(artifact_types=["trama_workflow"], requires_workflow=True)
    )
    llm.queue("generate_workflow", workflow_response(graph))
    llm.queue("repair_workflow", workflow_repair_response(graph))
    body = {
        "intent": "malformed workflow candidate review",
        "constraints": {"reuse_policy": "force_new"},
    }
    if endpoint == "/plans":
        body["requested_artifact_types"] = ["trama_workflow"]
        body["reuse_policy"] = "force_new"
        body["constraints"] = {}
    async with make_client(llm=llm) as client:
        response = await client.post(endpoint, json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "generation_failed"
    assert len(llm.called("generate_workflow")) == len(llm.called("repair_workflow")) == 1
    assert "definition.nodes[0]" in json.dumps(response.json())
    assert await db_session.scalar(select(func.count()).select_from(ArtifactORM)) == 0


def test_serialized_url_and_header_templates_are_normalized_without_changing_body(native):
    graph = malformed_graph({})
    request = graph["nodes"][0]["action"]["request"]
    request.update(
        url={"value": "http://candidate/trama/execute"},
        headers={"Content-Type": {"value": "application/json"}},
        body={"input": "{{payload.x}}"},
    )
    prepared = typed_json_workflow(graph)
    assert typed_json_workflow(prepared) == prepared
    action = prepared["nodes"][0]["action"]
    assert action["request"]["url"] == "http://candidate/trama/execute"
    assert native.action(action, {"payload": {"x": 7}})["request"]["body"] == {"input": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["null", "[]", '"invalid"', "{"])
@pytest.mark.parametrize("operation", ["generate", "repair"])
async def test_unusable_provider_response_preserves_original_candidate(operation, raw):
    provider = OpenAIProvider(Settings(), client=object())
    provider._chat_with_retry = AsyncMock(return_value=(raw, stats()))
    with pytest.raises(LLMError) as failure:
        if operation == "generate":
            await provider.generate_workflow(
                goal="workflow review",
                available_capabilities=[],
                constraints={},
                similar_workflows=[],
            )
        else:
            await provider.repair_workflow(
                original_goal="workflow review",
                current_definition={},
                validation_errors=["invalid"],
                attempt_number=2,
                available_capability_names=[],
            )
    assert failure.value.details["candidate"] == raw
    assert failure.value.details["category"] == "graph"
    assert failure.value.details["stats"]["total_tokens"] == 60
    assert provider._chat_with_retry.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["counter", "state", "contracts", "plan", "container"])
async def test_invalid_cached_continuation_is_422_without_model_recovery(
    make_client, db_session, failure
):
    from ritesmith.llm.generation_context import prepare_catalog
    from ritesmith.registry.models import GenerationJob
    from ritesmith.runtime.host_functions import get_available_provider_capabilities
    from ritesmith.workflows.semantic import FORMAT_VERSION

    goal = "Retain the minimum across three samples"
    cached = {
        "version": FORMAT_VERSION,
        "intent": goal,
        "plan": semantic_example("state_tracking"),
        "completed_repeat": "monitor",
        "contract_version": prepare_catalog(goal, get_available_provider_capabilities()).version,
    }
    state = {"remaining_iterations": 2, "state": {"minimum": 3}}
    if failure == "counter":
        state.pop("remaining_iterations")
    elif failure == "state":
        state["state"] = {}
    elif failure == "contracts":
        cached["contract_version"] = "outdated"
    elif failure == "plan":
        cached.pop("plan")
    else:
        cached = ["invalid"]
    llm = ScriptedLLM()
    async with make_client(
        llm=llm, settings=Settings(generation_semantic_workflows=True)
    ) as client:
        response = await client.post(
            "/generate/trama-workflow",
            json={
                "intent": goal,
                "context": {"workflow_continuation": cached, "continuation": state},
            },
        )
    assert response.status_code == 422
    if failure == "container":
        assert response.json()["detail"][0]["loc"] == ["body", "context"]
    else:
        assert response.json()["error"] == "generation_failed"
    assert "workflow_continuation" in json.dumps(response.json())
    assert llm.calls == []
    assert await db_session.scalar(select(func.count()).select_from(ArtifactORM)) == 0
    if failure != "container":
        assert await db_session.scalar(select(GenerationJob.status)) == "failed"
