"""Intent verification: the plan's readback against the user's own words."""

import json
from types import SimpleNamespace

import pytest

from ritesmith.config import Settings, get_settings
from ritesmith.core.intent_verification import verify_intent
from ritesmith.tests.unit.test_plans_api import MockPlanLLM

PRESENCE = {"person_alias": "ana", "message": "Crédito do Caio.", "channel": "telegram"}
USER = [
    "Me lembra de 6 em 6 meses de colocar mais crédito no celular do Caio",
    "A primeira data é hoje + 6 meses. Lembra no telegram",
    "Bota 15 de maio de 2027 as 8 da manha",
]


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    from ritesmith.runtime import host_functions

    monkeypatch.setattr(
        host_functions,
        "get_available_provider_capabilities",
        lambda: [{"capability_name": "home.execute"}, {"capability_name": "telegram.send"}],
    )


class FakeResponses:
    def __init__(self, verdict="mismatch", fail=False):
        self.verdict, self.fail, self.calls = verdict, fail, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("slow")
        body = {
            "verdict": self.verdict,
            "reason": "pedido: data fixa; construído: chegada",
            "requested": {"trigger": "time"},
            "built": {"trigger": "arrival"},
        }
        return SimpleNamespace(
            output_text=json.dumps(body), usage=SimpleNamespace(input_tokens=900, output_tokens=120)
        )


def llm_with(responses):
    llm = MockPlanLLM()
    llm.client = SimpleNamespace(responses=responses)
    return llm


def settings(mode, **kw):
    return get_settings().model_copy(
        update={
            "intent_verification": mode,
            "llm_workflow_model": "gpt-6.1-terra",
            "local_timezone": "America/Sao_Paulo",
            **kw,
        }
    )


async def create(make_client, llm, mode):
    async with make_client(llm=llm, settings=settings(mode)) as c:
        r = await c.post(
            "/plans",
            json={
                "intent": "Lembrar em 15/05/2027 às 08:00 e repetir a cada 6 meses.",
                "mode": "propose",
                "context": {"presence_reminder": PRESENCE, "user_request": USER},
            },
        )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio(loop_scope="session")
async def test_shadow_records_the_mismatch_without_blocking(make_client):
    responses = FakeResponses("mismatch")
    plan = await create(make_client, llm_with(responses), "shadow")
    assert plan["readback"].startswith("• Quando ana chegar em casa")
    assert plan["verification"]["verdict"] == "mismatch"
    assert plan["verification"]["mode"] == "shadow" and plan["status"] != "blocked"
    sent = responses.calls[0]
    assert sent["model"] == "gpt-6.1-terra" and sent["reasoning"] == {"effort": "low"}
    content = sent["input"][0]["content"]
    assert "Bota 15 de maio de 2027" in content and "Quando ana chegar em casa" in content


@pytest.mark.asyncio(loop_scope="session")
async def test_enforce_blocks_a_mismatch_with_the_reason(make_client):
    plan = await create(make_client, llm_with(FakeResponses("mismatch")), "enforce")
    assert plan["status"] == "blocked"
    assert "não corresponde ao pedido" in plan["policy_decision"]["reason"]
    assert "chegada" in plan["policy_decision"]["reason"]


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("verdict", ["match", "unsure"])
async def test_enforce_passes_match_and_unsure(make_client, verdict):
    plan = await create(make_client, llm_with(FakeResponses(verdict)), "enforce")
    assert plan["status"] != "blocked" and plan["verification"]["verdict"] == verdict


@pytest.mark.asyncio(loop_scope="session")
async def test_errors_fail_open_and_off_never_calls(make_client):
    plan = await create(make_client, llm_with(FakeResponses(fail=True)), "enforce")
    assert plan["status"] != "blocked" and plan["verification"]["verdict"] == "error"
    responses = FakeResponses()
    plan = await create(make_client, llm_with(responses), "off")
    assert plan["verification"] is None and responses.calls == []


@pytest.mark.asyncio
async def test_bad_json_or_unknown_verdict_is_an_error():
    class Weird(FakeResponses):
        async def create(self, **kwargs):
            return SimpleNamespace(output_text='{"verdict": "probably"}', usage=None)

    client = SimpleNamespace(responses=Weird())
    out = await verify_intent(
        client, Settings(), user_request="x", intent="x", readback="• y", now_local="sábado"
    )
    assert out["verdict"] == "error"


class Sequence(FakeResponses):
    """mismatch on the first check, match after the regeneration."""

    def __init__(self, verdicts):
        super().__init__()
        self.verdicts = list(verdicts)

    async def create(self, **kwargs):
        self.verdict = self.verdicts.pop(0)
        return await super().create(**kwargs)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("second, blocked", [("match", False), ("mismatch", True)])
async def test_enforce_regenerates_once_with_the_feedback(
    make_client, monkeypatch, second, blocked
):
    from ritesmith.core.planning import PlanBuilder

    seen = {}

    async def regenerate(self, req, plan_id, verification):
        seen["reason"] = verification["reason"]
        step, artifact, validation = await PlanBuilder._handle_workflow_artifact(
            self,
            req=req.model_copy(update={"context": {"presence_reminder": PRESENCE}}),
            plan_id=plan_id,
            step_id="step_1",
        )
        return step, artifact, validation

    monkeypatch.setattr(PlanBuilder, "_can_regenerate", staticmethod(lambda req, artifacts: True))
    monkeypatch.setattr(PlanBuilder, "_regenerate_with_feedback", regenerate)
    responses = Sequence(["mismatch", second])
    plan = await create(make_client, llm_with(responses), "enforce")
    assert len(responses.calls) == 2
    assert seen["reason"] == "pedido: data fixa; construído: chegada"
    assert plan["verification"]["retried_after"]["verdict"] == "mismatch"
    assert (plan["status"] == "blocked") is blocked


def test_recipes_are_never_regenerated():
    from ritesmith.core.planning import PlanBuilder
    from ritesmith.schemas.plan import CreatePlanRequest

    req = CreatePlanRequest(intent="x", context={"presence_reminder": PRESENCE})
    wf = SimpleNamespace(artifact_type=SimpleNamespace(value="trama_workflow"))
    assert PlanBuilder._can_regenerate(req, [wf]) is False
    assert PlanBuilder._can_regenerate(CreatePlanRequest(intent="x"), [wf]) is True
    assert PlanBuilder._can_regenerate(CreatePlanRequest(intent="x"), [wf, wf]) is False


@pytest.mark.asyncio(loop_scope="session")
async def test_regeneration_request_carries_the_feedback(monkeypatch):
    from ritesmith.core.planning import PlanBuilder
    from ritesmith.schemas.plan import CreatePlanRequest

    captured = {}

    async def handle(self, req, plan_id, step_id, **kw):
        captured["context"] = req.context
        captured["reuse"] = req.reuse_policy
        return "step", "artifact", None

    monkeypatch.setattr(PlanBuilder, "_handle_workflow_artifact", handle)
    builder = object.__new__(PlanBuilder)
    req = CreatePlanRequest(intent="x", context={"user_request": USER})
    out = await builder._regenerate_with_feedback(
        req, "plan_x", {"reason": "faltou desligar o ar", "requested": {}, "built": {}}
    )
    assert out == ("step", "artifact", None)
    assert captured["context"]["verifier_feedback"]["reason"] == "faltou desligar o ar"
    assert captured["context"]["user_request"] == USER and captured["reuse"].value == "force_new"
