"""Sandbox Lua via Lupa.

Cada execução cria uma nova LuaRuntime isolada.
Globals perigosos são removidos após a criação.
Host functions são injetadas conforme o runtime profile.
Timeout é aplicado via ThreadPoolExecutor + Future.result(timeout).

Limitação conhecida: quando o timeout estoura, a thread Lua não é
terminada à força — ela continuará rodando até que o código Lua ceda
controle ou finalize naturalmente. Em produção, o isolamento real
exige subprocess/WASM (deferred para V1).
"""

import atexit
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from ritesmith.config import get_settings
from ritesmith.observability.metrics import (
    lua_timeout_total,
    sandbox_conversion_depth,
    sandbox_queue_depth,
)
from ritesmith.runtime._conversion import lua_to_python, python_to_lua
from ritesmith.runtime.host_functions import get_functions_for_profile

log = logging.getLogger(__name__)

_FORBIDDEN_GLOBALS = [
    "io",
    "os",
    "debug",
    "load",
    "loadfile",
    "dofile",
    "require",
    "package",
    "rawget",
    "rawset",
    "rawequal",
    "collectgarbage",
    "newproxy",
]

_EXECUTOR = ThreadPoolExecutor(
    max_workers=get_settings().lua_sandbox_workers,
    thread_name_prefix="lua-sandbox",
)
# Shutdown is handled by FastAPI lifespan; atexit is a fallback for non-server usage
atexit.register(_EXECUTOR.shutdown, wait=False)


def _inject_host_functions(lua, profile: str) -> None:
    """Injeta host functions no namespace Lua usando tabelas aninhadas."""
    from ritesmith.runtime._conversion import python_to_lua

    fns = get_functions_for_profile(profile)
    namespaces: dict[str, dict] = {}

    def _wrap(fn):
        """Convert dict/list return values to Lua tables so Lua operators work."""

        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, (dict, list, tuple)):
                return python_to_lua(lua, result)
            return result

        return wrapper

    for full_name, fn_def in fns.items():
        parts = full_name.split(".", 1)
        ns, fname = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], parts[0])
        if ns not in namespaces:
            namespaces[ns] = {}
        namespaces[ns][fname] = _wrap(fn_def.callable)

    for ns, methods in namespaces.items():
        tbl = lua.table()
        for fname, fn in methods.items():
            tbl[fname] = fn
        lua.globals()[ns] = tbl


def _remove_dangerous_globals(lua) -> None:
    g = lua.globals()
    for name in _FORBIDDEN_GLOBALS:
        try:
            g[name] = None
        except Exception:
            log.debug("could not remove Lua global %r", name, exc_info=True)


def run_in_sandbox(
    content: str,
    input_data: dict,
    context: dict,
    profile: str = "transform_only",
    timeout_ms: int = 1000,
) -> tuple[dict | None, str | None, bool]:
    """Executa script Lua em sandbox.

    Returns:
        (output_dict, error_message, timed_out)
    """

    def _execute() -> tuple[dict | None, str | None]:
        from lupa import LuaError, LuaRuntime

        lua = LuaRuntime(unpack_returned_tuples=False)
        _remove_dangerous_globals(lua)
        _inject_host_functions(lua, profile)

        lua_input = python_to_lua(lua, input_data)
        lua_context = python_to_lua(lua, context)

        try:
            lua.execute(content)
        except LuaError as e:
            return None, f"Syntax/load error: {e}"

        run_fn = lua.globals().run
        if run_fn is None:
            return None, "Script must define a 'run(input, context)' function"

        try:
            result = run_fn(lua_input, lua_context)
        except LuaError as e:
            return None, f"Runtime error: {e}"
        except Exception as e:
            return None, f"Unexpected error: {e}"

        if result is None:
            return None, "run() returned nil — must return a table"

        max_depth: list[int] = [0]
        output = lua_to_python(result, _max_depth=max_depth)
        sandbox_conversion_depth.observe(max_depth[0])
        return output, None

    timeout_s = timeout_ms / 1000
    sandbox_queue_depth.inc()
    future = _EXECUTOR.submit(_execute)
    try:
        output, error = future.result(timeout=timeout_s)
        return output, error, False
    except FuturesTimeoutError:
        lua_timeout_total.inc()
        return None, f"Execution timed out after {timeout_ms}ms", True
    except Exception as e:
        return None, f"Executor error: {e}", False
    finally:
        sandbox_queue_depth.dec()
