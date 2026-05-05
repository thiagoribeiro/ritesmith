import time
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.observability.metrics import fts_search_duration, fts_search_results
from ritesmith.registry.models import Artifact, ArtifactVersion

_EXCLUDED_STATUSES = {"deprecated", "archived", "rejected"}


@dataclass
class SearchResult:
    artifact: Artifact
    version: ArtifactVersion | None
    score: float


async def fts_search(
    db: AsyncSession,
    query: str,
    *,
    artifact_types: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[SearchResult]:
    """Full-text search over artifacts using PostgreSQL tsvector + ts_rank.

    Uses a single JOIN to fetch the current artifact version, avoiding N+1 queries.
    """
    tsquery = func.plainto_tsquery("english", query)
    rank_col = func.ts_rank(Artifact.search_vector, tsquery).label("rank")

    stmt = (
        select(Artifact, ArtifactVersion, rank_col)
        .outerjoin(
            ArtifactVersion,
            (ArtifactVersion.artifact_id == Artifact.artifact_id)
            & (ArtifactVersion.version == Artifact.current_version),
        )
        .where(Artifact.search_vector.op("@@")(tsquery))
        .order_by(rank_col.desc())
        .limit(limit)
    )

    if artifact_types:
        stmt = stmt.where(Artifact.artifact_type.in_(artifact_types))
    if tags:
        stmt = stmt.where(Artifact.tags.contains(tags))
    if status:
        stmt = stmt.where(Artifact.status == status)
    else:
        stmt = stmt.where(Artifact.status.not_in(_EXCLUDED_STATUSES))

    # Exclude expired artifacts
    stmt = stmt.where(
        or_(Artifact.expires_at.is_(None), Artifact.expires_at > func.now())
    )

    artifact_type_label = (artifact_types[0] if artifact_types and len(artifact_types) == 1 else "mixed")
    t0 = time.perf_counter()
    rows = await db.execute(stmt)
    elapsed = time.perf_counter() - t0

    fts_search_duration.labels(artifact_type=artifact_type_label).observe(elapsed)

    results = [
        SearchResult(artifact=artifact, version=version, score=float(score))
        for artifact, version, score in rows
    ]
    fts_search_results.observe(len(results))
    return results
