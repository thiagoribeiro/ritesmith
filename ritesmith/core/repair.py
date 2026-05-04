"""Generic repair loop shared by Lua and workflow generation services."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from ritesmith.core.exceptions import LLMError, LLMRateLimitError
from ritesmith.observability.metrics import generation_attempts_total

log = logging.getLogger(__name__)


async def run_repair_loop(
    artifact_type: str,
    max_attempts: int,
    attempt_fn: Callable[[int], Awaitable[bool]],
) -> None:
    """Run a generate → validate → repair loop, emitting generation_attempts_total.

    attempt_fn(attempt_number) must return True when a valid artifact was produced,
    False to continue. The caller manages all state (content, validation, meta) via
    closure/nonlocal.
    """
    success = False
    consecutive_parse_failures = 0
    for attempt in range(1, max_attempts + 1):
        try:
            success = await attempt_fn(attempt)
            consecutive_parse_failures = 0
            if success:
                break
        except LLMRateLimitError:
            log.warning("rate limited on attempt %d/%d, backing off", attempt, max_attempts)
            if attempt < max_attempts:
                await asyncio.sleep(2 ** attempt)   # 2s, 4s, 8s, 16s
        except LLMError:
            consecutive_parse_failures += 1
            log.warning(
                "LLM parse failure %d/2 on attempt %d", consecutive_parse_failures, attempt
            )
            if consecutive_parse_failures >= 2:
                log.error("unrecoverable: LLM not following JSON schema after 2 parse failures")
                break   # unrecoverable: LLM not following JSON schema

    outcome = "success" if success else "exhausted"
    log.info("repair loop finished artifact_type=%s outcome=%s", artifact_type, outcome)
    generation_attempts_total.labels(artifact_type=artifact_type, outcome=outcome).inc()
