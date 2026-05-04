"""Reuse check shared by generation services."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.core.audit import AuditLogger
from ritesmith.observability.metrics import artifact_reuse_total
from ritesmith.registry.search import fts_search
from ritesmith.schemas.generation import GeneratedArtifactResponse

_REUSE_SCORE_THRESHOLD = 0.05


async def check_reuse(
    db: AsyncSession,
    intent: str,
    artifact_types: list[str],
    reuse_policy: str,
    audit: AuditLogger | None = None,
) -> GeneratedArtifactResponse | None:
    """Return a reuse response if a sufficiently similar artifact exists, otherwise None."""
    if reuse_policy == "force_new":
        return None

    results = await fts_search(db, intent, artifact_types=artifact_types, limit=5)
    if not results or results[0].score < _REUSE_SCORE_THRESHOLD:
        return None

    best = results[0]
    artifact_type = artifact_types[0] if artifact_types else "unknown"
    artifact_reuse_total.labels(artifact_type=artifact_type).inc()
    artifact_schema = _build_artifact(best.artifact, best.version)

    if audit:
        await audit.log_event(
            "generation.reused", "artifact", best.artifact.artifact_id,
            payload={"goal": intent, "score": best.score},
        )

    return GeneratedArtifactResponse(
        artifact=artifact_schema,
        reused=True,
        source_artifact_id=best.artifact.artifact_id,
    )
