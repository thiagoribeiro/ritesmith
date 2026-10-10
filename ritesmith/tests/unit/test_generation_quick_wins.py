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
