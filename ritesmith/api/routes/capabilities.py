from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.core.exceptions import NotFoundError
from ritesmith.registry.models import Capability as CapabilityORM
from ritesmith.registry.service import RegistryService
from ritesmith.schemas.capability import Capability, CapabilityKind, CapabilityListResponse, CapabilityStatus
from ritesmith.storage.postgres import get_db

router = APIRouter(prefix="/capabilities", tags=["Capabilities"])


def _build_capability(orm: CapabilityORM) -> Capability:
    return Capability(
        capability_id=orm.capability_id,
        name=orm.name,
        kind=CapabilityKind(orm.kind),
        status=CapabilityStatus(orm.status),
        description=orm.description,
        input_schema=orm.input_schema,
        output_schema=orm.output_schema,
        risk_level=orm.risk_level,
        tags=orm.tags,
        provider_id=orm.provider_id,
        metadata=orm.metadata_,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


@router.get("", response_model=CapabilityListResponse)
async def list_capabilities(
    kind: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> CapabilityListResponse:
    svc = RegistryService(db)
    caps = await svc.list_capabilities(kind=kind, status=status, limit=limit)
    return CapabilityListResponse(capabilities=[_build_capability(c) for c in caps], total=len(caps))


@router.get("/{capability_id}", response_model=Capability)
async def get_capability(capability_id: str, db: AsyncSession = Depends(get_db)) -> Capability:
    svc = RegistryService(db)
    cap = await svc.get_capability(capability_id)
    if not cap:
        raise NotFoundError(f"Capability '{capability_id}' not found")
    return _build_capability(cap)
