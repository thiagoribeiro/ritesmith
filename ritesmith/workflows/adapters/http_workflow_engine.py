"""HTTP adapter for Trama workflow engine."""
import httpx

from ritesmith.workflows.base import WorkflowEngineAdapter


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
        payload = {"definition": definition, "input": input_data or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/workflows/execute",
                json=payload,
                headers=self.headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["execution_id"]

    async def get_status(self, external_execution_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = client.get(
                f"{self.base_url}/workflows/executions/{external_execution_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()
