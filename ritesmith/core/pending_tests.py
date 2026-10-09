"""Drain already-paid independent tests within the shared request deadline."""

import asyncio
import logging

from ritesmith.core.generation_budget import current_budget


async def finish_pending_tests(task):
    if task is None:
        return
    budget = current_budget.get()
    if not task.done():
        remaining = budget.remaining if budget else 30
        try:
            async with asyncio.timeout(remaining):
                await asyncio.shield(task)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception:
            # The caller records test failures; cleanup never replaces the original error.
            logging.getLogger(__name__).debug(
                "Independent tests failed during cleanup", exc_info=True
            )
    await asyncio.gather(task, return_exceptions=True)
