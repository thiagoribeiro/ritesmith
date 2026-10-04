"""Runtime Luau via LunarDyson (luau_script).

Os scripts chamam as host functions como `tools.<namespace>.<fn>({ ... })`: um
único argumento-tabela cujos campos viram kwargs da função Python. Os nomes dos
campos vêm do `input_schema` do provider (que casa com os parâmetros da função)
ou da assinatura Python para as funções do núcleo.

Cada thread do executor mantém um lunardyson.Runtime selado por (profile,
timeout, memória): o runtime não é thread-safe, mas é reaproveitado entre
execuções. Timeout, memória e sandbox são aplicados pela própria lib — não há
thread "zumbi" como no sandbox do lupa.

Os tipos Luau para o checker (`ld_check`) são derivados dos JSON Schemas que o
RiteSmith já tem: `input_schema`/`output_schema` dos providers e da requisição.

Erros são mapeados para os mesmos prefixos do sandbox Lua ("Runtime error:",
"Syntax/load error:") para que o self-heal do ExecutionService funcione igual.
"""

import asyncio
import functools
import inspect
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import jsonschema

from ritesmith.config import Settings, get_settings
from ritesmith.observability.metrics import lua_timeout_total
from ritesmith.runtime.base import RuntimeResult
from ritesmith.runtime.host_functions import get_functions_for_profile

log = logging.getLogger(__name__)

# json.* existe só para o Lua (as tools do Luau já trocam tabelas).
_EXCLUDED_NAMESPACES = {"json"}

# Classe de efeito por tier de profile — metadado para os budgets do LunarDyson.
_EFFECT_BY_TIER = {
    "transform_only": "pure",
    "readonly_network": "read",
    "analytics_local": "read",
    "sensitive_personal": "read",
    "notification": "external_write",
    "filesystem_write": "write",
    "reporting": "write",
    "side_effects": "external_write",
    "trusted_internal": "external_write",
}

_HTTP_RESULT = "{ ok: boolean?, status: number?, body: any, error: string?, message: string? }"

# Funções do núcleo sem input_schema: (assinatura Luau, ordem posicional ou None = kwargs)
_CORE_TOOLS: dict[str, tuple[str, list[str] | None]] = {
    "http.get_json": (f"(args: {{ url: string }}) -> {_HTTP_RESULT}", None),
    "http.post_json": (f"(args: {{ url: string, body: any }}) -> {_HTTP_RESULT}", None),
    "http.request": (
        (
            f"(args: {{ method: string, url: string, headers: {{ [string]: string }}?, body: any }})"
            f" -> {_HTTP_RESULT}"
        ),
        None,
    ),
    "text.slugify": ("(args: { s: string }) -> string", ["s"]),
    "text.upper": ("(args: { s: string }) -> string", ["s"]),
    "text.lower": ("(args: { s: string }) -> string", ["s"]),
    "text.strip": ("(args: { s: string }) -> string", ["s"]),
    "time.now_utc": ("(args: {}?) -> string", []),
    "time.timestamp": ("(args: {}?) -> number", []),
    "casp.query": (
        "(args: { resource_type: string, filters: { [string]: any }?, capability: string? }) -> any",
        None,
    ),
    "casp.resolve": (
        "(args: { resource_type: string, capability: string, hint: string }) -> any",
        None,
    ),
    "casp.execute": (
        (
            "(args: { resource_id: string, resource_type: string, capability: string,"
            " input_data: { [string]: any }? }) -> any"
        ),
        None,
    ),
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LUAU_RESERVED = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
    "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
}  # fmt: skip


@functools.cache
def luau_available() -> bool:
    try:
        import lunardyson  # noqa: F401
    except Exception as e:
        log.warning("lunardyson not available (%s); luau scripts cannot run", e)
        return False
    return True


def effective_script_language(settings: Settings | None = None) -> str:
    """Language for newly generated scripts: settings.script_language, or "lua" as fallback."""
    settings = settings or get_settings()
    if settings.script_language == "luau" and luau_available():
        return "luau"
    return "lua"


def script_artifact_type(language: str) -> str:
    return "luau_script" if language == "luau" else "lua_script"


# ---------------------------------------------------------------------------
# JSON Schema → Luau types
# ---------------------------------------------------------------------------


def _optional(t: str) -> str:
    if t == "any" or t.endswith("?"):
        return t
    return f"({t})?" if "|" in t else f"{t}?"


def schema_to_luau(schema: dict | None, *, fields_present: bool = False, _depth: int = 0) -> str:
    """Converts a JSON Schema into a Luau type.

    fields_present=True treats every object property as present (used for tool
    results, whose schemas rarely list `required`); otherwise properties outside
    `required` are optional.
    """
    if not isinstance(schema, dict) or _depth > 6:
        return "any"
    recurse = functools.partial(schema_to_luau, fields_present=fields_present, _depth=_depth + 1)

    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            parts = sorted({recurse(s) for s in schema[key]})
            return "any" if "any" in parts else " | ".join(parts)

    enum = schema.get("enum")
    if (
        isinstance(enum, list)
        and enum
        and all(isinstance(v, str) and _IDENT.match(v) for v in enum)
    ):
        return " | ".join(f'"{v}"' for v in enum)

    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        inner = " | ".join(recurse({**schema, "type": x}) for x in non_null) or "nil"
        return _optional(inner) if "null" in t else inner

    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            extra = schema.get("additionalProperties")
            return "{ [string]: " + (recurse(extra) if isinstance(extra, dict) else "any") + " }"
        if any(not _IDENT.match(k) or k in _LUAU_RESERVED for k in props):
            return "{ [string]: any }"
        required = set(schema.get("required") or [])
        fields = []
        for name, sub in props.items():
            ft = recurse(sub)
            fields.append(f"{name}: {ft if fields_present or name in required else _optional(ft)}")
        return "{ " + ", ".join(fields) + " }"

    if t == "array":
        return "{ " + recurse(schema.get("items")) + " }"
    return {
        "string": "string",
        "number": "number",
        "integer": "number",
        "boolean": "boolean",
        "null": "nil",
    }.get(t, "any")


_ERROR_FIELDS = "read error: string, read message: string?"


def _output_type(output_schema: dict | None) -> str:
    """Luau type for run()'s result: the schema's table, or the {error, message} convention.

    Properties are `read` (covariant): with read-write properties the strict solver
    rejects `{ message = "x" }` where `message: string?` is expected. A union also
    rejects literals that omit an optional field, so schemas with optional fields get
    a single table where every field is optional — weaker, but no false positives;
    jsonschema still validates the real contract at execution time.
    """
    props = (output_schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return f"{{ [string]: any }} | {{ {_ERROR_FIELDS} }}"
    table = schema_to_luau({k: v for k, v in output_schema.items() if k != "required"})
    if table.startswith("{ ["):  # names that are not valid Luau identifiers
        return f"{table} | {{ {_ERROR_FIELDS} }}"
    required = set(output_schema.get("required") or [])
    fields = []
    for name, sub in props.items():
        ftype = schema_to_luau(sub)
        fields.append(f"read {name}: {ftype if name in required else _optional(ftype)}")
    if required >= set(props):
        return "{ " + ", ".join(fields) + f" }} | {{ {_ERROR_FIELDS} }}"
    fields = [f"read {name}: {_optional(schema_to_luau(sub))}" for name, sub in props.items()]
    return "{ " + ", ".join(fields + [_ERROR_FIELDS.replace("string,", "string?,")]) + " }"


def script_type_declarations(input_schema: dict | None, output_schema: dict | None) -> str:
    """`Context`, `Input` and `Output` aliases for a script's run(input, context): Output."""
    inp = schema_to_luau(input_schema) if input_schema else "{ [string]: any }"
    return (
        f"type Context = {{ [string]: any }}\ntype Input = {inp}\n"
        f"type Output = {_output_type(output_schema)}\n"
    )


def _tool_output_type(fn_def) -> str:
    out_schema = getattr(fn_def, "output_schema", None)
    if not out_schema:
        return "any"
    out = schema_to_luau(out_schema, fields_present=True)
    is_object = out_schema.get("type") == "object" or "properties" in out_schema
    if is_object and out.startswith("{ ") and not out.startswith("{ [") and "error:" not in out:
        out = out[:-2] + ", error: string?, message: string? }"  # tools report failures inline
    return out


def tool_signature(name: str, fn_def) -> tuple[str, list[str] | None]:
    """(Luau signature, positional argument order or None for kwargs)."""
    in_schema = getattr(fn_def, "input_schema", None)
    if not in_schema and name in _CORE_TOOLS:
        return _CORE_TOOLS[name]
    out = _tool_output_type(fn_def)
    if in_schema and in_schema.get("properties"):
        return f"(args: {schema_to_luau(in_schema)}) -> {out}", None

    try:
        params = [
            p
            for p in inspect.signature(fn_def.callable).parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    except (TypeError, ValueError):
        return f"(args: {{ [string]: any }}?) -> {out}", None
    if not params:
        return f"(args: {{}}?) -> {out}", None
    fields = ", ".join(f"{p.name}: {'any' if p.default is p.empty else 'any?'}" for p in params)
    return f"(args: {{ {fields} }}) -> {out}", None


def luau_tools_for_profile(profile: str) -> dict:
    return {
        name: fn_def
        for name, fn_def in get_functions_for_profile(profile).items()
        if name.count(".") == 1 and name.split(".")[0] not in _EXCLUDED_NAMESPACES
    }


def describe_tools_for_prompt(profile: str) -> list[str]:
    lines = []
    for name, fn_def in sorted(luau_tools_for_profile(profile).items()):
        signature, _ = tool_signature(name, fn_def)
        description = getattr(fn_def, "description", "") or ""
        lines.append(f"tools.{name}: {signature}" + (f"  -- {description}" if description else ""))
    return lines


# ---------------------------------------------------------------------------
# Runtimes
# ---------------------------------------------------------------------------


# Sentinel kept out of the "Runtime error:" self-heal path: a tool timeout is a
# latency event, not a bad artifact, so it must not auto-deprecate + regenerate.
_TOOL_TIMEOUT_SENTINEL = "RiteSmith tool timeout"


def _invoke(fn: Callable, positional: list[str] | None, args: dict):
    if positional is not None:
        return fn(*[args[k] for k in positional if k in args])
    return fn(**args)


def _adapter(fn: Callable, positional: list[str] | None) -> Callable:
    def call(args):
        if args is None or args == []:
            args = {}
        if not isinstance(args, dict):
            raise TypeError(
                "tools take a single table of named arguments, e.g. tools.ns.fn({ x = 1 })"
            )
        # Per-tool wall-clock timeout. The VM CPU deadline cannot interrupt a blocked
        # Python call, so run it on a side thread with a deadline. A timed-out thread
        # cannot be force-killed (it leaks until it returns), but the VM is freed and
        # network clients already carry their own httpx timeouts.
        timeout_ms = get_settings().luau_tool_timeout_ms
        if not timeout_ms or timeout_ms <= 0:
            return _invoke(fn, positional, args)
        future = _get_tool_executor().submit(_invoke, fn, positional, args)
        try:
            return future.result(timeout=timeout_ms / 1000)
        except FuturesTimeout as e:
            future.cancel()
            raise TimeoutError(f"{_TOOL_TIMEOUT_SENTINEL}: exceeded {timeout_ms}ms") from e

    return call


def build_runtime(profile: str, cpu_time_ms: int, memory_mb: float, type_decls: str | None = None):
    import lunardyson as ld

    rt = ld.Runtime(memory_mb=memory_mb, cpu_time_ms=cpu_time_ms)
    if type_decls:
        rt.declare_types(type_decls)
    for name, fn_def in sorted(luau_tools_for_profile(profile).items()):
        signature, positional = tool_signature(name, fn_def)
        rt.tool(
            name,
            _adapter(fn_def.callable, positional),
            signature=signature,
            effect=_EFFECT_BY_TIER.get(fn_def.profile, "read"),
        )
    return rt


_local = threading.local()
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_tool_executor: ThreadPoolExecutor | None = None
_tool_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=get_settings().lua_sandbox_workers, thread_name_prefix="luau-sandbox"
            )
        return _executor


def _get_tool_executor() -> ThreadPoolExecutor:
    """Side pool for per-tool timeouts; distinct from the sandbox pool to avoid deadlock."""
    global _tool_executor
    with _tool_executor_lock:
        if _tool_executor is None:
            _tool_executor = ThreadPoolExecutor(
                max_workers=max(8, get_settings().lua_sandbox_workers * 2),
                thread_name_prefix="luau-tool",
            )
        return _tool_executor


def shutdown_executor() -> None:
    global _executor, _tool_executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None
    with _tool_executor_lock:
        if _tool_executor is not None:
            _tool_executor.shutdown(wait=False)
            _tool_executor = None


def _pooled_runtime(profile: str, cpu_time_ms: int, memory_mb: float):
    pool = _local.__dict__.setdefault("runtimes", {})
    key = (profile, cpu_time_ms, memory_mb)
    rt = pool.get(key)
    if rt is None:
        rt = pool[key] = build_runtime(profile, cpu_time_ms, memory_mb)
    return rt


def run_luau(
    content: str,
    input_data: dict,
    context: dict,
    profile: str,
    timeout_ms: int,
    memory_mb: float,
) -> tuple[dict | None, str | None, bool, int]:
    """Runs a script synchronously. Returns (output, error, timed_out, peak_memory_bytes)."""
    rt = _pooled_runtime(profile, timeout_ms, memory_mb)
    result = rt.execute(content, input_data, context)
    peak = result.stats.peak_memory_bytes
    if result.ok:
        return result.output, None, False, peak
    kind, message = result.error_kind, result.message
    if kind == "timeout":
        return None, f"Execution timed out after {timeout_ms}ms", True, peak
    # A per-tool timeout surfaces as a runtime error carrying our sentinel; treat it
    # as a timeout (timed_out=True, no "Runtime error:" prefix) so it does not trigger
    # the auto-deprecate/regenerate self-heal — a slow tool is not a broken artifact.
    if message and _TOOL_TIMEOUT_SENTINEL in message:
        return None, f"Tool call timed out: {message}", True, peak
    if kind == "syntax":
        return None, f"Syntax/load error: {message}", False, peak
    if kind == "contract":
        return None, message, False, peak
    # runtime, memory, unknown_tool, tool/effect budget: the script itself misbehaved
    return None, f"Runtime error: {message}", False, peak


class LuauScriptRuntime:
    """Same interface as LuaScriptRuntime, for luau_script artifacts."""

    def __init__(self, settings: Settings):
        self.default_timeout_ms = settings.lua_timeout_ms
        self.memory_limit_mb = settings.lua_memory_limit_mb
        self.wall_clock_ms = settings.luau_wall_clock_ms

    async def execute(
        self,
        content: str,
        input_data: dict,
        context: dict,
        timeout_ms: int | None = None,
        profile: str = "transform_only",
        output_schema: dict | None = None,
    ) -> RuntimeResult:
        timeout = timeout_ms if timeout_ms is not None else self.default_timeout_ms
        start = time.monotonic()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            _get_executor(),
            run_luau,
            content,
            input_data,
            context,
            profile,
            timeout,
            self.memory_limit_mb,
        )
        # Wall-clock backstop: if a tool (or chain of tools) blocks past the cap, stop
        # waiting and return a timeout. The sandbox thread is abandoned, not killed —
        # it keeps running until its own tool/VM deadlines release it.
        if self.wall_clock_ms and self.wall_clock_ms > 0:
            try:
                output, error, timed_out, peak = await asyncio.wait_for(
                    fut, timeout=self.wall_clock_ms / 1000
                )
            except TimeoutError:
                lua_timeout_total.inc()
                return RuntimeResult(
                    output={},
                    error=f"Execution abandoned after wall-clock {self.wall_clock_ms}ms",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    memory_used_bytes=0,
                    timed_out=True,
                )
        else:
            output, error, timed_out, peak = await fut
        duration_ms = int((time.monotonic() - start) * 1000)
        if timed_out:
            lua_timeout_total.inc()
        if error:
            return RuntimeResult(
                output={},
                error=error,
                duration_ms=duration_ms,
                memory_used_bytes=peak,
                timed_out=timed_out,
            )
        if output_schema and output is not None:
            try:
                jsonschema.validate(instance=output, schema=output_schema)
            except jsonschema.ValidationError as e:
                return RuntimeResult(
                    output=output or {},
                    error=f"Output schema validation failed: {e.message}",
                    duration_ms=duration_ms,
                    memory_used_bytes=peak,
                )
        return RuntimeResult(output=output or {}, duration_ms=duration_ms, memory_used_bytes=peak)

    def check(
        self,
        content: str,
        *,
        profile: str = "transform_only",
        input_schema: dict | None = None,
        output_schema: dict | None = None,
    ) -> dict[str, list[dict]]:
        """Type-checks against the profile's tools and the script's Input/Output types.

        Returns {"strict": [...], "nonstrict": [...]} with error-severity diagnostics only.
        """
        rt = build_runtime(
            profile,
            self.default_timeout_ms,
            self.memory_limit_mb,
            type_decls=script_type_declarations(input_schema, output_schema),
        )
        try:
            return {
                mode: [
                    d
                    for d in rt.check(content, strict=(mode == "strict"))
                    if d["severity"] == "error"
                ]
                for mode in ("nonstrict", "strict")
            }
        finally:
            rt.close()
