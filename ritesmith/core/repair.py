"""Generation recovery: one owner for transport retry, repair and fallback."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from ritesmith.core.exceptions import LLMError, LLMRateLimitError
from ritesmith.core.generation_budget import current_budget
from ritesmith.observability.generation import current_trace
from ritesmith.observability.metrics import generation_attempts_total

log = logging.getLogger(__name__)


@dataclass
class RecoveryAllowance:
    remaining: int
    expected_seconds: float = 0

    def claim(self):
        budget = current_budget.get()
        if self.remaining <= 0 or (
            budget
            and budget.remaining < max(budget.minimum_recovery_seconds, self.expected_seconds)
        ):
            return False
        self.remaining -= 1
        return True


current_recovery: ContextVar[RecoveryAllowance | None] = ContextVar("recovery", default=None)


def claim_recovery(operation="proposal", limit=1):
    allowance = current_recovery.get()
    if allowance is not None:
        return allowance.claim()
    budget = current_budget.get()
    return budget.recover(operation, limit) if budget else True


async def run_repair_loop(
    artifact_type: str,
    max_attempts: int,
    attempt_fn: Callable[[int], Awaitable[bool]],
    *,
    settings=None,
    state_fn=None,
    initial_recoveries=0,
    initial_duration=0,
) -> int:
    bounded = settings is not None and settings.generation_bounded_recovery
    allowance = RecoveryAllowance(
        max(0, settings.generation_recovery_attempts - initial_recoveries)
        if bounded
        else max_attempts - 1,
        expected_seconds=initial_duration,
    )
    token = current_recovery.set(allowance)
    success = False
    performed = 0
    previous = None
    consecutive_parse_failures = 0
    try:
        for attempt in range(1, max_attempts + 1):
            if attempt > 1 and not allowance.claim():
                break
            performed = attempt
            try:
                success = await attempt_fn(attempt)
                consecutive_parse_failures = 0
                if success:
                    break
                if state_fn is not None:
                    candidate, diagnostics = state_fn()
                    trace = current_trace.get()
                    if trace is not None:
                        trace.diagnostics.extend(d.model_dump() for d in diagnostics)
                    state = (candidate, tuple(d.model_dump_json() for d in diagnostics))
                    if any(not d.repairable for d in diagnostics) or state == previous:
                        break
                    previous = state
            except LLMRateLimitError:
                if not bounded and attempt < max_attempts:
                    await asyncio.sleep(2**attempt)
            except LLMError as exc:
                if exc.details.get("retryable") is False:
                    break
                category = exc.details.get("category", "response")
                log.warning("generation failure category=%s", category)
                if not bounded and category in ("response", "graph", "truncated"):
                    consecutive_parse_failures += 1
                    if consecutive_parse_failures >= 3:
                        break
        outcome = "success" if success else "exhausted"
        generation_attempts_total.labels(artifact_type=artifact_type, outcome=outcome).inc()
        return performed
    finally:
        current_recovery.reset(token)
