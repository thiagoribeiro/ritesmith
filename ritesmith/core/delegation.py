"""DelegationService — delegates workflow execution to an external engine (Trama)."""

from ritesmith.core.exceptions import RiteSmithError
from ritesmith.workflows.base import WorkflowEngineAdapter


class DelegationService:
    def __init__(self, adapter: WorkflowEngineAdapter):
        self.adapter = adapter

    async def delegate(self, definition: dict, input_data: dict | None = None) -> str:
        """Submit workflow and return the external execution_id."""
        try:
            return await self.adapter.submit_workflow(definition, input_data)
        except Exception as e:
            raise RiteSmithError(f"Workflow delegation failed: {e}") from e

    async def get_status(self, external_execution_id: str) -> dict:
        try:
            return await self.adapter.get_status(external_execution_id)
        except Exception as e:
            raise RiteSmithError(f"Failed to get workflow status: {e}") from e
