"""HTTP adapter for Trama workflow engine."""

import httpx

from ritesmith.workflows.base import WorkflowEngineAdapter
from ritesmith.workflows.compact import to_trama_definition

# Kept under its old name: the adapter and existing callers normalize through it.
_normalize_definition = to_trama_definition


class HttpWorkflowEngineAdapter(WorkflowEngineAdapter):
    def __init__(self, base_url: str, timeout: float = 10.0, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    async def submit_workflow(
        self,
        definition: dict,
        input_data: dict | None = None,
    ) -> str:
        normalized = _normalize_definition(definition)
        payload = {"definition": normalized, "payload": input_data or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/workflows/run",
                json=payload,
                headers=self.headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["id"]

    async def get_steps(self, external_execution_id: str) -> list[dict]:
        """Every step Trama ran for this execution, in order (stepName = node id)."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/workflows/{external_execution_id}/steps",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_status(self, external_execution_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/workflows/{external_execution_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()
