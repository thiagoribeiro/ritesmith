from fastapi import APIRouter, Depends

from ritesmith.api.deps import get_execution_service
from ritesmith.core.execution import ExecutionService
from ritesmith.schemas.execution import CreateExecutionRequest, Execution

router = APIRouter(prefix="/executions", tags=["executions"])


@router.post("", response_model=Execution, status_code=201)
async def create_execution(
    req: CreateExecutionRequest,
    service: ExecutionService = Depends(get_execution_service),
) -> Execution:
    return await service.create_execution(req)


@router.get("/{execution_id}", response_model=Execution)
async def get_execution(
    execution_id: str,
    service: ExecutionService = Depends(get_execution_service),
) -> Execution:
    return await service.get_execution(execution_id)
