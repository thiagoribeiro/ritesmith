"""Admin endpoints for CASP migration and provider status."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.registry.search import fts_search
from ritesmith.storage.postgres import get_db

router = APIRouter(prefix="/admin", tags=["admin"])
log = logging.getLogger(__name__)


@router.get("/casp/migration-status")
async def casp_migration_status(db: AsyncSession = Depends(get_db)) -> dict:
    """Returns how many persisted Lua artifacts still use home.* functions.

    When pending == 0, the home.* provider can be removed.
    """
    results = await fts_search(db, "home.", artifact_types=["lua_script"], limit=200)
    home_artifacts = [
        {"artifact_id": r.artifact.artifact_id, "name": r.artifact.name}
        for r in results
        if r.version and r.version.content and "home." in r.version.content
    ]
    return {
        "pending": len(home_artifacts),
        "artifacts": home_artifacts,
        "note": "Migrate these artifacts to casp.* before removing the home.* provider.",
    }


@router.get("/casp/providers")
async def casp_providers_status() -> dict:
    """Returns CASP provider configuration and discovery cache state."""
    from ritesmith.config import get_settings
    from ritesmith.runtime.casp.client import get_types
    settings = get_settings()
    statuses = []
    for p in settings.casp_providers:
        url = p.url.rstrip("/")
        try:
            types = get_types(url)
            statuses.append({
                "url": url,
                "resource_types_configured": p.resource_types or ["all"],
                "priority": p.priority,
                "weight": p.weight,
                "discovered_types": [rt["type"] for rt in types.get("resourceTypes", [])],
                "available": True,
            })
        except Exception as exc:
            statuses.append({"url": url, "available": False, "error": str(exc)})
    return {"providers": statuses}
