from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CapabilityKind(StrEnum):
    tool = "tool"
    provider = "provider"
    runtime = "runtime"
    storage = "storage"
    notification = "notification"
    workflow_engine = "workflow_engine"
    script_function = "script_function"
    catalog = "catalog"


class CapabilityStatus(StrEnum):
    available = "available"
    unavailable = "unavailable"
    disabled = "disabled"
    experimental = "experimental"
    deprecated = "deprecated"


class Capability(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    capability_id: str
    name: str
    kind: CapabilityKind
    status: CapabilityStatus
    description: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    risk_level: str = "low"
    tags: list[str] | None = None
    provider_id: str | None = None
    metadata: dict | None = None
    created_at: datetime
    updated_at: datetime


class CapabilityListResponse(BaseModel):
    capabilities: list[Capability]
    total: int
