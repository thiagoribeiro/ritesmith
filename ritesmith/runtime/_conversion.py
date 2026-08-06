"""Shared Lua ↔ Python value conversion with depth and size guards."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_MAX_DEPTH = 32
_MAX_LIST_LEN = 1000


def lua_to_python(val, *, _depth: int = 0, _max_depth: list[int] | None = None) -> object:
    """Recursively convert a Lua table to a Python dict/list/scalar.

    Pass _max_depth=[0] to track the deepest recursion level reached.
    """
    if _max_depth is not None and _depth > _max_depth[0]:
        _max_depth[0] = _depth
    if _depth > _MAX_DEPTH:
        return str(val)
    try:
        if hasattr(val, "items"):
            return {
                lua_to_python(k, _depth=_depth + 1, _max_depth=_max_depth): lua_to_python(
                    v, _depth=_depth + 1, _max_depth=_max_depth
                )
                for k, v in val.items()
            }
        if hasattr(val, "__iter__") and not isinstance(val, (str, bytes)):
            items: list[object] = []
            for i, v in enumerate(val):
                if i >= _MAX_LIST_LEN:
                    break
                items.append(lua_to_python(v, _depth=_depth + 1, _max_depth=_max_depth))
            return items
    except Exception:
        log.debug("lua_to_python: falling back to raw value for %r", val, exc_info=True)
    return val


def python_to_lua(lua_runtime, val, *, _depth: int = 0) -> object:
    """Recursively convert a Python dict/list/tuple to a Lua table."""
    if _depth > _MAX_DEPTH:
        return str(val)
    if isinstance(val, dict):
        tbl = lua_runtime.table()
        for k, v in val.items():
            tbl[k] = python_to_lua(lua_runtime, v, _depth=_depth + 1)
        return tbl
    if isinstance(val, (list, tuple)):
        tbl = lua_runtime.table()
        for i, v in enumerate(val[:_MAX_LIST_LEN], start=1):
            tbl[i] = python_to_lua(lua_runtime, v, _depth=_depth + 1)
        return tbl
    return val
