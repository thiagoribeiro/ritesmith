"""Cost gates include failed requests and avoid irrelevant workflow test calls."""

import pytest

from benchmarks.generation_latency.pricing import estimated_cost
from benchmarks.generation_latency.subset import comparison, measurements
from ritesmith.config import Settings
from ritesmith.core.generation_dispatcher import GenerationDispatcher
from ritesmith.llm.base import GenerationProposal, IntentAnalysis
from ritesmith.schemas.generation import GenerateRequest
from ritesmith.tests.fakes.llm import script_response, workflow_response
from ritesmith.tests.unit.test_generation_latency import SLEEP_WORKFLOW, ProposalLLM


def row(*, valid=True, seconds=9, tokens=1000, entrypoint="specialized"):
    return {
        "task": "task",
        "kind": "luau_script",
        "phase": "full",
        "model": "gpt-5-mini",
        "entrypoint": entrypoint,
        "concurrency": 1,
        "client_tests": True,
        "valid": valid,
        "duration_s": seconds,
        "calls": [{"model": "gpt-5-mini", "prompt_tokens": 0, "completion_tokens": tokens}],
    }


def test_cost_per_valid_artifact_includes_failed_calls():
    result = measurements([row(), row(valid=False)], 10)
    assert result["estimated_usd"] == pytest.approx(0.004)
    assert result["cost_per_valid_artifact_usd"] == pytest.approx(0.004)
    assert result["valid_within_target_fraction"] == 0.5


def test_subset_requires_latency_quality_and_fifty_percent_cost_saving():
    baseline = [row(seconds=20, tokens=2000)]
    result = comparison([row()], baseline)[0]
    assert result["qualified_on_subset"] and result["cost_reduction_fraction"] == 0.5
    assert not comparison([row(seconds=10.1)], baseline)[0]["qualified_on_subset"]
    assert not comparison([row(tokens=1001)], baseline)[0]["qualified_on_subset"]
    assert not comparison([row(entrypoint="plans")], baseline)[0]["qualified_on_subset"]


def test_cancelled_or_unmetered_failure_has_unknown_cost():
    assert estimated_cost([{"model": "gpt-5-mini", "cancelled": True}]) is None
    assert estimated_cost([{"model": "gpt-5-mini", "error": "LLMTimeoutError"}]) is None
    assert not comparison(
        [{**row(), "calls": [{"model": "gpt-5-mini", "cancelled": True}]}], [row(tokens=2000)]
    )[0]["cost_gate"]


def test_unrecorded_software_failure_remains_in_the_comparison():
    groups = comparison(
        [{**row(), "software_sha256": "known"}, row(valid=False)], [row(tokens=2000)]
    )
    assert len(groups) == 2
    assert any(
        g["software_sha256"] == "unrecorded" and not g["qualified_on_subset"] for g in groups
    )


@pytest.mark.asyncio
async def test_automatic_workflow_does_not_generate_and_cancel_lua_tests(db_session):
    llm = ProposalLLM().queue(
        "propose",
        GenerationProposal(
            analysis=IntentAnalysis(artifact_types=["trama_workflow"], requires_workflow=True),
            workflow=workflow_response(SLEEP_WORKFLOW),
        ),
    )
    response = await GenerationDispatcher(
        db_session, llm, Settings(script_language="lua")
    ).dispatch(
        GenerateRequest(
            intent="Remind me in five minutes", constraints={"reuse_policy": "force_new"}
        )
    )
    assert response.validation.valid
    assert [method for method, _ in llm.calls] == ["propose"]


@pytest.mark.asyncio
async def test_deferred_script_still_gets_independent_tests(db_session):
    class TestableProposal(ProposalLLM):
        async def generate_tests(self, goal, input_schema, output_schema):
            return self._next("generate_tests", {"goal": goal})

    llm = (
        TestableProposal()
        .queue(
            "propose",
            GenerationProposal(
                analysis=IntentAnalysis(artifact_types=["lua_script"]),
                script=script_response(
                    "function run(input, context) return {result=42} end", risk_assessment="medium"
                ),
            ),
        )
        .queue("generate_tests", [{"input": {}, "expected_output": {"result": 42}}])
    )
    response = await GenerationDispatcher(
        db_session, llm, Settings(script_language="lua")
    ).dispatch(
        GenerateRequest(intent="monitor a number", constraints={"reuse_policy": "force_new"})
    )
    assert response.validation.valid
    assert [method for method, _ in llm.calls] == ["propose", "generate_tests"]
    assert any(check.name == "test_case_0" for check in response.validation.checks)
