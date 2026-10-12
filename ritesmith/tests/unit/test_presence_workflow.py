"""Arrival reminders use a verified one-shot recipe, never model-inferred IO."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ritesmith.config import Settings
from ritesmith.core import workflow_generation as module
from ritesmith.core.presence_workflow import PresenceReminder, presence_arrival_workflow
from ritesmith.schemas.generation import GenerateWorkflowRequest
from ritesmith.workflows.validator import WorkflowValidator

SPEC = {"person_alias": "thiago", "message": "tomar os remédios", "channel": "telegram"}


@pytest.mark.parametrize(
    "change",
    [
        {"person_alias": ""},
        {"message": " "},
        {"message": "x" * 2001},
        {"channel": "voice"},
        {"repeat": True},
        {"person_alias": 123},
    ],
)
def test_presence_contract_rejects_unsupported_or_missing_parameters(change):
    with pytest.raises(ValidationError):
        PresenceReminder.model_validate({**SPEC, **change})


def test_recipe_validates_with_real_capability_contracts():
    recipe = presence_arrival_workflow(PresenceReminder(**SPEC), "http://ritesmith:8081/")
    caps = {"home.execute": {}, "telegram.send": {}}
    assert WorkflowValidator(caps).validate(recipe) == []
    calls = [n for n in recipe["nodes"] if n["kind"] == "task"]
    notifications = [
        n for n in calls if n["action"]["request"]["body"]["capability_name"] == "telegram.send"
    ]
    assert len(notifications) == 1 and notifications[0]["next"] == "end"
    assert notifications[0]["action"]["request"]["body"]["input"] == {"text": SPEC["message"]}
    for n in calls:
        r = n["action"]["request"]
        assert r["url"] == "http://ritesmith:8081/trama/execute"
        if n not in notifications:
            assert r["body"] == {
                "capability_name": "home.execute",
                "input": {"capability": "presence.read", "target": "thiago", "input": {}},
            }
    assert "stat.tick" not in json.dumps(recipe) and "casp.execute" not in json.dumps(recipe)


@pytest.fixture
def service(monkeypatch):
    from ritesmith.runtime import host_functions

    llm = AsyncMock()
    db = AsyncMock()
    svc = module.WorkflowGenerationService(
        db=db, llm=llm, settings=Settings(generation_max_attempts=1)
    )
    monkeypatch.setattr(svc, "_get_capabilities", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        host_functions,
        "get_available_provider_capabilities",
        lambda: [{"capability_name": "home.execute"}, {"capability_name": "telegram.send"}],
    )
    monkeypatch.setattr(module, "check_reuse", AsyncMock())
    monkeypatch.setattr(module, "fts_search", AsyncMock())
    return svc


@pytest.mark.asyncio
async def test_service_uses_recipe_and_never_reuses_old_reminder(service):
    request = GenerateWorkflowRequest(
        intent="Lembrar quando chegar", context={"presence_reminder": SPEC}, save=False
    )
    result = await service.generate_workflow(request, plan_id="plan_test")
    assert result.validation.valid and not result.reused
    recipe = json.loads(result.artifact.content)
    notify = next(n for n in recipe["nodes"] if n["id"] == "notify")
    assert notify["next"] == "rs_complete"
    service.llm.generate_workflow.assert_not_awaited()
    service.llm.repair_workflow.assert_not_awaited()
    module.check_reuse.assert_not_awaited()
    module.fts_search.assert_not_awaited()
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [[], ["home.execute"], ["telegram.send"]])
async def test_service_refuses_missing_runtime_capabilities(service, monkeypatch, available):
    from ritesmith.runtime import host_functions

    monkeypatch.setattr(
        host_functions,
        "get_available_provider_capabilities",
        lambda: [{"capability_name": name} for name in available],
    )
    request = GenerateWorkflowRequest(intent="Lembrar", context={"presence_reminder": SPEC})
    with pytest.raises(ValueError, match="unavailable"):
        await service.generate_workflow(request)
    service.llm.generate_workflow.assert_not_awaited()
    service.db.commit.assert_not_awaited()
