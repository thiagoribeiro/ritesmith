from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ExecutionStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    rejected = "rejected"


class Execution(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    execution_id: str
    artifact_id: str
    artifact_version: int | None = None
    plan_id: str | None = None
    runtime: str | None = None
    status: ExecutionStatus
    input: dict | None = None
    output: dict | None = None
    error: dict | None = None
    idempotency_key: str | None = None
    delegated_execution_id: str | None = None
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class CreateExecutionRequest(BaseModel):
    artifact_id: str
    version: int | None = None
    input: dict | None = None
    runtime: str | None = None
    dry_run: bool = False
    approval_token: str | None = None
    idempotency_key: str | None = None
    plan_id: str | None = None


class TramaExecuteRequest(BaseModel):
    capability_name: str | None = None
    artifact_id: str | None = None
    input: dict | None = None


class TramaExecuteResponse(BaseModel):
    output: dict
