from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.core.exceptions import NotFoundError
from ritesmith.registry.models import Artifact as ArtifactORM
from ritesmith.registry.models import ArtifactVersion as ArtifactVersionORM
from ritesmith.registry.search import fts_search
from ritesmith.registry.service import RegistryService
from ritesmith.schemas.artifact import (
    Artifact,
    ArtifactListResponse,
    ArtifactStatus,
    ArtifactType,
    ArtifactVersion,
    CreateArtifactRequest,
)
from ritesmith.storage.postgres import get_db

router = APIRouter(prefix="/artifacts", tags=["Artifacts"])


def _build_artifact(orm: ArtifactORM, av: ArtifactVersionORM | None = None) -> Artifact:
    return Artifact(
        artifact_id=orm.artifact_id,
        artifact_type=ArtifactType(orm.artifact_type),
        name=orm.name,
        version=orm.current_version,
        status=ArtifactStatus(orm.status),
        content=av.content if av else None,
        content_hash=orm.content_hash,
        description=orm.description,
        generated_by_plan_id=orm.generated_by_plan_id,
        tags=orm.tags,
        risk_level=av.risk_level if av else None,
        metadata=av.metadata_ if av else None,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _build_version(av: ArtifactVersionORM) -> ArtifactVersion:
    return ArtifactVersion(
        artifact_id=av.artifact_id,
        version=av.version,
        content=av.content,
        input_schema=av.input_schema,
        output_schema=av.output_schema,
        risk_level=av.risk_level,
        side_effects=av.side_effects,
        deterministic=av.deterministic,
        requires_approval=av.requires_approval,
        allowed_primitives=av.allowed_primitives,
        created_at=av.created_at,
    )


@router.get("", response_model=ArtifactListResponse)
async def list_artifacts(
    query: str | None = Query(None, description="Full-text search query"),
    artifact_type: str | None = Query(None),
    status: str | None = Query(None),
    tags: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ArtifactListResponse:
    svc = RegistryService(db)
    if query:
        results = await fts_search(
            db,
            query,
            artifact_types=[artifact_type] if artifact_type else None,
            tags=tags,
            status=status,
            limit=limit,
        )
        artifacts = [_build_artifact(r.artifact, r.version) for r in results]
    else:
        orms = await svc.list_artifacts(
            artifact_type=artifact_type,
            status=status,
            tags=tags,
            limit=limit,
            offset=offset,
        )
        artifacts = [_build_artifact(o) for o in orms]

    return ArtifactListResponse(artifacts=artifacts, total=len(artifacts))


@router.post("", response_model=Artifact, status_code=201)
async def create_artifact(
    req: CreateArtifactRequest,
    db: AsyncSession = Depends(get_db),
) -> Artifact:
    svc = RegistryService(db)
    artifact, version = await svc.create_artifact(
        name=req.name,
        artifact_type=req.artifact_type,
        content=req.content,
        description=req.description,
        tags=req.tags,
        input_schema=req.input_schema,
        output_schema=req.output_schema,
        risk_level=req.risk_level,
        metadata=req.metadata,
    )
    await db.commit()
    return _build_artifact(artifact, version)


@router.get("/{artifact_id}", response_model=Artifact)
async def get_artifact(artifact_id: str, db: AsyncSession = Depends(get_db)) -> Artifact:
    svc = RegistryService(db)
    pair = await svc.get_artifact_with_version(artifact_id)
    if not pair:
        raise NotFoundError(f"Artifact '{artifact_id}' not found")
    artifact, version = pair
    return _build_artifact(artifact, version)


@router.get("/{artifact_id}/versions", response_model=list[ArtifactVersion])
async def list_versions(
    artifact_id: str, db: AsyncSession = Depends(get_db)
) -> list[ArtifactVersion]:
    svc = RegistryService(db)
    artifact = await svc.get_artifact(artifact_id)
    if not artifact:
        raise NotFoundError(f"Artifact '{artifact_id}' not found")
    versions = await svc.list_artifact_versions(artifact_id)
    return [_build_version(v) for v in versions]


@router.get("/{artifact_id}/versions/{version}", response_model=Artifact)
async def get_artifact_version(
    artifact_id: str,
    version: int,
    db: AsyncSession = Depends(get_db),
) -> Artifact:
    svc = RegistryService(db)
    pair = await svc.get_artifact_with_version(artifact_id, version)
    if not pair:
        raise NotFoundError(f"Artifact '{artifact_id}' version {version} not found")
    artifact, av = pair
    return _build_artifact(artifact, av)
