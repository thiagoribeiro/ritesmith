"""Direct web search endpoint — bypasses planning/Trama overhead.

Returns Brave Search results synchronously. Intended for agent_core web_search tool.
"""

import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ritesmith.runtime.providers.web import _web_search

router = APIRouter(prefix="/search", tags=["search"])


class SearchResponse(BaseModel):
    query: str
    results: list[dict]


@router.get("", response_model=SearchResponse)
async def web_search(
    q: str = Query(..., description="Search query"),
    limit: int = Query(5, ge=1, le=20),
) -> SearchResponse:
    results = await asyncio.to_thread(_web_search, q, limit)
    return SearchResponse(query=q, results=results)
