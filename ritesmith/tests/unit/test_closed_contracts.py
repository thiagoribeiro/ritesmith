"""Closed capability contracts: generation-time validation, the /trama/execute bridge and
the capability registry agree on the real provider schemas.

Regressions for two r2d2 workflows (2026-10-11) that were accepted and then failed in
Trama: an extra `previous_state` argument to llm.evaluate (HTTP 500 from the callable)
and a reference to `output.result` on market.exchange_rate, whose field is `rate`.
"""

from sqlalchemy import select

from ritesmith.config import Settings
from ritesmith.core.provider_registration import register_all_providers
from ritesmith.registry.models import Capability
from ritesmith.runtime.providers import PROVIDERS
from ritesmith.runtime.providers.base import invoke_capability
from ritesmith.runtime.providers.reports import _path_for
from ritesmith.tests.fakes.llm import ScriptedLLM
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.semantic_validation import validate_semantics

BASE = "http://ritesmith:8081"
TOKEN = "trama-secret"

# Every provider contract, independent of which providers are configured in this process.
REGISTRY = {
    name: {
        "capability_name": name,
        "input_schema": fd.input_schema,
        "output_schema": fd.output_schema,
    }
    for provider in PROVIDERS
    for name, fd in provider.lua_functions().items()
}


def _errors(steps):
    return validate_semantics(compile_plan({"name": "t", "steps": steps}, BASE), REGISTRY)


FETCH = {
    "kind": "call",
    "id": "fetch",
    "capability_name": "market.exchange_rate",
    "args": {"from_currency": "usd", "to_currency": "brl"},
}


def _notify(text_parts):
    return {
        "kind": "call",
        "id": "notify",
        "capability_name": "telegram.send",
        "args": {"text": {"type": "template", "parts": text_parts}},
    }


def test_unknown_input_is_rejected_with_and_without_templates():
    literal = {
        "kind": "call",
        "id": "decide",
        "capability_name": "llm.evaluate",
        "args": {"task": "judge", "previous_state": 1},
    }
    templated = {
        **literal,
        "args": {"task": "judge", "previous_state": {"ref": "fetch", "path": "rate"}},
    }
    for step in (literal, templated):
        errors = _errors([FETCH, step])
        assert any("unknown input 'previous_state' for llm.evaluate" in e for e in errors), errors
        assert any("accepted: current, data, previous, task" in e for e in errors)


def test_unknown_output_field_is_rejected_and_known_fields_pass():
    errors = _errors([FETCH, _notify(["USD/BRL ", {"ref": "fetch", "path": "result"}])])
    assert any("unknown output field 'result'" in e and "rate" in e for e in errors), errors
    assert not _errors([FETCH, _notify(["USD/BRL ", {"ref": "fetch", "path": "rate"}])])
    # Tools report failures inline; reading them is always allowed.
    assert not _errors([FETCH, _notify(["err ", {"ref": "fetch", "path": "error"}])])


def test_array_output_is_read_through_result():
    today = {"kind": "call", "id": "today", "capability_name": "calendar.get_today", "args": {}}
    wrong = _errors([today, _notify(["x ", {"ref": "today", "path": "events"}])])
    assert any("is a array; read it as output.result" in e for e in wrong), wrong
    assert not _errors([today, _notify(["x ", {"ref": "today", "path": "result.0.summary"}])])


def test_single_args_convention_is_invoked_with_the_dict():
    result = invoke_capability(_path_for, {"theme": "sales", "description": "weekly"})
    assert isinstance(result, dict) and "path" in result


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


async def test_bridge_rejects_unknown_and_missing_inputs_with_422(make_client):
    async with make_client(llm=ScriptedLLM(), settings=Settings(trama_token=TOKEN)) as client:
        unknown = await client.post(
            "/trama/execute",
            json={"capability_name": "stat.min_value", "input": {"current": 3, "bogus": 1}},
            headers=_headers(),
        )
        missing = await client.post(
            "/trama/execute",
            json={"capability_name": "stat.min_value", "input": {}},
            headers=_headers(),
        )
        ok = await client.post(
            "/trama/execute",
            json={"capability_name": "stat.min_value", "input": {"current": 3, "previous_min": 5}},
            headers=_headers(),
        )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["unknown"] == ["bogus"]
    assert missing.status_code == 422 and missing.json()["detail"]["missing"] == ["current"]
    assert ok.status_code == 200 and ok.json()["output"]["min_value"] == 3


async def test_bridge_maps_callable_type_errors_to_422(make_client):
    # text.* built-ins declare no schema, so the callable itself rejects the argument.
    async with make_client(llm=ScriptedLLM(), settings=Settings(trama_token=TOKEN)) as client:
        resp = await client.post(
            "/trama/execute",
            json={"capability_name": "text.slugify", "input": {"wrong": "x"}},
            headers=_headers(),
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_input"


async def test_registry_stores_and_refreshes_real_contracts(db_session):
    await register_all_providers(db_session)
    row = (
        await db_session.execute(
            select(Capability).where(Capability.capability_id == "market.exchange_rate")
        )
    ).scalar_one()
    assert set(row.output_schema["properties"]) == {"from", "to", "rate", "timestamp"}
    assert row.input_schema["required"] == ["from_currency", "to_currency"]
    # Rows written by older releases with empty contracts are refreshed on the next boot.
    row.input_schema, row.output_schema = {}, {}
    await db_session.flush()
    await register_all_providers(db_session)
    await db_session.refresh(row)
    assert row.output_schema["properties"]["rate"]


def test_repair_prompt_carries_contracts_of_capabilities_used():
    from ritesmith.core.workflow_generation import _used_contracts
    from ritesmith.llm import prompts

    candidate = compile_plan({"name": "t", "steps": [FETCH]}, BASE)
    contracts = _used_contracts(candidate, REGISTRY)
    assert [c["name"] for c in contracts] == ["market.exchange_rate"]
    assert "rate" in contracts[0]["out"]["properties"]
    user = prompts.workflow_repair_user(
        "goal", candidate, ["err"], 1, "{}", capability_contracts=contracts
    )
    assert "CONTRACTS OF THE CAPABILITIES USED" in user and '"rate"' in user
