from pydantic import BaseModel, Field

from ritesmith.schemas.artifact import Artifact, ValidationResult


class ScriptConstraints(BaseModel):
    allow_network: bool = True
    allow_filesystem: bool = False
    max_lines: int = 60
    max_http_calls: int = 2
    allowed_host_functions: list[str] | None = None
    runtime_profile: str = "transform_only"
    reuse_policy: str = "prefer_reuse"


class GenerateScriptRequest(BaseModel):
    intent: str = Field(..., min_length=1)
    language: str = "lua"
    required_capabilities: list[str] | None = None
    constraints: ScriptConstraints | None = None
    context: dict | None = None
    save: bool = False
    input_schema: dict | None = None
    output_schema: dict | None = None


class GenerateWorkflowRequest(BaseModel):
    intent: str = Field(..., min_length=1)
    target_runtime: str = "trama"
    required_capabilities: list[str] | None = None
    constraints: dict | None = None
    context: dict | None = None
    save: bool = False


class GenerateRequest(BaseModel):
    """Unified generation request — RiteSmith decides internally whether to produce Lua or a workflow."""

    intent: str = Field(..., min_length=1)
    save: bool = False
    context: dict | None = None
    constraints: dict | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None


class GeneratedArtifactResponse(BaseModel):
    artifact: Artifact
    validation: ValidationResult | None = None
    reused: bool = False
    source_artifact_id: str | None = None


class ValidateArtifactRequest(BaseModel):
    content: str = Field(..., min_length=1)
    artifact_type: str
    input_schema: dict | None = None
    output_schema: dict | None = None
    test_cases: list[dict] | None = None
    constraints: dict | None = None
