from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.registry.search import fts_search
from ritesmith.schemas.policy import MemorySearchRequest, MemorySearchResponse, MemorySearchResult
from ritesmith.storage.postgres import get_db

router = APIRouter(prefix="/memory", tags=["Memory"])


@router.post("/search", response_model=MemorySearchResponse)
async def memory_search(
    req: MemorySearchRequest,
    db: AsyncSession = Depends(get_db),
) -> MemorySearchResponse | JSONResponse:
    if req.search_mode == "embeddings":
        return JSONResponse(
            status_code=501,
            content={
                "error": "not_implemented",
                "message": "Embeddings search not available in V0",
            },
        )

    # full_text e hybrid ambos usam FTS em V0
    results = await fts_search(
        db,
        req.query,
        artifact_types=req.artifact_types,
        tags=req.tags,
        limit=req.limit,
    )

    items = [
        MemorySearchResult(
            artifact_id=r.artifact.artifact_id,
            score=r.score,
            artifact=_build_artifact(r.artifact, r.version),
            reason=f"FTS score {r.score:.4f}",
        )
        for r in results
    ]
    return MemorySearchResponse(results=items)
