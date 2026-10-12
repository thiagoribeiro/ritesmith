"""Trama → RiteSmith integration endpoints.

POST /trama/execute — called by Trama workflow task nodes to invoke RiteSmith
capabilities (tool provider functions or registered Lua artifacts) without
calling external APIs directly.

Auth: Bearer token from RITESMITH_TRAMA_TOKEN config variable. The token is
embedded in generated workflow definitions at generation time.
"""

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException

from ritesmith.api.deps import get_execution_service
from ritesmith.config import Settings, get_settings
from ritesmith.core.execution import ExecutionService
from ritesmith.runtime.host_functions import get_capability
from ritesmith.runtime.providers.base import invoke_capability
from ritesmith.schemas.execution import (
    CreateExecutionRequest,
    TramaExecuteRequest,
    TramaExecuteResponse,
)
from ritesmith.workflows.semantic_validation import unknown_inputs

router = APIRouter(prefix="/trama", tags=["trama"])


@router.post("/execute", response_model=TramaExecuteResponse)
async def trama_execute(
    req: TramaExecuteRequest,
    authorization: str = Header(...),
    x_rs_plan: str | None = Header(None),
    service: ExecutionService = Depends(get_execution_service),
    settings: Settings = Depends(get_settings),
) -> TramaExecuteResponse:
    if not settings.trama_token:
        raise HTTPException(
            status_code=503, detail="Trama integration not configured — set RITESMITH_TRAMA_TOKEN"
        )
    if authorization != f"Bearer {settings.trama_token}":
        raise HTTPException(status_code=401, detail="Invalid token")

    # Kill switch: workflows started for a plan carry X-RS-Plan on every call
    # (injected at execution time). Once the plan is cancelled, refuse — the
    # task fails and the workflow stops instead of running out its iterations.
    if x_rs_plan:
        from sqlalchemy import select

        from ritesmith.registry.models import Plan as PlanORM

        status = (
            await service.db.execute(select(PlanORM.status).where(PlanORM.plan_id == x_rs_plan))
        ).scalar_one_or_none()
        if status == "cancelled":
            raise HTTPException(status_code=409, detail=f"Plan {x_rs_plan} was cancelled")

    if req.capability_name:
        cap = get_capability(req.capability_name)
        if not cap:
            raise HTTPException(
                status_code=404, detail=f"Unknown capability: {req.capability_name}"
            )
        args = req.input or {}
        schema = getattr(cap, "input_schema", None)
        # A contract violation is a 422: Trama does not retry it, unlike a 500.
        unknown = unknown_inputs(schema, args)
        missing = [k for k in (schema or {}).get("required", []) if k not in args]
        if unknown or missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_input",
                    "capability": req.capability_name,
                    "unknown": unknown,
                    "missing": missing,
                    "accepted": sorted((schema or {}).get("properties", {})),
                },
            )
        try:
            result = await asyncio.to_thread(invoke_capability, cap.callable, args)
        except TypeError as e:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_input",
                    "capability": req.capability_name,
                    "message": str(e),
                },
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        if isinstance(result, dict):
            return TramaExecuteResponse(output=result)
        return TramaExecuteResponse(output={"result": result})

    if req.artifact_id:
        exec_ = await service.create_execution(
            CreateExecutionRequest(
                artifact_id=req.artifact_id,
                input=req.input,
            )
        )
        return TramaExecuteResponse(output=exec_.output or {})

    raise HTTPException(status_code=422, detail="Either capability_name or artifact_id is required")
