"""Regressions for rejections observed in the 2026-10-10 scorecard run; no provider calls."""

import json
from unittest.mock import AsyncMock

import pytest

from ritesmith.config import Settings
from ritesmith.llm.base import LLMCallStats
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.tests.unit.test_generation_latency import BASE, example
from ritesmith.tests.unit.test_notification_recovery import REFERENCE, TASK
from ritesmith.workflows.compact import compile_workflow


def _provider(response):
    provider = OpenAIProvider(Settings(generation_typed_prompts=True), client=object())
    provider._chat_with_retry = AsyncMock(
        return_value=(json.dumps(response), LLMCallStats(model="test"))
    )
    return provider


async def _propose(provider):
    return await provider.propose(
        goal=TASK["goal"],
        constraints={},
        input_schema=TASK["input_schema"],
        output_schema=TASK["output_schema"],
        profile="notification",
    )


@pytest.mark.asyncio
async def test_copied_artifact_type_placeholder_does_not_discard_valid_script():
    provider = _provider(
        {
            "analysis": {"artifact_types": ["lua_script OR trama_workflow"]},
            "script": {"script": REFERENCE, "name": "notify_inactive", "description": "d"},
        }
    )
    proposal, _ = await _propose(provider)
    assert proposal.script.script == REFERENCE
    assert proposal.analysis.requires_lua and not proposal.analysis.requires_workflow


@pytest.mark.asyncio
async def test_proposal_shape_has_no_copyable_placeholders():
    provider = _provider(
        {"analysis": {}, "script": {"script": REFERENCE, "name": "n", "description": "d"}}
    )
    await _propose(provider)
    user = provider._chat_with_retry.call_args.args[2]
    assert "artifact_types" not in user
    assert " OR trama_workflow" not in user and "<workflow definition" not in user


def test_compiler_ignores_null_fields_and_defaults_null_next():
    compact = example("linear")
    compact["version_extra"] = None
    compact["nodes"][-1]["next"] = None
    assert compile_workflow(compact, BASE) == compile_workflow(example("linear"), BASE)


def _semantic_provider(definition):
    from ritesmith.workflows.constructors import semantic_example

    provider = _provider(
        {
            "analysis": {},
            "workflow": {
                "name": "w",
                "description": "d",
                "definition": definition(semantic_example) if callable(definition) else definition,
            },
        }
    )
    provider.settings = Settings(generation_semantic_workflows=True)
    return provider


@pytest.mark.asyncio
async def test_semantic_mode_prompts_one_grammar_and_a_plan_shape():
    provider = _semantic_provider(lambda ex: {"format": "semantic_plan", "plan": ex("linear")})
    await provider.propose(
        goal="Monitor BTC for 7 days",
        constraints={},
        context={"duration_days": 7, "runs_per_day": 4, "schedule_hours": 6},
    )
    system, user = provider._chat_with_retry.call_args.args[1:3]
    assert "semantic_plan" in system and "compact_graph" not in system
    assert "Trama v2 is a graph" not in system and "stat.tick" not in system
    assert '"format":"semantic_plan"' in user
    assert "repeat count = 28" in user and "max_iterations" not in user


@pytest.mark.asyncio
async def test_semantic_mode_accepts_bare_plan():
    provider = _semantic_provider(lambda ex: ex("linear"))
    proposal, _ = await provider.propose(goal="Send BTC price", constraints={})
    assert proposal.workflow.definition["nodes"]


@pytest.mark.asyncio
async def test_semantic_mode_rejects_graph_definitions():
    from ritesmith.core.exceptions import LLMError

    provider = _semantic_provider(example("linear"))
    with pytest.raises(LLMError, match="semantic_plan"):
        await provider.propose(goal="Send BTC price", constraints={})


def test_count_repeat_final_action_reads_last_pass_without_early_exits():
    from ritesmith.workflows.constructors import semantic_example
    from ritesmith.workflows.semantic import compile_plan
    from ritesmith.workflows.semantic_validation import uninitialized_reference_errors

    plan = semantic_example("fixed_samples")
    plan["steps"].append(
        {
            "kind": "call",
            "id": "notify",
            "capability_name": "telegram.send",
            "args": {
                "text": {
                    "type": "template",
                    "parts": ["S: ", {"ref": "samples", "path": "samples"}],
                }
            },
        }
    )
    definition = compile_plan(plan, BASE)
    assert uninitialized_reference_errors(definition) == []
    switches = [n for n in definition["nodes"] if n["kind"] == "switch"]
    assert len(switches) == 1  # only the last pass can finish a count-only block


def test_repeat_id_reads_previous_state_inside_and_final_state_after():
    from ritesmith.workflows.semantic import compile_plan
    from ritesmith.workflows.semantic_validation import uninitialized_reference_errors

    state = {"ref": "monitor", "path": "minimum"}
    plan = {
        "name": "min",
        "steps": [
            {
                "kind": "repeat",
                "id": "monitor",
                "interval_seconds": 60,
                "count": 3,
                "state": {"minimum": {"ref": "min", "path": "min_value"}},
                "initial_state": {"minimum": None},
                "steps": [
                    {
                        "kind": "call",
                        "id": "fetch",
                        "capability_name": "market.coin_price",
                        "args": {"symbol": "eth"},
                    },
                    {
                        "kind": "state",
                        "id": "min",
                        "capability_name": "stat.min_value",
                        "args": {
                            "current": {"ref": "fetch", "path": "price"},
                            "previous_min": state,
                        },
                    },
                ],
            },
            {
                "kind": "call",
                "id": "notify",
                "capability_name": "telegram.send",
                "args": {"text": {"type": "template", "parts": ["Min: ", state]}},
            },
        ],
    }
    definition = compile_plan(plan, BASE)
    assert uninitialized_reference_errors(definition) == []
    inputs = {
        n["id"]: n["action"]["request"]["body"]["input"]
        for n in definition["nodes"]
        if n["kind"] == "task"
    }
    assert inputs["min__rs_1"]["previous_min"] is None
    assert (
        "monitor_tick__rs_1.response.body.output.state.minimum"
        in inputs["min__rs_2"]["previous_min"]
    )
    assert "monitor_tick.response.body.output.state.minimum" in inputs["notify"]["text"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("minimum", "state.minimum"),
        ("state.minimum", "state.minimum"),
        ("iteration", "iteration"),
    ],
)
def test_repeat_alias_keeps_explicit_state_prefix_and_counters(path, expected):
    from ritesmith.workflows.semantic import _repeat_state_aliases

    plan = {
        "steps": [
            {"kind": "repeat", "id": "m", "steps": [{"args": {"x": {"ref": "m", "path": path}}}]}
        ]
    }
    aliased = _repeat_state_aliases(plan)["steps"][0]["steps"][0]["args"]["x"]
    assert aliased == {"ref": "m_tick", "path": expected}


@pytest.mark.asyncio
async def test_proposal_without_analysis_is_accepted():
    provider = _provider({"script": {"script": REFERENCE, "name": "n", "description": "d"}})
    proposal, _ = await _propose(provider)
    assert proposal.analysis.requires_lua and proposal.script.script == REFERENCE
