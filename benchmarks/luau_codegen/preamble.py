"""Source builders: typed preamble for luau-analyze and the execution harness.

The execution harness is written in the common subset of Lua 5.5 (lupa) and
Luau so the same file runs on both targets. The generated script is wrapped in
a closure whose upvalues shadow the forbidden globals — in the Luau CLI the
builtin environment is read-only, so `os = nil` would not remove anything.
"""

import math

from ritesmith.runtime.sandbox import _FORBIDDEN_GLOBALS

RESULT_SENTINEL = "__RESULT__"

# Globals hidden from the script, per target. Lua mirrors the production sandbox.
HIDDEN_GLOBALS: dict[str, list[str]] = {
    "lua": list(_FORBIDDEN_GLOBALS),
    "luau": ["os", "debug", "getfenv", "setfenv", "loadstring", "require"],
}

BASE_TYPES = "type Context = { now: string }"
DEFAULT_CONTEXT = {"now": "2026-09-30T12:00:00Z"}


# ---------------------------------------------------------------------------
# Python value → Lua literal
# ---------------------------------------------------------------------------


def _lua_string(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def lua_literal(value) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float cannot be a Lua literal: {value}")
        return repr(value)
    if isinstance(value, str):
        return _lua_string(value)
    if isinstance(value, list):
        return "{" + ", ".join(lua_literal(v) for v in value) + "}"
    if isinstance(value, dict):
        items = ", ".join(f"[{_lua_string(str(k))}] = {lua_literal(v)}" for k, v in value.items())
        return "{" + items + "}"
    raise TypeError(f"unsupported value for Lua literal: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Typed preamble (luau-analyze)
# ---------------------------------------------------------------------------


def _tools_by_namespace(task: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for tool in task.get("tools", []):
        ns, fn = tool["name"].split(".", 1)
        grouped.setdefault(ns, []).append({**tool, "fn": fn})
    return grouped


def tools_type_decl(task: dict) -> str:
    grouped = _tools_by_namespace(task)
    if not grouped:
        return ""
    lines = ["local tools: {"]
    for ns, tools in grouped.items():
        lines.append(f"    {ns}: {{")
        lines.extend(f"        {t['fn']}: {t['signature']}," for t in tools)
        lines.append("    },")
    lines.append("} = (nil :: any)")
    return "\n".join(lines)


def task_types(task: dict) -> str:
    return "\n".join(p for p in (BASE_TYPES, task.get("types", "").strip()) if p)


def strip_directives(script: str) -> str:
    """Drop `--!strict` style directives; the preamble decides the mode."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("--!"))


def analyze_source(task: dict, script: str, mode: str) -> tuple[str, int]:
    """Returns (source, line_offset) — subtract offset from reported line numbers."""
    header = "\n".join(
        p for p in (f"--!{mode}", task_types(task), tools_type_decl(task), "-- script") if p
    )
    offset = header.count("\n") + 1
    return header + "\n" + strip_directives(script), offset


# ---------------------------------------------------------------------------
# Execution harness (luau CLI / lupa)
# ---------------------------------------------------------------------------

_JSON_ENCODER = r"""
local __print, __pcall, __tostring, __type = print, pcall, tostring, type
local __pairs, __ipairs, __sort, __concat = pairs, ipairs, table.sort, table.concat
local __format, __gsub, __byte, __floor, __abs = string.format, string.gsub, string.byte, math.floor, math.abs
local __huge = math.huge
local __escapes = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
local function __jstr(s)
  local escaped = __gsub(s, '[%c"\\]', function(c)
    return __escapes[c] or __format("\\u%04x", __byte(c))
  end)
  return '"' .. escaped .. '"'
end
local function __json(v, depth)
  depth = depth or 0
  if depth > 40 then return '"<max depth>"' end
  local t = __type(v)
  if t == "nil" then return "null" end
  if t == "boolean" then return __tostring(v) end
  if t == "number" then
    if v ~= v or v == __huge or v == -__huge then return "null" end
    if __floor(v) == v and __abs(v) < 1e15 then return __format("%d", v) end
    return __format("%.14g", v)
  end
  if t == "string" then return __jstr(v) end
  if t ~= "table" then return __jstr("<" .. t .. ">") end
  local count = 0
  for _ in __pairs(v) do count = count + 1 end
  local n = #v
  local parts = {}
  if count > 0 and n == count then
    for i = 1, n do parts[i] = __json(v[i], depth + 1) end
    return "[" .. __concat(parts, ",") .. "]"
  end
  local keys = {}
  for k in __pairs(v) do keys[#keys + 1] = __tostring(k) end
  __sort(keys)
  local lookup = {}
  for k, val in __pairs(v) do lookup[__tostring(k)] = val end
  for i, k in __ipairs(keys) do parts[i] = __jstr(k) .. ":" .. __json(lookup[k], depth + 1) end
  return "{" .. __concat(parts, ",") .. "}"
end
"""


def _tools_runtime(task: dict) -> str:
    lines = ["local __calls = {}", "local __stubs = {}", "local __limits = {}", "tools = {}"]
    for ns, tools in _tools_by_namespace(task).items():
        lines.append(f"tools.{ns} = {{}}")
        for t in tools:
            name = t["name"]
            lines.append(f"__stubs[{_lua_string(name)}] = {t['stub'].strip()}")
            if t.get("max_calls") is not None:
                lines.append(f"__limits[{_lua_string(name)}] = {int(t['max_calls'])}")
            lines.append(
                f"tools.{ns}.{t['fn']} = function(...)\n"
                f"  local n = (__calls[{_lua_string(name)}] or 0) + 1\n"
                f"  __calls[{_lua_string(name)}] = n\n"
                f"  local limit = __limits[{_lua_string(name)}]\n"
                f"  if limit and n > limit then\n"
                f"    error({_lua_string(f'tool call budget exceeded: {name} (max ')} .. limit .. ')')\n"
                f"  end\n"
                f"  return __stubs[{_lua_string(name)}](...)\n"
                f"end"
            )
    return "\n".join(lines)


def exec_source(task: dict, script: str, target: str, cases: list[dict]) -> tuple[str, int]:
    """Returns (source, line_offset) for running every test case in one process."""
    hidden = HIDDEN_GLOBALS[target]
    shadow = f"local {', '.join(hidden)}\n" if hidden else ""
    head = (
        _JSON_ENCODER
        + _tools_runtime(task)
        + "\n"
        + shadow
        + "local __load_ok, __load_err = __pcall(function()\n"
    )
    offset = head.count("\n")
    body = strip_directives(script) if target == "luau" else script
    cases_lit = lua_literal(
        [{"input": c["input"], "context": c.get("context", DEFAULT_CONTEXT)} for c in cases]
    )
    tail = f"""
end)
local __cases = {cases_lit}
local __results = {{}}
if not __load_ok then
  __print({_lua_string(RESULT_SENTINEL)} .. __json({{ load_error = __tostring(__load_err) }}))
elseif __type(run) ~= "function" then
  __print({_lua_string(RESULT_SENTINEL)} .. __json({{ contract_error = true }}))
else
  for i, c in __ipairs(__cases) do
    for k in __pairs(__calls) do __calls[k] = 0 end
    local ok, out = __pcall(run, c.input, c.context)
    local calls = {{}}
    for k, v in __pairs(__calls) do calls[k] = v end
    if ok then
      __results[i] = {{ ok = true, output = out, calls = calls }}
    else
      __results[i] = {{ ok = false, error = __tostring(out), calls = calls }}
    end
  end
  __print({_lua_string(RESULT_SENTINEL)} .. __json({{ cases = __results }}))
end
"""
    return head + body + tail, offset
