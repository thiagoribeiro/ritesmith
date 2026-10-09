"""Offline replay of the second frozen reliability delivery; no provider calls."""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import jsonschema
import pytest

from benchmarks.generation_latency.reliability import hidden_cases, notification_fixtures
from benchmarks.generation_latency.tasks import BY_ID
from ritesmith.config import Settings
from ritesmith.llm import prompts
from ritesmith.llm.base import LLMCallStats
from ritesmith.llm.generation_context import proposal_kind
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.llm.script_candidate import expand_script
from ritesmith.runtime.luau import luau_available
from ritesmith.runtime.providers.base import HostFunctionDef
from ritesmith.runtime.validation_session import FixtureError, LuauValidationSession, preflight
from ritesmith.schemas.test_spec import bind_known_fixtures

EVIDENCE = (
    Path(__file__).resolve().parents[3]
    / "benchmarks/generation_latency/results/reliability-native-20261008/stage1-spending.json"
)
EVIDENCE_SHA = "6f33c55a792583e14d7b1636b113ab1e43ed31e5b094bc81bd7032125489b720"
TASK = BY_ID["t3_notify_inactive"]
REFERENCE = """function run(input: Input, context: Context): Output
local listing = tools.crm.list({segment = input.segment})
if not listing.ok then return {error = listing.error, message = listing.message} end
local notified: {string} = {}
local failed: {string} = {}
local attempts = 0
for _, customer in listing.customers do
    if customer.last_seen_days >= input.inactive_days then
        if attempts >= 10 then break end
        attempts += 1
        local sent = tools.message.send({user = customer.id, text = `Hi {customer.name}, we miss you!`})
        if sent.ok then table.insert(notified, customer.id)
        else table.insert(failed, customer.id) end
    end
end
return {notified = notified, failed = failed, count = #notified}
end"""


def retained(method):
    assert hashlib.sha256(EVIDENCE.read_bytes()).hexdigest() == EVIDENCE_SHA
    calls = json.loads(EVIDENCE.read_text())["calls"]
    return json.loads(next(call["response_content"] for call in calls if call["method"] == method))


@pytest.fixture
def session(monkeypatch):
    if not luau_available():
        pytest.skip("lunardyson not installed")

    def never_live(**kwargs):
        raise AssertionError("Offline notification verification must never contact external tools")

    tools = {
        tool["name"]: HostFunctionDef(tool["name"], "notification", never_live)
        for tool in TASK["tools"]
    }
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.luau_tools_for_profile", lambda profile: tools
    )
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.script_type_declarations",
        lambda *args: "type Context = {[string]: any}\n" + TASK["types"],
    )
    signatures = {tool["name"]: tool["signature"] for tool in TASK["tools"]}
    monkeypatch.setattr(
        "ritesmith.runtime.validation_session.tool_signature",
        lambda name, definition: (signatures[name], None),
    )
    runtime = LuauValidationSession(
        Settings(), "notification", TASK["input_schema"], TASK["output_schema"]
    )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("goal", "kind"),
    [
        (TASK["goal"], "script"),
        ("Summarize every customer's balance", "script"),
        ("Notify every eligible customer every six hours", "mixed"),
        ("Query prices every 6 hours", "mixed"),
        ("Send a summary every weekday", "mixed"),
        ("Notify every customer a cada seis horas", "mixed"),
    ],
)
def test_quantified_customers_do_not_trigger_workflow_routing(goal, kind):
    assert proposal_kind(goal, TASK["input_schema"], TASK["output_schema"]) == kind


def test_original_notification_candidate_has_strict_union_errors(session):
    candidate = retained("proposal_workflow")["script"]
    script = expand_script(candidate)["script"]
    errors = session.check(script)["strict"]
    assert errors and any("Key 'error' is missing" in error["message"] for error in errors)


def test_original_independent_expectations_are_rejected_without_execution(session):
    cases = retained("test_gen")["test_cases"]
    for case in cases:
        case["source"] = "intent"
    bound = bind_known_fixtures(cases, notification_fixtures())
    with pytest.raises(jsonschema.ValidationError) as error:
        preflight(bound, session.tools, TASK["input_schema"], TASK["output_schema"])
    assert list(error.value.absolute_path) == ["message"]
    assert not session.counts
    # Removing nulls would not fix the contradictory business expectations.
    for case in bound:
        case["expected_output"] = {
            key: value for key, value in case["expected_output"].items() if value is not None
        }
    with pytest.raises(FixtureError, match="overlap"):
        preflight(bound, session.tools, TASK["input_schema"], TASK["output_schema"])


def test_discriminated_reference_passes_unchanged_cases_and_ten_attempt_limit(session):
    assert not session.check(REFERENCE)["strict"]
    for spec in preflight(
        hidden_cases(TASK), session.tools, TASK["input_schema"], TASK["output_schema"]
    ):
        result, fixture_error, errors = session.execute(REFERENCE, spec)
        assert result.ok and fixture_error is None and not errors
    assert session.counts["message.send"] == 10
    assert len(result.output["notified"]) == 9 and result.output["failed"] == ["c4"]


def test_counting_successes_instead_of_attempts_fails_the_original_limit(session):
    script = REFERENCE.replace("        attempts += 1\n", "").replace(
        "if sent.ok then table.insert(notified, customer.id)",
        "if sent.ok then attempts += 1; table.insert(notified, customer.id)",
    )
    spec = preflight(
        [hidden_cases(TASK)[-1]], session.tools, TASK["input_schema"], TASK["output_schema"]
    )[0]
    result, fixture_error, errors = session.execute(script, spec)
    assert result.ok and fixture_error is None
    assert session.counts["message.send"] == 11
    assert any("expected 10 calls, got 11" in error for error in errors)


def test_generation_and_repair_prompts_preserve_discriminated_tool_contracts():
    for system in (prompts.luau_generation_system(True), prompts.luau_repair_system()):
        assert "narrow tool-result unions" in system
        assert "check `result.error` before" not in system
    assert "including failed attempts" in prompts.luau_generation_system(True)
    assert "do not emit null unless the schema permits null" in prompts.test_generation_system()


@pytest.mark.asyncio
async def test_notification_proposal_uses_script_prompts_and_script_model():
    provider = OpenAIProvider(
        Settings(
            generation_typed_prompts=True,
            llm_script_model="gpt-5-mini",
            llm_workflow_model="gpt-5.4-mini",
        ),
        client=object(),
    )
    provider._chat_with_retry = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "analysis": {"artifact_types": ["lua_script"]},
                    "script": {
                        "script": REFERENCE,
                        "name": "notify_inactive",
                        "description": "Notify eligible customers with a bounded send count.",
                    },
                }
            ),
            LLMCallStats(model="test"),
        )
    )
    proposal, _ = await provider.propose(
        goal=TASK["goal"],
        constraints={},
        input_schema=TASK["input_schema"],
        output_schema=TASK["output_schema"],
        profile="notification",
    )
    args = provider._chat_with_retry.call_args.args
    assert args[5] == "proposal_script"
    assert provider.operation_config(args[0], args[5])[0] == "gpt-5-mini"
    assert "semantic_plan" not in args[1] and "CAPABILITY INVENTORY" not in args[2]
    assert proposal.script.script == REFERENCE
