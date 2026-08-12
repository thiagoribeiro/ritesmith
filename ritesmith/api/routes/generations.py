from fastapi import APIRouter, Depends, Request

from ritesmith.api.deps import (
    get_generation_dispatcher,
    get_generation_service,
    get_workflow_generation_service,
)
from ritesmith.api.limiter import limiter
from ritesmith.core.generation import GenerationService
from ritesmith.core.generation_dispatcher import GenerationDispatcher
from ritesmith.core.workflow_generation import WorkflowGenerationService
from ritesmith.schemas.generation import (
    GeneratedArtifactResponse,
    GenerateRequest,
    GenerateScriptRequest,
    GenerateWorkflowRequest,
)

router = APIRouter(prefix="/generate", tags=["generation"])


@router.post("", response_model=GeneratedArtifactResponse)
@limiter.limit("10/minute")
async def generate(
    request: Request,
    req: GenerateRequest,
    dispatcher: GenerationDispatcher = Depends(get_generation_dispatcher),
) -> GeneratedArtifactResponse:
    """Generate a capability or workflow from a natural-language intent.

    RiteSmith automatically decides whether to produce a Lua script or a Trama
    workflow based on intent analysis — the caller does not need to specify a type.
    """
    return await dispatcher.dispatch(req)


@router.post("/lua", response_model=GeneratedArtifactResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def generate_lua(
    request: Request,
    req: GenerateScriptRequest,
    service: GenerationService = Depends(get_generation_service),
) -> GeneratedArtifactResponse:
    return await service.generate_lua(req)


@router.post("/trama-workflow", response_model=GeneratedArtifactResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def generate_workflow(
    request: Request,
    req: GenerateWorkflowRequest,
    service: WorkflowGenerationService = Depends(get_workflow_generation_service),
) -> GeneratedArtifactResponse:
    return await service.generate_workflow(req)
