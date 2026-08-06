from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ritesmith.runtime.providers import PROVIDERS

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("")
async def list_providers() -> JSONResponse:
    return JSONResponse([p.manifest() for p in PROVIDERS])


@router.get("/{namespace}")
async def get_provider(namespace: str) -> JSONResponse:
    for p in PROVIDERS:
        if p.namespace == namespace:
            return JSONResponse(p.manifest())
    return JSONResponse(
        status_code=404,
        content={"error": "not_found", "message": f"Provider '{namespace}' not found"},
    )
