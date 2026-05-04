from fastapi import APIRouter, Depends

from ritesmith.api.deps import get_audit, get_llm_provider, get_plan_builder
from ritesmith.core.planning import PlanBuilder
from ritesmith.schemas.plan import ApprovePlanRequest, CreatePlanRequest, Plan, RejectPlanRequest

router = APIRouter(prefix="/plans", tags=["plans"])


@router.post("", response_model=Plan, status_code=201)
async def create_plan(
    req: CreatePlanRequest,
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.build_plan(req)


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


@router.post("/{plan_id}/artifacts", response_model=Plan)
async def persist_artifacts(
    plan_id: str,
    builder: PlanBuilder = Depends(get_plan_builder),
) -> Plan:
    return await builder.persist_artifacts(plan_id)
