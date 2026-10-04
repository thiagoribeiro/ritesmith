"""Three-stage reuse selection, shared by generation services and the planner.

Cheap → expensive:
  1. Recall (FTS) over name + usage_description + description + tags → top-K.
  2. Deterministic contract-compatibility filter (``core.compat``) — the safety
     gate: only candidates that can safely run in place of the request survive.
  3. Semantic judge (LLM, optional) picks the candidate that actually fulfils the
     intent among the survivors. Permissions/risk were already enforced in (2);
     the judge decides relevance only.

A miss at any stage (no recall, nothing compatible, judge says "none") means
generate a new artifact.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings, get_settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.compat import is_compatible
from ritesmith.llm.base import LLMProvider
from ritesmith.observability.metrics import artifact_reuse_total
from ritesmith.registry.search import SearchResult, fts_search
from ritesmith.schemas.generation import GeneratedArtifactResponse

log = logging.getLogger(__name__)

_REUSE_SCORE_THRESHOLD = 0.05


async def check_reuse(
    db: AsyncSession,
    intent: str,
    artifact_types: list[str],
    reuse_policy: str,
    audit: AuditLogger | None = None,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    runtime_profile: str | None = None,
    allowed_risk: str | None = None,
    llm: LLMProvider | None = None,
    settings: Settings | None = None,
) -> GeneratedArtifactResponse | None:
    """Return a reuse response if a compatible, relevant artifact exists, else None."""
    if reuse_policy == "force_new":
        return None
    settings = settings or get_settings()
    requested_type = artifact_types[0] if artifact_types else "unknown"

    # 1. Recall
    results = await fts_search(
        db, intent, artifact_types=artifact_types, limit=settings.reuse_recall_limit
    )
    results = [r for r in results if r.score >= _REUSE_SCORE_THRESHOLD]
    if not results:
        return None

    # 2. Deterministic contract-compatibility filter (safety gate)
    compatible = [
        r
        for r in results
        if is_compatible(
            r,
            request_input_schema=input_schema,
            request_output_schema=output_schema,
            requested_profile=runtime_profile,
            allowed_risk=allowed_risk,
            requested_type=requested_type,
        )
    ]
    if not compatible:
        log.info("reuse: %d recalled, 0 contract-compatible → generating", len(results))
        return None

    # 3. Semantic judge (relevance only; permissions already guaranteed)
    chosen = await _judge(intent, compatible, settings, llm)
    if chosen is None:
        log.info("reuse: %d compatible, judge selected none → generating", len(compatible))
        return None

    artifact_reuse_total.labels(artifact_type=requested_type).inc()
    if audit:
        await audit.log_event(
            "generation.reused",
            "artifact",
            chosen.artifact.artifact_id,
            payload={"goal": intent, "score": chosen.score},
        )
    return GeneratedArtifactResponse(
        artifact=_build_artifact(chosen.artifact, chosen.version),
        reused=True,
        source_artifact_id=chosen.artifact.artifact_id,
    )


async def _judge(
    intent: str,
    compatible: list[SearchResult],
    settings: Settings,
    llm: LLMProvider | None,
) -> SearchResult | None:
    """Pick the relevant survivor. Deterministic (best FTS hit) when the judge is off."""
    if not settings.reuse_llm_judge or llm is None:
        return compatible[0]
    candidates = [
        {
            "name": r.artifact.name,
            "usage_description": r.artifact.usage_description,
            "description": r.artifact.description,
            "input_schema": r.version.input_schema if r.version else None,
            "output_schema": r.version.output_schema if r.version else None,
        }
        for r in compatible
    ]
    try:
        idx, _stats = await llm.judge_reuse(intent, candidates)
    except NotImplementedError:
        return compatible[0]
    if idx is None or not (0 <= idx < len(compatible)):
        return None
    return compatible[idx]
