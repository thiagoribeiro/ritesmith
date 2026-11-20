from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    time: str
    # "lunardyson <version>" when luau scripts run on LunarDyson, "lupa" when the
    # process fell back to the Lua sandbox (lunardyson missing).
    script_runtime: str


def _script_runtime() -> str:
    try:
        import lunardyson
    except Exception:
        return "lupa"
    return f"lunardyson {lunardyson.version()}"


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version="0.2.1",
        time=datetime.now(UTC).isoformat(),
        script_runtime=_script_runtime(),
    )
