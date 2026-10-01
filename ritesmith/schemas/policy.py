from enum import StrEnum

from pydantic import BaseModel


class PolicyDecisionValue(StrEnum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class PolicyEvaluationRequest(BaseModel):
    operation: str  # generate | persist | execute | discover | delegate
    artifact_type: str | None = None
    capability_ids: list[str] | None = None
    risk_level: str | None = None
    context: dict | None = None
    # Artifacts created by hand (POST /artifacts) get a risk floor from their runtime
    # profile, so a caller cannot declare "low" risk on a device-control script.
    runtime_profile: str | None = None
    manual: bool = False


class PolicyDecision(BaseModel):
    decision: PolicyDecisionValue
    reason: str | None = None
    required_approvals: list[str] | None = None
    blocked_capabilities: list[str] | None = None


class MemorySearchRequest(BaseModel):
    query: str
    artifact_types: list[str] | None = None
    tags: list[str] | None = None
    search_mode: str = "full_text"  # full_text | embeddings | hybrid
    limit: int = 20


class MemorySearchResult(BaseModel):
    artifact_id: str
    score: float
    artifact: "Artifact | None" = None
    reason: str | None = None


class MemorySearchResponse(BaseModel):
    results: list[MemorySearchResult]


# resolve forward ref
from ritesmith.schemas.artifact import Artifact

MemorySearchResult.model_rebuild()
