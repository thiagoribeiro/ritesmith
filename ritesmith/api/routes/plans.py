import asyncio
import logging

from fastapi import APIRouter, Depends, Query

from ritesmith.api.deps import get_plan_builder
from ritesmith.core.planning import PlanBuilder
from ritesmith.schemas.plan import (
    ApprovePlanRequest,
    CancelPlanRequest,
    CompletePlanRequest,
    CreatePlanRequest,
    GenerationMode,
    Plan,
    PlanStatus,
    RejectPlanRequest,
)

router = APIRouter(prefix="/plans", tags=["plans"])

log = logging.getLogger(__name__)


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()):
        log.error("background_task_failed task=%s: %s", task.get_name(), exc, exc_info=exc)


async def _auto_execute(artifact_id: str, plan_id: str) -> None:
    """Fire-and-forget: run artifact via ExecutionService with a fresh DB session."""
    try:
        from sqlalchemy import select

        from ritesmith.config import get_settings
        from ritesmith.core.audit import AuditLogger
        from ritesmith.core.execution import ExecutionService
        from ritesmith.registry.models import Plan as PlanORM
        from ritesmith.schemas.execution import CreateExecutionRequest
        from ritesmith.storage.postgres import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(PlanORM).where(PlanORM.plan_id == plan_id))
            plan_orm = result.scalar_one_or_none()

            # Pass continuation state (from cron chain spawn) as Trama execution payload
            continuation_input = (plan_orm.context or {}).get("continuation") if plan_orm else None

            svc = ExecutionService(db=db, settings=get_settings(), audit=AuditLogger(db))
            exec_ = await svc.create_execution(
                CreateExecutionRequest(
                    artifact_id=artifact_id,
                    plan_id=plan_id,
                    input={**continuation_input, "continuation": continuation_input}
                    if continuation_input
                    else None,
                )
            )
            log.info(
                "auto_execute artifact=%s plan=%s exec_status=%s continuation=%s",
                artifact_id,
                plan_id,
                exec_.status,
                bool(continuation_input),
            )

            # Transition plan to executing when delegation is waiting on Trama
            if (
                exec_.status in ("waiting", "running")
                and plan_orm
                and plan_orm.status == PlanStatus.approved
            ):
                plan_orm.status = PlanStatus.executing
                await db.commit()
            # Scripts finish here, and a workflow Trama refused never starts —
            # either way nothing else would ever complete the plan or fire its
            # callback, so close it now.
            elif exec_.status in ("succeeded", "failed", "rejected") and plan_orm:
                await _finish_from_execution(db, plan_orm, exec_)
    except Exception as e:
        log.error(
            "auto_execute failed artifact=%s plan=%s: %s", artifact_id, plan_id, e, exc_info=True
        )


async def _finish_from_execution(db, plan_orm, exec_) -> None:
    import json

    from ritesmith.config import get_settings
    from ritesmith.core.audit import AuditLogger
    from ritesmith.core.planning import PlanBuilder

    if exec_.status == "succeeded":
        target = PlanStatus.completed
        summary = json.dumps(exec_.output, ensure_ascii=False)[:500] if exec_.output else None
    else:
        target = PlanStatus.failed
        err = exec_.error or {}
        summary = f"execução {exec_.status}: {err.get('reason') or err.get('message') or err}"[:500]
    builder = PlanBuilder(db=db, llm=None, settings=get_settings(), audit=AuditLogger(db))
    try:
        await builder._finish(plan_orm, target, summary)
    except Exception as e:  # already terminal (e.g. completed by rs_complete) — nothing to do
        log.info("auto_execute finish skipped plan=%s: %s", plan_orm.plan_id, e)


@router.get("", response_model=list[Plan])
async def list_plans(
    status: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    builder: PlanBuilder = Depends(get_plan_builder),
) -> list[Plan]:
    return await builder.list_plans(status=status, limit=limit)


@router.post("", response_model=Plan, status_code=201)
async def create_plan(
    req: CreatePlanRequest,
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    plan = await builder.build_plan(req)
    if req.mode == GenerationMode.execute and plan.status == PlanStatus.approved:
        for artifact in plan.artifacts:
            if artifact.artifact_id:
                t = asyncio.create_task(_auto_execute(artifact.artifact_id, plan.plan_id))
                t.add_done_callback(_log_task_error)
    return plan


@router.get("/{plan_id}", response_model=Plan)
async def get_plan(
    plan_id: str,
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.get_plan(plan_id)


@router.post("/{plan_id}/approve", response_model=Plan)
async def approve_plan(
    plan_id: str,
    req: ApprovePlanRequest = ApprovePlanRequest(),
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.approve_plan(plan_id, req)


@router.post("/{plan_id}/reject", response_model=Plan)
async def reject_plan(
    plan_id: str,
    req: RejectPlanRequest = RejectPlanRequest(),
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.reject_plan(plan_id, req)


@router.post("/{plan_id}/cancel", response_model=Plan)
async def cancel_plan(
    plan_id: str,
    req: CancelPlanRequest = CancelPlanRequest(),
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.cancel_plan(plan_id, req.reason)


@router.post("/{plan_id}/complete", response_model=Plan)
async def complete_plan(
    plan_id: str,
    req: CompletePlanRequest = CompletePlanRequest(),
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.complete_plan(plan_id, req)


@router.post("/{plan_id}/artifacts", response_model=Plan)
async def persist_artifacts(
    plan_id: str,
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.persist_artifacts(plan_id)
