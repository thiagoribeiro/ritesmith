"""Thin async HTTP client for the RiteSmith API."""

from __future__ import annotations

import os

import httpx

_BASE = os.environ.get("RITESMITH_BASE_URL", "http://localhost:8081")
_TIMEOUT = 60.0


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(base_url=_BASE, timeout=_TIMEOUT) as c:
        r = await c.post(path, json=body)
        r.raise_for_status()
        return r.json()


async def _get(path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(base_url=_BASE, timeout=_TIMEOUT) as c:
        r = await c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


async def plan(intent: str, constraints: dict | None = None) -> dict:
    return await _post("/plans", {"intent": intent, "constraints": constraints or {}})


async def generate(intent: str, **kwargs) -> dict:
    return await _post("/generate", {"intent": intent, **kwargs})


async def search_artifacts(query: str, artifact_type: str | None = None, limit: int = 10) -> dict:
    params: dict = {"query": query, "limit": limit}
    if artifact_type:
        params["artifact_type"] = artifact_type
    return await _get("/artifacts", params)


async def execute(artifact_id: str, input_data: dict, idempotency_key: str | None = None) -> dict:
    body: dict = {"artifact_id": artifact_id, "input": input_data}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    return await _post("/executions", body)


async def get_execution(execution_id: str) -> dict:
    return await _get(f"/executions/{execution_id}")


async def list_capabilities(kind: str | None = None, limit: int = 50) -> dict:
    params: dict = {"limit": limit}
    if kind:
        params["kind"] = kind
    return await _get("/capabilities", params)
