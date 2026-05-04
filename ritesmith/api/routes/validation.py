from fastapi import APIRouter, Depends

from ritesmith.config import Settings, get_settings
from ritesmith.core.validation import ValidationPipeline
from ritesmith.schemas.artifact import ValidationResult
from ritesmith.schemas.generation import ValidateArtifactRequest

router = APIRouter(tags=["Validation"])


@router.post("/validate", response_model=ValidationResult)
async def validate_artifact(
    req: ValidateArtifactRequest,
    settings: Settings = Depends(get_settings),
) -> ValidationResult:
    pipeline = ValidationPipeline(settings)
    profile = (req.constraints or {}).get("runtime_profile", "transform_only")
    return await pipeline.run(
        content=req.content,
        artifact_type=req.artifact_type,
        constraints=req.constraints,
        test_cases=req.test_cases,
        profile=profile,
    )
