"""Plan state machine (_ALLOWED_TRANSITIONS), complete/replan, and the completion callback."""

import asyncio

import pytest

from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.exceptions import InvalidTransitionError
from ritesmith.core.planning import _ALLOWED_TRANSITIONS, PlanBuilder, _is_safe_callback_url
from ritesmith.schemas.plan import (
    ApprovePlanRequest,
    CompletePlanRequest,
    PlanStatus,
    RejectPlanRequest,
)
from ritesmith.tests.factories import make_plan


def _builder(db, **settings_overrides) -> PlanBuilder:
    # No LLM: these tests drive only the transition methods, never build_plan.
    return PlanBuilder(
        db=db, llm=None, settings=Settings(**settings_overrides), audit=AuditLogger(db)
    )


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

ALL_STATES = list(PlanStatus)


def test_terminal_states_have_no_outgoing_transitions():
    for terminal in (
        PlanStatus.rejected,
        PlanStatus.blocked,
        PlanStatus.completed,
        PlanStatus.superseded,
    ):
        assert _ALLOWED_TRANSITIONS.get(terminal, set()) == set()


@pytest.mark.parametrize(
    ("start", "target", "method"),
    [
        ("proposed", PlanStatus.approved, "approve"),
        ("proposed", PlanStatus.rejected, "reject"),
        ("approved", PlanStatus.completed, "complete"),
        ("persisted", PlanStatus.completed, "complete"),
        ("executing", PlanStatus.failed, "complete_failed"),
    ],
)
async def test_allowed_transitions_succeed(db_session, start, target, method):
    plan = await make_plan(db_session, status=start)
    builder = _builder(db_session)
    if method == "approve":
        result = await builder.approve_plan(plan.plan_id, ApprovePlanRequest())
    elif method == "reject":
        result = await builder.reject_plan(plan.plan_id, RejectPlanRequest(reason="no"))
    elif method == "complete":
        result = await builder.complete_plan(plan.plan_id, CompletePlanRequest(status="completed"))
    else:
        result = await builder.complete_plan(plan.plan_id, CompletePlanRequest(status="failed"))
    assert result.status == target


@pytest.mark.parametrize("start", ["approved", "rejected", "completed", "blocked", "superseded"])
async def test_approving_from_a_non_proposed_state_is_rejected(db_session, start):
    plan = await make_plan(db_session, status=start)
    with pytest.raises(InvalidTransitionError):
        await _builder(db_session).approve_plan(plan.plan_id, ApprovePlanRequest())


async def test_completing_a_rejected_plan_is_rejected(db_session):
    plan = await make_plan(db_session, status="rejected")
    with pytest.raises(InvalidTransitionError):
        await _builder(db_session).complete_plan(
            plan.plan_id, CompletePlanRequest(status="completed")
        )


async def test_double_approve_is_rejected(db_session):
    plan = await make_plan(db_session, status="proposed")
    builder = _builder(db_session)
    await builder.approve_plan(plan.plan_id, ApprovePlanRequest())
    with pytest.raises(InvalidTransitionError):
        await builder.approve_plan(plan.plan_id, ApprovePlanRequest())


# ---------------------------------------------------------------------------
# replan + budget
# ---------------------------------------------------------------------------


async def test_replan_exhausted_returns_none_and_leaves_plan_untouched(db_session):
    plan = await make_plan(db_session, status="failed", context={"replan_count": 2})
    result = await _builder(db_session, casp_max_replan_attempts=2).replan(plan.plan_id, "boom")
    assert result is None
    await db_session.refresh(plan)
    assert plan.status == "failed"  # not superseded


async def test_replan_from_a_non_failed_state_is_rejected(db_session):
    plan = await make_plan(db_session, status="approved", context={"replan_count": 0})
    with pytest.raises(InvalidTransitionError):
        await _builder(db_session, casp_max_replan_attempts=2).replan(plan.plan_id, "boom")


# ---------------------------------------------------------------------------
# Completion callback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://example.com/hook", True),
        ("http://example.com/hook", True),
        ("http://localhost:9000/x", False),
        ("http://127.0.0.1/x", False),
        ("http://10.0.0.5/x", False),
        ("http://192.168.1.1/x", False),
        ("ftp://example.com/x", False),
        (" not-a-url ", False),
    ],
)
def test_is_safe_callback_url(url, safe):
    assert _is_safe_callback_url(url) is safe


async def test_completion_fires_callback_for_a_safe_url(db_session, httpx_mock, monkeypatch):
    httpx_mock.add_response(method="POST", url="https://hooks.test/done", status_code=200)
    fired = asyncio.Event()
    real_fire = __import__("ritesmith.core.planning", fromlist=["_fire_callback"])._fire_callback

    async def tracking_fire(*args):
        await real_fire(*args)
        fired.set()

    monkeypatch.setattr("ritesmith.core.planning._fire_callback", tracking_fire)
    plan = await make_plan(db_session, status="approved", callback_url="https://hooks.test/done")
    await _builder(db_session).complete_plan(
        plan.plan_id, CompletePlanRequest(status="completed", summary="ok")
    )

    await asyncio.wait_for(fired.wait(), timeout=2)
    request = httpx_mock.get_request()
    assert request is not None
    import json

    body = json.loads(request.read())
    assert body == {"plan_id": plan.plan_id, "status": "completed", "summary": "ok"}


@pytest.mark.parametrize(
    "private_url", ["http://127.0.0.1/x", "http://10.1.2.3/x", "http://192.168.0.1/x"]
)
async def test_completion_does_not_call_a_private_callback(
    db_session, httpx_mock, monkeypatch, private_url
):
    done = asyncio.Event()
    real_fire = __import__("ritesmith.core.planning", fromlist=["_fire_callback"])._fire_callback

    async def tracking_fire(*args):
        await real_fire(*args)
        done.set()

    monkeypatch.setattr("ritesmith.core.planning._fire_callback", tracking_fire)
    plan = await make_plan(db_session, status="approved", callback_url=private_url)
    await _builder(db_session).complete_plan(plan.plan_id, CompletePlanRequest(status="completed"))

    await asyncio.wait_for(done.wait(), timeout=2)
    assert httpx_mock.get_request() is None  # blocked before any HTTP call


@pytest.mark.xfail(
    strict=True, reason="SSRF gap: callback blocklist misses link-local and 172.17-31 (Phase 0)"
)
@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://172.20.0.1/x"])
def test_callback_blocklist_gap(url):
    # Documents a known hole until the unified is_public_url (DNS-resolving) guard lands.
    assert _is_safe_callback_url(url) is False


async def test_complete_plan_via_api(make_client, db_session):
    from ritesmith.tests.fakes.llm import ScriptedLLM

    plan = await make_plan(db_session, status="approved")
    async with make_client(llm=ScriptedLLM()) as client:
        resp = await client.post(
            f"/plans/{plan.plan_id}/complete", json={"status": "completed", "summary": "done"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
