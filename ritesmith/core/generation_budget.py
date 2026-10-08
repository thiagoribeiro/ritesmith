"""One wall-clock deadline shared by nested generation, tests and persistence."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import monotonic

from ritesmith.core.exceptions import GenerationFailedError


@dataclass
class GenerationBudget:
    deadline: float
    minimum_recovery_seconds: float = 3
    recoveries: dict[str, int] = field(default_factory=dict)
    prepared: dict = field(default_factory=dict)
    observed_seconds: dict[str, float] = field(default_factory=dict)

    @property
    def remaining(self):
        return max(0.0, self.deadline - monotonic())

    def recover(self, operation: str, limit: int = 1) -> bool:
        if self.remaining < max(
            self.minimum_recovery_seconds, self.observed_seconds.get(operation, 0)
        ):
            return False
        used = self.recoveries.get(operation, 0)
        if used >= limit:
            return False
        self.recoveries[operation] = used + 1
        return True


current_budget: ContextVar[GenerationBudget | None] = ContextVar("generation_budget", default=None)


def bounded_generation(fn):
    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        settings = getattr(self, "settings", None) or self._settings
        if not settings.generation_bounded_recovery or current_budget.get() is not None:
            return await fn(self, *args, **kwargs)
        budget = GenerationBudget(
            monotonic() + settings.generation_deadline_seconds,
            settings.generation_recovery_min_seconds,
        )
        token = current_budget.set(budget)
        guard = asyncio.timeout(settings.generation_deadline_seconds)
        try:
            async with guard:
                return await fn(self, *args, **kwargs)
        except TimeoutError as exc:
            if not guard.expired():
                raise
            from ritesmith.observability.generation import current_trace, outcome

            trace = current_trace.get()
            if trace is not None and trace.artifact_types:
                trace.accepted = False
            else:
                outcome("unknown", False)
            raise GenerationFailedError(
                "Generation deadline exceeded",
                details={"category": "deadline", "retryable": False},
            ) from exc
        finally:
            current_budget.reset(token)

    return wrapped
