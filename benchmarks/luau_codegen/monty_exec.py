"""The `python` target: scripts run in Monty (pydantic-monty), type-checked by its bundled ty.

Tool stubs stay in Lua (the task files are shared by every target): each host
function runs its stub in lupa and converts tables both ways, so the Python
script sees exactly the data the Luau script sees.
"""

import re
import threading
from functools import lru_cache

import lupa
import pydantic_monty as pm

from benchmarks.luau_codegen import pytypes

EXEC_LIMITS: pm.ResourceLimits = {
    "max_feed_duration_secs": 2.0,
    "max_memory": 64 * 1024 * 1024,
    "max_recursion_depth": 200,
}
PRELUDE = "import tools\n"  # the script sees `tools` the way Luau sees the global
_UNSUPPORTED = "monty syntax parser does not yet support"
_LINE = re.compile(r"main\.py:(\d+)")
_LINE_WORD = re.compile(r"\bline (\d+)")

_pool_lock = threading.Lock()
_pool: pm.Monty | None = None


def pool() -> pm.Monty:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = pm.Monty(max_processes=8).__enter__()
        return _pool


def monty_version() -> str:
    from importlib.metadata import version

    return version("pydantic-monty")


def _remap(text: str) -> str:
    """Line numbers refer to the script, not to the hidden prelude."""

    def fix(m: re.Match) -> str:
        n = int(m.group(1)) - PRELUDE.count("\n")
        return m.group(0).replace(m.group(1), str(max(n, 1)))

    return _LINE_WORD.sub(fix, _LINE.sub(fix, text))


def _message(e: Exception) -> str:
    text = str(e).strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # tracebacks end with "ExcType: message"; keep that plus the failing location
    tail = lines[-1] if lines else type(e).__name__
    where = next((ln.strip() for ln in reversed(lines) if "line " in ln or "main.py:" in ln), "")
    return _remap(f"{tail} ({where})" if where and where != tail else tail)


# ---------------------------------------------------------------------------
# Lua stubs as host functions
# ---------------------------------------------------------------------------


def _to_lua(lua, value):
    if isinstance(value, dict):
        t = lua.table()
        for k, v in value.items():
            t[k] = _to_lua(lua, v)
        return t
    if isinstance(value, (list, tuple)):
        t = lua.table()
        for i, v in enumerate(value, start=1):
            t[i] = _to_lua(lua, v)
        return t
    return value


def _from_lua(value):
    if lupa.lua_type(value) != "table":
        if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
            return int(value)
        return value
    items = list(value.items())
    n = len(items)
    if (
        n
        and all(isinstance(k, int) for k, _ in items)
        and sorted(k for k, _ in items) == list(range(1, n + 1))
    ):
        return [_from_lua(value[i]) for i in range(1, n + 1)]
    return {str(k): _from_lua(v) for k, v in items}


class ToolHost:
    """One per execution: the Lua stubs, call counters and max_calls budgets."""

    def __init__(self, task: dict):
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=False)
        self.calls: dict[str, int] = {}
        self.task = task
        self.stubs = {t["name"]: self.lua.eval(t["stub"].strip()) for t in task.get("tools", [])}
        self.limits = {t["name"]: t.get("max_calls") for t in task.get("tools", [])}

    def function(self, name: str):
        def call(args=None):
            n = self.calls.get(name, 0) + 1
            self.calls[name] = n
            limit = self.limits.get(name)
            if limit is not None and n > limit:
                raise RuntimeError(f"tool call budget exceeded: {name} (max {limit})")
            return _from_lua(self.stubs[name](_to_lua(self.lua, args if args is not None else {})))

        return call

    def modules(self, stubs: dict[str, str] | None) -> dict[str, pm.ExternalModule]:
        grouped: dict[str, dict] = {}
        for t in self.task.get("tools", []):
            ns, fn = t["name"].split(".", 1)
            grouped.setdefault(ns, {})[fn] = self.function(t["name"])
        sub = {
            ns: pm.ExternalModule(fns, stubs=(stubs or {}).get(ns)) for ns, fns in grouped.items()
        }
        root = "\n".join(f"from . import {ns}" for ns in sub) + "\n"
        return {"tools": pm.ExternalModule(sub, stubs=root)}


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _stubs_for(task_id: str, task_json: str) -> tuple[str, dict[str, str]]:
    import json

    task = json.loads(task_json)
    return pytypes.task_stubs(task), pytypes.tool_module_stubs(task)


def _stubs(task: dict) -> tuple[str, dict[str, str]]:
    import json

    slim = {k: task[k] for k in ("id", "types", "tools") if k in task}
    return _stubs_for(task["id"], json.dumps(slim, sort_keys=True))


def compile_errors(script: str) -> list[str]:
    """Parse errors, plus syntax Monty rejects at parse time (yield, method decorators…).
    Defining the functions runs no tool, so top-level code errors also show up here."""
    host = ToolHost({"id": "_compile", "tools": []})
    with pool().checkout(
        limits=EXEC_LIMITS, external_modules={"tools": pm.ExternalModule({})}
    ) as s:
        try:
            s.feed_run(PRELUDE + script)
        except pm.MontySyntaxError as e:
            return [_message(e)]
        except pm.MontyRuntimeError as e:
            if _UNSUPPORTED in str(e):
                return [_message(e)]
        except pm.MontyError as e:
            return [_message(e)]
    del host
    return []


def typecheck(task: dict, script: str) -> list[str]:
    types, module_stubs = _stubs(task)
    host = ToolHost(task)
    with pool().checkout(
        type_check=True,
        type_check_stubs=types,
        type_check_format="concise",
        external_modules=host.modules(module_stubs),
        limits=EXEC_LIMITS,
    ) as s:
        try:
            s.feed_run(PRELUDE + script)  # checked before it runs; only defines functions
        except pm.MontyTypingError as e:
            return [_remap(ln.strip()) for ln in str(e).splitlines() if "error[" in ln]
        except pm.MontyError:
            return []  # runtime problems are the execute stage's job
    return []


def execute(task: dict, script: str) -> tuple[list[dict], str | None]:
    """Returns ([{ok, output|error, calls}] per case, load/contract error)."""
    results = []
    for case in task["test_cases"]:
        host = ToolHost(task)
        with pool().checkout(limits=EXEC_LIMITS, external_modules=host.modules(None)) as s:
            try:
                s.feed_run(PRELUDE + script)
            except pm.MontyError as e:
                return [], "top-level error: " + _message(e)
            try:
                is_fn = s.feed_run("type(run).__name__") == "function"
            except pm.MontyError:
                is_fn = False
            if not is_fn:
                return [], "Script must define a 'run(input, context)' function (global, not local)"
            try:
                out = s.feed_run(
                    "run(__input, __context)",
                    inputs={
                        "__input": case["input"],
                        "__context": case.get("context", {"now": "2026-09-30T12:00:00Z"}),
                    },
                )
                results.append({"ok": True, "output": out, "calls": dict(host.calls)})
            except pm.MontyCrashedError as e:
                results.append(
                    {"ok": False, "error": f"worker crashed: {e}", "calls": dict(host.calls)}
                )
            except pm.MontyError as e:
                results.append({"ok": False, "error": _message(e), "calls": dict(host.calls)})
    return results, None
