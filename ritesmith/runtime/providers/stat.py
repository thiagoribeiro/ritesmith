"""stat.* host functions — stateless math/statistics utilities."""

from __future__ import annotations

from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider


def _min_value(
    current: float,
    previous_min: float | None = None,
    iteration: int = 0,
    initial_min: float | None = None,
) -> dict:
    current = float(current or 0)
    # On first iteration previous_min is null; seed from initial_min (continuation payload)
    effective_prev = previous_min if previous_min is not None else initial_min
    new_min = current if effective_prev is None else min(float(effective_prev), current)
    return {"min_value": new_min, "iteration": int(iteration) + 1, "last_value": current}


def _max_value(
    current: float,
    previous_max: float | None = None,
    iteration: int = 0,
    initial_max: float | None = None,
) -> dict:
    current = float(current or 0)
    effective_prev = previous_max if previous_max is not None else initial_max
    new_max = current if effective_prev is None else max(float(effective_prev), current)
    return {"max_value": new_max, "iteration": int(iteration) + 1, "last_value": current}


def _tick(iteration: int = 0, state: dict | None = None, initial_state: dict | None = None) -> dict:
    """Increment an iteration counter and carry arbitrary state unchanged.

    On first iteration of a cron chain, pass initial_state from payload.continuation.state.
    On subsequent iterations, state carries through from the previous tick output.
    """
    effective_state = state if state is not None else (initial_state or {})
    return {"iteration": int(iteration or 0) + 1, "state": effective_state}


def _length(items) -> dict:
    import json as _json

    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except Exception:
            return {"count": len(items), "type": "string_chars"}
    if isinstance(items, (list, tuple)):
        return {"count": len(items)}
    if isinstance(items, dict):
        return {"count": len(items), "type": "object_keys"}
    return {"count": 0, "error": f"unsupported type: {type(items).__name__}"}


class StatProvider(ToolProvider):
    namespace = "stat"
    profile = "transform_only"
    risk_level = "low"
    side_effects = "none"

    def is_available(self) -> bool:
        return True

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        _min_output = {
            "type": "object",
            "properties": {
                "min_value": {"type": "number"},
                "iteration": {"type": "integer"},
                "last_value": {"type": "number"},
            },
        }
        _max_output = {
            "type": "object",
            "properties": {
                "max_value": {"type": "number"},
                "iteration": {"type": "integer"},
                "last_value": {"type": "number"},
            },
        }
        return {
            "stat.collect": HostFunctionDef(
                "stat.collect",
                "transform_only",
                _collect,
                description="Append a sample to the previous list; initial_samples seeds continuation state.",
                input_schema={
                    "type": "object",
                    "required": ["current"],
                    "properties": {
                        "current": {},
                        "previous_samples": {"type": ["array", "null"]},
                        "initial_samples": {"type": ["array", "null"]},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["samples"],
                    "properties": {"samples": {"type": "array", "items": {}}},
                },
            ),
            "stat.chain_step": HostFunctionDef(
                "stat.chain_step",
                "transform_only",
                _chain_step,
                description="Advance compiled monitor chains; decrement remaining work, preserve state, stop on deadline.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "iteration": {"type": ["integer", "null"]},
                        "remaining_iterations": {"type": ["integer", "null"]},
                        "initial_remaining": {"type": ["integer", "null"]},
                        "deadline_unix": {"type": ["number", "null"]},
                        "duration_seconds": {"type": ["number", "null"]},
                        "initial_deadline": {"type": ["number", "null"]},
                        "state": {"type": ["object", "null"]},
                        "previous_state": {"type": ["object", "null"]},
                        "initial_state": {"type": ["object", "null"]},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": [
                        "iteration",
                        "remaining_iterations",
                        "state",
                        "done",
                        "deadline_unix",
                    ],
                    "properties": {
                        "iteration": {"type": "integer"},
                        "remaining_iterations": {"type": ["integer", "null"]},
                        "deadline_unix": {"type": ["number", "null"]},
                        "state": {"type": "object"},
                        "done": {"type": "boolean"},
                    },
                },
            ),
            "stat.min_value": HostFunctionDef(
                "stat.min_value",
                "transform_only",
                _min_value,
                description=(
                    "Track a running minimum across loop iterations. "
                    "previous_min: self-referential output from previous iteration (null on first). "
                    "initial_min: seed value from payload.continuation.previous_min for cron chains."
                ),
                input_schema={
                    "type": "object",
                    "required": ["current"],
                    "properties": {
                        "current": {
                            "type": "number",
                            "description": "Value observed this iteration",
                        },
                        "previous_min": {
                            "type": "number",
                            "description": "min_value from self-referential previous output",
                        },
                        "initial_min": {
                            "type": "number",
                            "description": "Seed min for cron chain continuation (from payload)",
                        },
                        "iteration": {
                            "type": "integer",
                            "description": "iteration from previous output (defaults 0)",
                        },
                    },
                },
                output_schema=_min_output,
            ),
            "stat.max_value": HostFunctionDef(
                "stat.max_value",
                "transform_only",
                _max_value,
                description=(
                    "Track a running maximum across loop iterations. "
                    "initial_max: seed value from payload.continuation.previous_max for cron chains."
                ),
                input_schema={
                    "type": "object",
                    "required": ["current"],
                    "properties": {
                        "current": {"type": "number"},
                        "previous_max": {"type": "number"},
                        "initial_max": {
                            "type": "number",
                            "description": "Seed max for cron chain continuation",
                        },
                        "iteration": {"type": "integer"},
                    },
                },
                output_schema=_max_output,
            ),
            "stat.tick": HostFunctionDef(
                "stat.tick",
                "transform_only",
                _tick,
                description=(
                    "Increment an iteration counter and carry arbitrary state unchanged. "
                    "Use for cron chains: state passes through each iteration; "
                    "initial_state seeds the first iteration of a continuation chain from payload."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "iteration": {
                            "type": "integer",
                            "description": "iteration from self-referential previous output",
                        },
                        "state": {
                            "type": "object",
                            "description": "state dict from previous tick output (null on first iteration)",
                        },
                        "initial_state": {
                            "type": "object",
                            "description": "Seed state for cron chain continuation (from payload.continuation.state)",
                        },
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "iteration": {"type": "integer"},
                        "state": {
                            "type": "object",
                            "description": "The state dict, passed through unchanged",
                        },
                    },
                },
            ),
            "stat.length": HostFunctionDef(
                "stat.length",
                "transform_only",
                _length,
                description=(
                    "Count the number of items in a list or keys in an object. "
                    "Accepts a JSON array, object, or JSON string. "
                    "Use to count results from web.fetch_json before sending a summary."
                ),
                input_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "items": {"description": "Array, object, or JSON string to count"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": "Number of items"},
                    },
                },
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        return []


def _advance_chain(
    iteration=0,
    remaining_iterations=None,
    initial_remaining=None,
    deadline_unix=None,
    duration_seconds=None,
    initial_deadline=None,
    state=None,
    previous_state=None,
    initial_state=None,
    *,
    now,
):
    """Progress a finite or indefinite chain; keep state without resetting counters."""
    deadline_unix = deadline_unix if deadline_unix is not None else initial_deadline
    if deadline_unix is None and duration_seconds is not None:
        deadline_unix = now + float(duration_seconds)
    current = int(iteration or 0) + 1
    remaining = remaining_iterations if remaining_iterations is not None else initial_remaining
    remaining = max(0, int(remaining) - 1) if remaining is not None else None
    effective_state = state if state is not None else previous_state
    if effective_state is None:
        effective_state = initial_state or {}
    return {
        "iteration": current,
        "deadline_unix": deadline_unix,
        "remaining_iterations": remaining,
        "state": effective_state,
        "done": remaining == 0 or (deadline_unix is not None and now >= float(deadline_unix)),
    }


def _chain_step(**kwargs):
    """Use server time for real execution; tests supply a virtual clock to _advance_chain."""
    import time

    return _advance_chain(now=time.time(), **kwargs)


def _collect(current, previous_samples=None, initial_samples=None):
    previous = previous_samples if previous_samples is not None else initial_samples
    return {"samples": list(previous or []) + [current]}
