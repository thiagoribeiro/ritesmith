"""Workflow engine adapter interface + canonical workflow definition types."""
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class WorkflowStep(BaseModel):
    step_id: str
    step_type: str  # capability | sleep | condition | notification | callback
    capability_id: str | None = None
    depends_on: list[str] = []
    inputs: dict[str, Any] = {}
    config: dict[str, Any] = {}


class WorkflowDefinition(BaseModel):
    workflow_id: str | None = None
    name: str
    description: str | None = None
    steps: list[WorkflowStep] = []
    metadata: dict[str, Any] = {}


class WorkflowEngineAdapter(ABC):
    @abstractmethod
    async def submit_workflow(
        self,
        definition: dict,
        input_data: dict | None = None,
    ) -> str:
        """Submit workflow to engine and return external execution_id."""
        ...

    @abstractmethod
    async def get_status(self, external_execution_id: str) -> dict:
        """Return status dict from the engine (at minimum: status, output, error)."""
        ...
