from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ritesmith.schemas.artifact import Artifact, ValidationResult


class PlanStatus(StrEnum):
    draft = "draft"
    proposed = "proposed"
    validating = "validating"
    blocked = "blocked"
    approved = "approved"
    rejected = "rejected"
    persisted = "persisted"
    executing = "executing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    superseded = "superseded"  # replaced by a re-plan


class GenerationMode(StrEnum):
    propose = "propose"
    generate = "generate"
    validate_only = "validate_only"
    persist = "persist"
    execute = "execute"


class ReusePolicy(StrEnum):
    prefer_reuse = "prefer_reuse"
    force_new = "force_new"
    reuse_or_generate = "reuse_or_generate"


class PlanConstraints(BaseModel):
    allow_shell: bool = False
    allow_network: bool = True
    allow_filesystem: bool = False
    allow_workflow_creation: bool = True
    require_approval_for_execution: bool = False
    max_runtime_seconds: int | None = None
    polling_interval_seconds: int | None = None
    allowed_capabilities: list[str] | None = None
    denied_capabilities: list[str] | None = None

    model_config = ConfigDict(extra="allow")


class PlanStep(BaseModel):
    step_id: str
    title: str
    description: str | None = None
    status: str  # pending | running | completed | failed | skipped
    artifact_id: str | None = None
    capability_id: str | None = None
    details: dict | None = None


class PolicyDecision(BaseModel):
    decision: str  # allow | deny | require_approval
    reason: str | None = None
    required_approvals: list[str] | None = None
    blocked_capabilities: list[str] | None = None


class Plan(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: str
    status: PlanStatus
    intent: str
    summary: str | None = None
    steps: list[PlanStep] = []
    artifacts: list[Artifact] = []
    validations: list[ValidationResult] = []
    policy_decision: PolicyDecision | None = None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    metadata: dict | None = None


class CreatePlanRequest(BaseModel):
    intent: str = Field(..., min_length=1)
    requested_artifact_types: list[str] | None = None
    mode: GenerationMode = GenerationMode.propose
    reuse_policy: ReusePolicy = ReusePolicy.prefer_reuse
    constraints: PlanConstraints | None = None
    context: dict | None = None
    replan_budget: int = Field(default=2, ge=0, le=5)


class ApprovePlanRequest(BaseModel):
    comment: str | None = None


class RejectPlanRequest(BaseModel):
    reason: str | None = None


class PersistArtifactsRequest(BaseModel):
    status: str = "validated"
