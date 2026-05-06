"""HTTP adapter for Trama workflow engine."""
import httpx

from ritesmith.workflows.base import WorkflowEngineAdapter

_DEFAULT_FAILURE_HANDLING = {"type": "retry", "maxAttempts": 1, "delayMillis": 0}
_TERMINAL_KEYWORDS = {"end", "done", "finish", "terminal", "stop", "complete"}


def _normalize_definition(definition: dict) -> dict:
    """Ensure V2 definitions are valid before submitting to Trama.

    - Injects default failureHandling when absent.
    - Replaces terminal keyword strings in 'next' fields with null (Trama uses null for terminal).
    """
    if not isinstance(definition, dict):
        return definition

    out = dict(definition)

    if "failureHandling" not in out:
        out["failureHandling"] = _DEFAULT_FAILURE_HANDLING

    if "nodes" in out and isinstance(out["nodes"], list):
        normalized_nodes = []
        for node in out["nodes"]:
            if not isinstance(node, dict):
                normalized_nodes.append(node)
                continue
            n = dict(node)
            # Terminal node references must be null, not a keyword string
            if n.get("next") in _TERMINAL_KEYWORDS:
                n["next"] = None
            # Sleep nodes: convert durationSeconds → durationMillis if needed
            if n.get("kind") == "sleep" and "durationSeconds" in n and "durationMillis" not in n:
                n["durationMillis"] = int(n.pop("durationSeconds") * 1000)
            normalized_nodes.append(n)
        out["nodes"] = normalized_nodes

    return out


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

    async def get_status(self, external_execution_id: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/workflows/{external_execution_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()
