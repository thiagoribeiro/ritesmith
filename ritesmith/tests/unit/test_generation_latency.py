"""Regressions for the compact compiler, prompt selection and operation routing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from ritesmith.config import Settings
from ritesmith.core.generation import GenerationService
from ritesmith.core.generation_dispatcher import GenerationDispatcher
from ritesmith.core.planning import PlanBuilder
from ritesmith.core.repair import run_repair_loop
from ritesmith.llm import prompts
from ritesmith.llm.base import GenerationProposal, IntentAnalysis, LLMCallStats
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.observability.generation import GenerationTrace, current_trace
from ritesmith.registry.models import AuditEvent, GenerationJob
from ritesmith.schemas.generation import (
    GenerateRequest,
    GenerateScriptRequest,
    GenerateWorkflowRequest,
)
from ritesmith.schemas.plan import CreatePlanRequest
from ritesmith.tests.fakes.llm import ScriptedLLM, script_response, workflow_response
from ritesmith.workflows.compact import compact_workflow, compile_workflow
from ritesmith.workflows.examples import EXAMPLE_IDS, example, select_examples, workflow_system
from ritesmith.workflows.validator import WorkflowValidator

BASE = "http://ritesmith:8081"
SLEEP_WORKFLOW = {
    "name": "wait",
    "version": "2",
    "entrypoint": "wait",
    "nodes": [{"id": "wait", "kind": "sleep", "durationSeconds": 5, "next": "end"}],
}


@pytest.mark.parametrize("name", EXAMPLE_IDS)
def test_compiler_preserves_graph_and_round_trip(name):
    compact = example(name)
    native = compile_workflow(compact, BASE)
    assert not WorkflowValidator().validate(native)
    assert compile_workflow(compact_workflow(native, BASE), BASE) == native
    assert compact == example(name)  # expansion must not mutate caller data


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_id": "art_x"},
        {"action": {}},
        {"unexpected": True},
    ],
)
def test_compiler_rejects_ambiguous_or_unknown_task_fields(changes):
    compact = example("linear")
    compact["nodes"][0].update(changes)
    with pytest.raises(ValidationError):
        compile_workflow(compact, BASE)


def test_compiler_preserves_advanced_actions_and_compensation():
    compact = example("async_callback")
    compact["nodes"][0]["compensation"] = {"url": "https://service.example/undo", "verb": "POST"}
    compact["failureHandling"] = {"type": "backoff", "maxAttempts": 4}
    native = compile_workflow(compact, BASE)
    assert native["nodes"][0] == compact["nodes"][0]
    assert native["failureHandling"] == compact["failureHandling"]


def test_compiler_preserves_fractional_sleep_contract():
    compact = {
        "name": "short_wait",
        "entrypoint": "wait",
        "nodes": [{"id": "wait", "kind": "sleep", "durationSeconds": 0.5, "next": "end"}],
    }
    native = compile_workflow(compact, BASE)
    assert native["nodes"][0]["durationSeconds"] == 0.5
    assert not WorkflowValidator().validate(native)


@pytest.mark.parametrize(
    ("goal", "context", "expected"),
    [
        ("Lembre-me em cinco minutos", None, {"linear"}),
        (
            "Monitor news forever in parallel",
            None,
            {
                "linear",
                "bounded_polling",
                "continuation",
                "parallel",
                "content_monitor",
                "state_tracking",
            },
        ),
        (
            "Monitor exchange rate",
            {"duration_days": 7, "runs_per_day": 4},
            {"linear", "bounded_polling", "continuation"},
        ),
        ("anything", {"workflow_examples": []}, set()),
        ("anything", {"workflow_examples": ["parallel", "parallel"]}, {"parallel"}),
        (
            "Monitor four times daily for seven days",
            None,
            {"linear", "bounded_polling", "fixed_samples", "continuation"},
        ),
        ("Collect four ETH prices one minute apart", None, {"linear", "fixed_samples"}),
        (
            "Fetch prices",
            {"sample_count": 30},
            {"linear", "fixed_samples", "continuation", "bounded_polling"},
        ),
        (
            "Run tasks",
            {"parallel": True, "callback_url": "https://service.example"},
            {"linear", "parallel", "async_callback"},
        ),
        (
            "Watch updates",
            {"monitor_type": "news", "schedule_hours": 1},
            {"linear", "bounded_polling", "content_monitor", "state_tracking"},
        ),
        (
            "Continue checks",
            {"continuation": {"previous_min": 10}},
            {"linear", "bounded_polling", "fixed_samples", "continuation", "state_tracking"},
        ),
    ],
)
def test_example_selection(goal, context, expected):
    assert set(select_examples(goal, context)) == expected


@pytest.mark.parametrize(
    "cls", [GenerateRequest, GenerateScriptRequest, GenerateWorkflowRequest, CreatePlanRequest]
)
@pytest.mark.parametrize("value", [["unknown"], "linear", [1]])
def test_invalid_examples_rejected_at_request_boundary(cls, value):
    with pytest.raises(ValidationError):
        cls(intent="test", context={"workflow_examples": value})


def test_empty_examples_keeps_essential_semantics():
    prompt, selected = workflow_system(
        "monitor forever", {"workflow_examples": []}, BASE, compact=True, filtered=True
    )
    assert not selected
    assert "max_iterations" in prompt and "branches" in prompt and "continuation" in prompt
    assert "bounded_polling:" not in prompt


@pytest.mark.asyncio
async def test_openai_effort_stats_and_operation_models():
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())), close=AsyncMock()
    )
    client.chat.completions.create.return_value = SimpleNamespace(
        model="gpt-5.2",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=30,
            total_tokens=130,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
            prompt_tokens_details=SimpleNamespace(cached_tokens=64),
        ),
        choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok":true}'))
        ],
    )
    provider = OpenAIProvider(
        Settings(
            llm_script_model="gpt-5.2", llm_workflow_model="gpt-5", llm_reasoning_effort="low"
        ),
        client=client,
    )
    trace = GenerationTrace()
    token = current_trace.set(trace)
    try:
        _, stats = await provider._chat_with_retry(
            provider.model, "system", "user", method="luau_gen"
        )
        call = client.chat.completions.create.call_args.kwargs
        assert call["model"] == "gpt-5.2" and call["reasoning_effort"] == "low"
        assert "temperature" not in call
        assert stats.cached_tokens == 64 and stats.reasoning_tokens == 10 and stats.duration_s > 0
        assert trace.calls[0]["method"] == "luau_gen"
        await provider._chat_with_retry(provider.model_fast, "system", "user", method="intent")
        assert "reasoning_effort" not in client.chat.completions.create.call_args.kwargs
        fallback = provider.fallback()
        await fallback._chat_with_retry(fallback.model, "system", "user", method="luau_repair")
        call = client.chat.completions.create.call_args.kwargs
        assert call["model"] == "gpt-5-mini" and call["reasoning_effort"] == "low"
        assert trace.fallback
        await provider.close()
        client.close.assert_awaited_once()
    finally:
        current_trace.reset(token)


@pytest.mark.asyncio
async def test_lazy_process_provider_closes_without_requiring_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(Settings(), lazy_client=True)
    assert provider._client is None
    await provider.close()
    assert provider._client is None


@pytest.mark.asyncio
async def test_unified_provider_normalizes_workflow_analysis_flags():
    import json

    provider = OpenAIProvider(Settings(), client=SimpleNamespace())
    provider._chat_with_retry = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "analysis": {"artifact_types": ["trama_workflow"], "requires_workflow": False},
                    "script": None,
                    "workflow": {
                        "name": "wait",
                        "description": "wait",
                        "definition": SLEEP_WORKFLOW,
                    },
                }
            ),
            LLMCallStats(model="test"),
        )
    )
    proposal, _ = await provider.propose(goal="Wait five seconds", constraints={})
    assert proposal.analysis.requires_workflow and not proposal.analysis.requires_lua


@pytest.mark.asyncio
async def test_attempt_counter_counts_actual_invocations():
    call = AsyncMock(side_effect=[False, True])
    assert await run_repair_loop(artifact_type="luau_script", max_attempts=5, attempt_fn=call) == 2


@pytest.mark.asyncio
async def test_exhausted_credit_is_not_retried_or_sent_to_fallback():
    import httpx
    from openai import RateLimitError

    from ritesmith.core.exceptions import LLMError
    from ritesmith.core.proposal import prepare_proposal

    error = RateLimitError(
        "No credits",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com")),
        body={"type": "insufficient_quota", "code": "credit_balance_exhausted"},
    )
    create = AsyncMock(side_effect=error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenAIProvider(Settings(), client=client)
    trace = GenerationTrace()
    token = current_trace.set(trace)
    try:
        with pytest.raises(LLMError) as raised:
            await provider._chat_with_retry("gpt-5-mini", "system", "user", method="luau_gen")
        assert raised.value.details["retryable"] is False
        create.assert_awaited_once()
        assert trace.calls[0]["provider_code"] == "credit_balance_exhausted"
        assert trace.calls[0]["api_attempts"] == 1
        attempt = AsyncMock(side_effect=raised.value)
        assert await run_repair_loop("luau_script", 5, attempt) == 1
        attempt.assert_awaited_once()
    finally:
        current_trace.reset(token)

    # Verify the automatic path also skips a same-account model fallback.
    from unittest.mock import patch

    provider.propose = AsyncMock(side_effect=raised.value)
    provider.fallback = AsyncMock()
    settings = Settings(generation_parallel_tests=False)
    with (
        patch("ritesmith.core.proposal.fts_search", AsyncMock(return_value=[])),
        patch("ritesmith.core.proposal.check_reuse", AsyncMock(return_value=None)),
        patch(
            "ritesmith.core.workflow_generation.WorkflowGenerationService._get_capabilities",
            AsyncMock(return_value=[]),
        ),
        pytest.raises(LLMError),
    ):
        await prepare_proposal(None, provider, settings, goal="test", constraints={})
    provider.fallback.assert_not_called()


class ProposalLLM(ScriptedLLM):
    supports_proposal = True

    async def propose(self, **kwargs):
        return self._next("propose", kwargs)


@pytest.mark.asyncio
async def test_unified_dispatch_consumes_candidate_without_second_call(db_session):
    llm = ProposalLLM()
    llm.queue(
        "propose",
        GenerationProposal(
            analysis=IntentAnalysis(),
            script=script_response("function run(input, context) return {result=42} end"),
        ),
    )
    settings = Settings(script_language="lua", generation_parallel_tests=False)
    response = await GenerationDispatcher(db_session, llm, settings).dispatch(
        GenerateRequest(intent="unified unique test", constraints={"reuse_policy": "force_new"})
    )
    assert response.validation.valid
    assert [method for method, _ in llm.calls] == ["propose"]
    assert llm.calls[0][1]["allow_dependencies"] is False
    job = await db_session.scalar(
        select(GenerationJob).where(GenerationJob.goal == "unified unique test")
    )
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_explicit_plan_type_skips_classification(db_session):
    llm = ScriptedLLM().queue("generate_workflow", workflow_response(SLEEP_WORKFLOW))
    response = await PlanBuilder(db_session, llm, Settings()).build_plan(
        CreatePlanRequest(
            intent="explicit workflow",
            requested_artifact_types=["trama_workflow"],
            reuse_policy="force_new",
        )
    )
    assert response.validations[0].valid
    assert [method for method, _ in llm.calls] == ["generate_workflow"]


@pytest.mark.asyncio
async def test_benchmark_stops_on_account_quota_without_dropping_failed_request(
    tmp_path, monkeypatch
):
    import json
    from argparse import Namespace

    import httpx

    from benchmarks.generation_latency import run as runner
    from benchmarks.generation_latency.campaign import write_report

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            502,
            json={"error": "llm_error"},
            headers={
                "x-benchmark-calls": json.dumps(
                    [{"provider_code": "credit_balance_exhausted", "model": "gpt-5-mini"}]
                )
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        runner.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handle)),
    )
    output = tmp_path / "blocked.jsonl"
    with pytest.raises(runner.BenchmarkBlocked):
        await runner.run(
            Namespace(
                url="http://staging",
                phase="full",
                model="gpt-5-mini",
                tasks="triage",
                task_id=None,
                kind="luau_script",
                repetitions=3,
                concurrency=1,
                entrypoints=["specialized"],
                rotate_entrypoints=False,
                missing_client_tests=False,
                output=str(output),
            )
        )
    assert len(requests) == 1
    raw = output.read_text()
    assert not json.loads(raw)["valid"]
    assert output.with_suffix(".partial.summary.json").exists()
    assert not output.with_suffix(".summary.json").exists()
    write_report(tmp_path, blocked="credits exhausted")
    assert output.read_text() == raw
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "blocked" and report["promotion"] is False
    assert len(report["failures"]) == 1


@pytest.mark.asyncio
async def test_transient_workflow_audit_is_committed(db_session):
    from ritesmith.core.audit import AuditLogger
    from ritesmith.core.workflow_generation import WorkflowGenerationService

    llm = ScriptedLLM().queue("generate_workflow", workflow_response(SLEEP_WORKFLOW))
    await WorkflowGenerationService(
        db_session, llm, Settings(), AuditLogger(db_session)
    ).generate_workflow(
        GenerateWorkflowRequest(
            intent="audit unique workflow", save=False, constraints={"reuse_policy": "force_new"}
        )
    )
    events = (
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "workflow_generation.llm_call")
        )
    ).all()
    assert events and events[-1].payload["stats"]["model"] == "scripted"


@pytest.mark.asyncio
async def test_test_generation_overlaps_candidate(db_session):
    started = asyncio.Event()

    class ParallelLLM(ScriptedLLM):
        async def generate_tests(self, *args):
            started.set()
            await asyncio.sleep(0)
            return [{"input": {}, "expected_output": {"result": 42}}], LLMCallStats(model="test")

        async def generate_lua(self, **kwargs):
            await asyncio.wait_for(started.wait(), 1)
            return script_response(
                "function run(input, context) return {result=42} end", risk_assessment="medium"
            ), LLMCallStats(model="test")

    response = await GenerationService(
        db_session, ParallelLLM(), Settings(script_language="lua")
    ).generate_lua(
        GenerateScriptRequest(
            intent="parallel test unique", constraints={"reuse_policy": "force_new"}
        )
    )
    assert response.validation.valid
    assert any(c.name == "test_case_0" for c in response.validation.checks)


def test_catalog_preserves_all_capabilities_and_schema_property_names():
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
        "required": ["title", "description"],
    }
    caps = [{"capability_name": f"cap.{i}", "input_schema": schema} for i in range(40)]
    prompt = prompts.workflow_generation_user("test", caps + caps, {}, [], "{}")
    assert prompt.count('"name":"cap.39"') == 1
    assert '"properties":{"title":{"type":"string"},"description":{"type":"string"}}' in prompt


def test_explicit_examples_override_unfiltered_prompt():
    _, selected = workflow_system(
        "monitor news", {"workflow_examples": []}, BASE, compact=True, filtered=False
    )
    assert selected == []
    assert select_examples("Search solar news once") == ["linear"]


@pytest.mark.asyncio
async def test_compound_plan_binds_validated_script_before_persisting_workflow(db_session):
    import json

    script = script_response("function run(input, context) return {text=input.text} end")
    native = compile_workflow(
        {
            "name": "use_script",
            "entrypoint": "normalize",
            "nodes": [
                {
                    "id": "normalize",
                    "kind": "task",
                    "capability_name": script.name,
                    "input": {"text": "{{ payload.text }}"},
                    "next": "end",
                }
            ],
        },
        BASE,
    )
    llm = ProposalLLM().queue(
        "propose",
        GenerationProposal(
            analysis=IntentAnalysis(
                requires_lua=True,
                requires_workflow=True,
                artifact_types=["trama_workflow", "lua_script"],
            ),
            script=script,
            workflow=workflow_response(native),
        ),
    )
    response = await PlanBuilder(
        db_session, llm, Settings(script_language="lua", generation_parallel_tests=False)
    ).build_plan(
        CreatePlanRequest(intent="compound unique test", mode="persist", reuse_policy="force_new")
    )
    assert len(response.artifacts) == 2 and all(v.valid for v in response.validations)
    generated, workflow = response.artifacts
    definition = json.loads(workflow.content)
    assert (
        definition["nodes"][0]["action"]["request"]["body"]["artifact_id"] == generated.artifact_id
    )
    assert response.steps[0].artifact_id == generated.artifact_id
    assert generated.metadata["certification"] == "lua"
    assert any(n["id"] == "rs_complete" for n in definition["nodes"])
    assert [method for method, _ in llm.calls] == ["propose"]


@pytest.mark.asyncio
async def test_independent_tests_overlap_unified_proposal(db_session):
    ready = asyncio.Event()

    class ParallelProposal(ProposalLLM):
        async def generate_tests(self, goal, input_schema, output_schema):
            ready.set()
            return [{"input": {}, "expected_output": {"result": 42}}], LLMCallStats(model="test")

        async def propose(self, **kwargs):
            await asyncio.wait_for(ready.wait(), 1)
            return GenerationProposal(
                analysis=IntentAnalysis(),
                script=script_response(
                    "function run(input, context) return {result=42} end", risk_assessment="medium"
                ),
            ), LLMCallStats(model="test")

    response = await GenerationDispatcher(
        db_session, ParallelProposal(), Settings(script_language="lua")
    ).dispatch(
        GenerateRequest(intent="unified parallel tests", constraints={"reuse_policy": "force_new"})
    )
    assert response.validation.valid
    assert any(c.name == "test_case_0" for c in response.validation.checks)


@pytest.mark.asyncio
async def test_failed_primary_generation_uses_fallback_and_counts_attempts(db_session):
    from ritesmith.core.exceptions import LLMError

    fallback = ScriptedLLM().queue(
        "generate_lua", script_response("function run(input, context) return {result=42} end")
    )

    class Primary(ScriptedLLM):
        def fallback(self):
            return fallback

    primary = Primary().queue("generate_lua", LLMError("provider unavailable"))
    response = await GenerationService(
        db_session, primary, Settings(script_language="lua", generation_parallel_tests=False)
    ).generate_lua(
        GenerateScriptRequest(
            intent="fallback unique test", constraints={"reuse_policy": "force_new"}
        )
    )
    assert response.validation.valid
    job = await db_session.scalar(
        select(GenerationJob).where(GenerationJob.goal == "fallback unique test")
    )
    assert job.attempts == 2
    assert len(primary.calls) == len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_all_failed_workflow_calls_are_invalid(db_session):
    from ritesmith.core.exceptions import LLMError
    from ritesmith.core.workflow_generation import WorkflowGenerationService

    llm = ScriptedLLM().queue("generate_workflow", *[LLMError("invalid JSON") for _ in range(2)])
    response = await WorkflowGenerationService(db_session, llm, Settings()).generate_workflow(
        GenerateWorkflowRequest(
            intent="failed workflow unique", constraints={"reuse_policy": "force_new"}, save=True
        )
    )
    assert not response.validation.valid
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_explicit_script_plan_preserves_network_permission_and_constraints(db_session):
    llm = ScriptedLLM().queue(
        "generate_lua", script_response("function run(input, context) return {ok=true} end")
    )
    response = await PlanBuilder(
        db_session, llm, Settings(script_language="lua", generation_parallel_tests=False)
    ).build_plan(
        CreatePlanRequest(
            intent="fetch a web page",
            requested_artifact_types=["lua_script"],
            reuse_policy="force_new",
            constraints={"allow_network": True, "max_lines": 12, "max_http_calls": 1},
        )
    )
    assert response.validations[0].valid
    constraints = llm.called("generate_lua")[0]["constraints"]
    assert constraints["runtime_profile"] == "readonly_network"
    assert constraints["max_lines"] == 12 and constraints["max_http_calls"] == 1
    assert not llm.called("analyze_intent")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/generate", "/generate/lua", "/generate/trama-workflow", "/plans"]
)
async def test_http_rejects_unknown_example_with_422(make_client, path):
    async with make_client(llm=ScriptedLLM()) as client:
        response = await client.post(
            path, json={"intent": "test", "context": {"workflow_examples": ["unknown"]}}
        )
    assert response.status_code == 422
