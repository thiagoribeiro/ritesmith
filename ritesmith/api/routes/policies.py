from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ritesmith.config import Settings, get_settings
from ritesmith.core.policy import PolicyEngine
from ritesmith.schemas.policy import PolicyDecision, PolicyEvaluationRequest

router = APIRouter(prefix="/policies", tags=["policies"])


@router.post("/evaluate", response_model=PolicyDecision)
async def evaluate_policy(
    req: PolicyEvaluationRequest,
    settings: Settings = Depends(get_settings),
) -> PolicyDecision:
    engine = PolicyEngine(settings)
    return engine.evaluate(req)
