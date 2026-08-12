import asyncio
import logging

from fastapi import APIRouter, Depends

from ritesmith.api.deps import get_plan_builder
from ritesmith.core.planning import PlanBuilder
from ritesmith.schemas.plan import (
    ApprovePlanRequest,
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
                    input=continuation_input,
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
    except Exception as e:
        log.error(
            "auto_execute failed artifact=%s plan=%s: %s", artifact_id, plan_id, e, exc_info=True
        )


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
