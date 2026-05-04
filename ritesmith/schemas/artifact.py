from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ArtifactType(StrEnum):
    lua_script = "lua_script"
    shell_script = "shell_script"
    trama_workflow = "trama_workflow"
    workflow_template = "workflow_template"
    prompt_template = "prompt_template"
    capability_adapter = "capability_adapter"
    plan = "plan"


class ArtifactStatus(StrEnum):
    draft = "draft"
    proposed = "proposed"
    validated = "validated"
    approved = "approved"
    active = "active"
    deprecated = "deprecated"
    rejected = "rejected"
    archived = "archived"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ValidationCheck(BaseModel):
    name: str
    status: str  # passed | warning | failed | skipped
    message: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    risk_level: RiskLevel | None = None
    checks: list[ValidationCheck] = []
    warnings: list[str] = []
    errors: list[str] = []


class ArtifactVersion(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: str
    version: int
    content: str
    input_schema: dict | None = None
    output_schema: dict | None = None
    risk_level: RiskLevel | None = None
    side_effects: str | None = None
    deterministic: bool = True
    requires_approval: bool = False
    allowed_primitives: list[str] | None = None
    created_at: datetime


class Artifact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: str
    artifact_type: ArtifactType
    name: str
    version: int
    status: ArtifactStatus
    content: str | None = None
    content_hash: str | None = None
    description: str | None = None
    generated_by_plan_id: str | None = None
    source_artifact_ids: list[str] | None = None
    tags: list[str] | None = None
    risk_level: str | None = None
    validation: ValidationResult | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict | None = None

    @property
    def runtime_profile(self) -> str:
        return (self.metadata or {}).get("runtime_profile", "transform_only")


class CreateArtifactRequest(BaseModel):
    name: str
    artifact_type: ArtifactType
    content: str
    description: str | None = None
    tags: list[str] | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    risk_level: RiskLevel | None = None
    metadata: dict | None = None


class ArtifactListResponse(BaseModel):
    artifacts: list[Artifact]
    total: int
