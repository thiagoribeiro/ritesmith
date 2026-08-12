import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.config import get_settings
from ritesmith.core.exceptions import InvalidTransitionError, NotFoundError
from ritesmith.core.ids import generate_id
from ritesmith.registry.models import Artifact, ArtifactVersion, Capability

_TTL_TYPES = {"lua_script", "trama_workflow"}

# Legal status transitions
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"proposed", "validated", "rejected", "archived", "deprecated"},
    "proposed": {"validated", "approved", "rejected", "archived"},
    "validated": {"approved", "active", "deprecated", "archived"},
    "approved": {"active", "deprecated", "archived"},
    "active": {"deprecated", "archived"},
    "deprecated": {"archived"},
    "rejected": {"archived"},
    "archived": set(),
}


class RegistryService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    async def create_artifact(
        self,
        *,
        name: str,
        artifact_type: str,
        content: str,
        description: str | None = None,
        tags: list[str] | None = None,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        risk_level: str | None = None,
        requires_approval: bool = False,
        generated_by_plan_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        artifact_id = generate_id("art")
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        now = datetime.now(UTC)

        expires_at: datetime | None = None
        ttl = get_settings().artifact_ttl_days
        if ttl > 0 and artifact_type in _TTL_TYPES:
            expires_at = now + timedelta(days=ttl)

        artifact = Artifact(
            artifact_id=artifact_id,
            name=name,
            artifact_type=artifact_type,
            current_version=1,
            status="draft",
            description=description,
            tags=tags,
            content_hash=content_hash,
            generated_by_plan_id=generated_by_plan_id,
            expires_at=expires_at,
        )
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version=1,
            content=content,
            input_schema=input_schema,
            output_schema=output_schema,
            risk_level=risk_level,
            requires_approval=requires_approval,
            metadata_=metadata,
        )
        self.db.add(artifact)
        self.db.add(version)
        await self.db.flush()
        await self.db.refresh(artifact)
        return artifact, version

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        stmt = select(Artifact).where(Artifact.artifact_id == artifact_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_artifact_with_version(
        self, artifact_id: str, version: int | None = None
    ) -> tuple[Artifact, ArtifactVersion] | None:
        artifact = await self.get_artifact(artifact_id)
        if not artifact:
            return None
        ver_num = version if version is not None else artifact.current_version
        stmt = select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.version == ver_num,
        )
        result = await self.db.execute(stmt)
        av = result.scalar_one_or_none()
        if not av:
            return None
        return artifact, av

    async def list_artifact_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        stmt = (
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create_new_version(
        self,
        artifact_id: str,
        content: str,
        *,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        risk_level: str | None = None,
        requires_approval: bool = False,
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        artifact = await self.get_artifact(artifact_id)
        if not artifact:
            raise NotFoundError(f"Artifact '{artifact_id}' not found")

        new_ver_num = artifact.current_version + 1
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        version = ArtifactVersion(
            artifact_id=artifact_id,
            version=new_ver_num,
            content=content,
            input_schema=input_schema,
            output_schema=output_schema,
            risk_level=risk_level,
            requires_approval=requires_approval,
            metadata_=metadata,
        )
        self.db.add(version)

        artifact.current_version = new_ver_num
        artifact.content_hash = content_hash
        artifact.updated_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(artifact)
        return artifact, version

    async def update_artifact_status(self, artifact_id: str, new_status: str) -> Artifact:
        artifact = await self.get_artifact(artifact_id)
        if not artifact:
            raise NotFoundError(f"Artifact '{artifact_id}' not found")

        allowed = _ALLOWED_TRANSITIONS.get(artifact.status, set())
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition artifact from '{artifact.status}' to '{new_status}'",
                details={
                    "current": artifact.status,
                    "requested": new_status,
                    "allowed": list(allowed),
                },
            )
        artifact.status = new_status
        artifact.updated_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.refresh(artifact)
        return artifact

    async def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Artifact]:
        stmt = select(Artifact).order_by(Artifact.created_at.desc()).limit(limit).offset(offset)
        if artifact_type:
            stmt = stmt.where(Artifact.artifact_type == artifact_type)
        if status:
            stmt = stmt.where(Artifact.status == status)
        if tags:
            stmt = stmt.where(Artifact.tags.contains(tags))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    async def get_capability(self, capability_id: str) -> Capability | None:
        stmt = select(Capability).where(Capability.capability_id == capability_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_capabilities(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Capability]:
        stmt = select(Capability).order_by(Capability.name).limit(limit)
        if kind:
            stmt = stmt.where(Capability.kind == kind)
        if status:
            stmt = stmt.where(Capability.status == status)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
