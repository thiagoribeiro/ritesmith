"""Provider registration at startup, the unified /generate dispatcher, and IdempotencyService."""

import pytest
from sqlalchemy import func, select

from ritesmith.core.exceptions import ConflictError
from ritesmith.core.idempotency import IdempotencyService
from ritesmith.core.provider_registration import register_all_providers
from ritesmith.registry.models import Capability, ProviderManifest
from ritesmith.runtime.providers import PROVIDERS
from ritesmith.tests.factories import unique
from ritesmith.tests.fakes.llm import (
    ScriptedLLM,
    intent,
    script_response,
    workflow_response,
)

# ---------------------------------------------------------------------------
# register_all_providers
# ---------------------------------------------------------------------------


async def _manifests(db) -> dict[str, str]:
    rows = (await db.execute(select(ProviderManifest))).scalars().all()
    return {row.provider_id: row.status for row in rows}


async def test_registers_every_provider_with_its_availability(db_session):
    await register_all_providers(db_session)
    manifests = await _manifests(db_session)
    for provider in PROVIDERS:
        expected = "active" if provider.is_available() else "unavailable"
        assert manifests[provider.namespace] == expected


async def test_capabilities_only_for_available_providers(db_session):
    await register_all_providers(db_session)
    rows = (await db_session.execute(select(Capability.provider_id).distinct())).scalars().all()
    available = {p.namespace for p in PROVIDERS if p.is_available()}
    assert set(rows) <= available


async def test_registration_is_idempotent(db_session):
    await register_all_providers(db_session)
    count_manifests = await db_session.scalar(select(func.count()).select_from(ProviderManifest))
    count_caps = await db_session.scalar(select(func.count()).select_from(Capability))
    await register_all_providers(db_session)
    assert (
        await db_session.scalar(select(func.count()).select_from(ProviderManifest))
        == count_manifests
    )
    assert await db_session.scalar(select(func.count()).select_from(Capability)) == count_caps


async def test_a_failing_provider_does_not_block_the_others(db_session, monkeypatch):
    broken = PROVIDERS[0]

    def boom():
        raise RuntimeError("manifest exploded")

    monkeypatch.setattr(broken, "manifest", boom)
    await register_all_providers(db_session)
    manifests = await _manifests(db_session)
    assert broken.namespace not in manifests
    assert all(p.namespace in manifests for p in PROVIDERS[1:])


# ---------------------------------------------------------------------------
# POST /generate (GenerationDispatcher)
# ---------------------------------------------------------------------------

LUA_SCRIPT = "function run(input, context) return { doubled = (input.value or 0) * 2 } end"


def _workflow_definition() -> dict:
    return {
        "name": "dispatcher_workflow",
        "version": "2.0.0",
        "entrypoint": "call",
        "nodes": [
            {
                "id": "call",
                "kind": "task",
                "action": {
                    "mode": "sync",
                    "request": {"url": "https://example.com/hook", "verb": "POST", "body": {}},
                    "successStatusCodes": [200],
                },
                "next": "end",
            }
        ],
    }


async def test_script_intent_routes_to_script_generation(make_client):
    llm = ScriptedLLM()
    llm.queue("analyze_intent", intent(requires_lua=True, requires_workflow=False))
    llm.queue("generate_lua", script_response(LUA_SCRIPT, name="doubler"))
    async with make_client(llm=llm) as client:
        resp = await client.post(
            "/generate",
            json={"intent": unique("double a value"), "constraints": {"reuse_policy": "force_new"}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["artifact"]["artifact_type"] == "lua_script"
    assert [name for name, _ in llm.calls] == ["analyze_intent", "generate_lua"]


async def test_workflow_intent_routes_to_workflow_generation(make_client):
    llm = ScriptedLLM()
    llm.queue("analyze_intent", intent(requires_lua=False, requires_workflow=True))
    llm.queue("generate_workflow", workflow_response(_workflow_definition()))
    async with make_client(llm=llm) as client:
        resp = await client.post(
            "/generate",
            json={"intent": unique("call a hook"), "constraints": {"reuse_policy": "force_new"}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["artifact"]["artifact_type"] == "trama_workflow"
    assert [name for name, _ in llm.calls] == ["analyze_intent", "generate_workflow"]


async def test_invalid_script_constraints_are_ignored(make_client):
    llm = ScriptedLLM()
    llm.queue("analyze_intent", intent(requires_workflow=False))
    llm.queue("generate_lua", script_response(LUA_SCRIPT))
    async with make_client(llm=llm) as client:
        resp = await client.post(
            "/generate",
            json={"intent": unique("double"), "constraints": {"max_lines": "not-a-number"}},
        )
    assert resp.status_code == 200, resp.text
    assert llm.called("generate_lua")[0]["constraints"] == {}


async def test_intent_receives_constraints_and_context(make_client):
    llm = ScriptedLLM()
    llm.queue("analyze_intent", intent(requires_workflow=False))
    llm.queue("generate_lua", script_response(LUA_SCRIPT))
    async with make_client(llm=llm) as client:
        await client.post(
            "/generate",
            json={
                "intent": unique("double"),
                "constraints": {"reuse_policy": "force_new"},
                "context": {"user": "u1"},
            },
        )
    call = llm.called("analyze_intent")[0]
    assert call["constraints"] == {"reuse_policy": "force_new"}
    assert call["context"] == {"user": "u1"}


# ---------------------------------------------------------------------------
# IdempotencyService
# ---------------------------------------------------------------------------


async def test_first_use_of_a_key_is_not_a_duplicate(db_session):
    service = IdempotencyService(db_session)
    assert await service.check_or_create(unique("key"), "executions") == (False, None)


async def test_in_flight_key_conflicts(db_session):
    service = IdempotencyService(db_session)
    key = unique("key")
    await service.check_or_create(key, "executions")
    with pytest.raises(ConflictError):
        await service.check_or_create(key, "executions")


async def test_completed_key_returns_the_original_execution(db_session):
    service = IdempotencyService(db_session)
    key = unique("key")
    await service.check_or_create(key, "executions")
    await service.mark_complete(key, "executions", "exec_123")
    assert await service.check_or_create(key, "executions") == (True, "exec_123")


async def test_mark_complete_on_unknown_key_is_a_noop(db_session):
    await IdempotencyService(db_session).mark_complete(unique("missing"), "executions", "exec_1")
