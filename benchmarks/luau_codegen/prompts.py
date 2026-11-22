"""Generation/repair prompts for the benchmark.

Derived from ritesmith/llm/prompts.py (lua_generation_system / lua_repair_*),
adapted to the `tools.ns.fn({...})` ABI proposed for LunarDyson. Lua and Luau
share the same text except for the LANGUAGE section, so the comparison
isolates the language rather than the prompt.
"""

import json

from benchmarks.luau_codegen import pytypes
from benchmarks.luau_codegen.preamble import task_types

LANG_NAME = {"lua": "Lua", "luau": "Luau", "python": "Python"}

_FORBIDDEN = {
    "lua": "require, os, io, debug, load, loadfile, dofile,\n  rawget, rawset, package, collectgarbage, newproxy",
    "luau": "require, os, io, debug, load, loadfile, dofile,\n  rawget, rawset, package, collectgarbage, newproxy,\n  getfenv, setfenv, loadstring",
}

_LANGUAGE = {
    "lua": """\
LANGUAGE: standard Lua 5.4
  - Only the standard string, table and math libraries are available""",
    "python": """\
LANGUAGE: Python, run by Monty — a sandboxed SUBSET of Python 3.14, not CPython
  - `tools` is already imported; do not import it again
  - Available: functions, f-strings, comprehensions, dicts/lists/sets/tuples, slicing,
    sorted/min/max/sum/len/round/abs/enumerate/zip/range, str methods, try/except,
    and the modules json, re, math, datetime (import them if you need them)
  - NOT available: generators/yield, del, classes with @property/@staticmethod/
    @classmethod or super(), user-defined exception classes, callable(), enum, abc,
    os, sys, io, open(), eval/exec, globals()
  - Type annotations are optional, but the script is type-checked with ty against the
    predeclared types, so any annotation you write must be correct""",
    "luau": """\
LANGUAGE: Luau (the typed Lua dialect from Roblox) — NOT Lua 5.x
  - Not available: goto, labels, <const>/<close>, math.tointeger, math.type, io, os
  - Available: continue, compound assignment (+=, -=, ..=), if-then-else expressions,
    string interpolation `{x}`, generalized iteration (for k, v in t do),
    string.split, table.find, table.clone, table.create
  - Type annotations are optional, but the script is type-checked with luau-analyze,
    so any annotation you write must be correct""",
}

RESPONSE_SCHEMA = {
    lang: json.dumps(
        {
            "script": f"<{LANG_NAME[lang]} code string>",
            "name": "<snake_case_name>",
            "description": "<one line description>",
        },
        indent=2,
    )
    for lang in LANG_NAME
}

REPAIR_SCHEMA = {
    lang: json.dumps(
        {
            "repaired_content": f"<corrected {LANG_NAME[lang]} code>",
            "changes_made": "<brief explanation of what was fixed>",
        },
        indent=2,
    )
    for lang in LANG_NAME
}


_PY_SYSTEM = """\
You are a Python script generator for RiteSmith, a sandboxed execution runtime.

MANDATORY RULES
- Define exactly ONE top-level entry point: def run(input, context) — helpers are other
  top-level functions
- No recursion, no infinite loops
- FORBIDDEN: imports of os, sys, subprocess, socket, io, pathlib; open, eval, exec,
  compile, __import__, globals, getattr, setattr
- External effects happen ONLY through the tools.* functions listed in the user message —
  nothing else exists
- Always return a dict — never None, never a bare primitive
- Be deterministic
- context["now"] is the current time as an ISO-8601 UTC string

TOOL CALLING CONVENTION
  Every tool takes a single dict argument and returns a dict:
    {"ok": True, ...} on success
    {"ok": False, "error": <code>, "message": <text>} on failure
  Always check result["ok"] before using the other fields.

ERROR HANDLING CONVENTION
  On recoverable error, return a dict with an "error" field:
    return {"error": "not_found", "message": "item does not exist"}
  Never raise and never use assert — an uncaught exception wastes an attempt.

OUTPUT CONTRACT
  The return value must be plain data (dict/list/str/number/bool/None).
  Every field required by the goal and output schema must be present and typed correctly.

{language}

STYLE GUIDE
  - One responsibility per script
  - Comments only when the logic would not be obvious to a reader

Respond with valid JSON matching the schema in the user message — nothing else."""


def generation_system(target: str) -> str:
    if target == "python":
        return _PY_SYSTEM.replace("{language}", _LANGUAGE["python"])
    lang = LANG_NAME[target]
    return f"""\
You are a {lang} script generator for RiteSmith, a sandboxed execution runtime.

MANDATORY RULES
- Define exactly ONE global function: run(input, context) — helpers must be local functions
- No recursion, no infinite loops
- FORBIDDEN globals: {_FORBIDDEN[target]}
- External effects happen ONLY through the tools.* functions listed in the user message —
  nothing else exists
- Always return a {lang} table — never nil, never a bare primitive
- Be deterministic
- context.now is the current time as an ISO-8601 UTC string

TOOL CALLING CONVENTION
  Every tool takes a single table argument and returns a table:
    {{ok = true, ...}} on success
    {{ok = false, error = <code>, message = <text>}} on failure
  Always check result.ok before using the other fields.

ERROR HANDLING CONVENTION
  On recoverable error, return a table with an "error" field:
    return {{error = "not_found", message = "item does not exist"}}
  Never call error() or assert() — the sandbox catches panics but wastes an attempt.

OUTPUT CONTRACT
  The return value must be a plain table (no userdata, no functions).
  Every field required by the goal and output schema must be present and typed correctly.

{_LANGUAGE[target]}

STYLE GUIDE
  - Prefer local variables
  - One responsibility per script
  - Comments only when the logic would not be obvious to a reader

Respond with valid JSON matching the schema in the user message — nothing else."""


def _tool_lines(task: dict, typed: bool, target: str = "luau") -> list[str]:
    lines = []
    py_sigs = pytypes.tool_signatures(task) if target == "python" and typed else {}
    for tool in task.get("tools", []):
        budget = (
            f" (at most {tool['max_calls']} calls per execution)" if "max_calls" in tool else ""
        )
        if py_sigs:
            lines.append(f"  - {py_sigs[tool['name']]}{budget}")
            lines.append(f"      # {_py_doc(tool['description'])}")
        elif typed:
            lines.append(f"  - tools.{tool['name']}: {tool['signature']}{budget}")
            lines.append(f"      -- {tool['description']}")
        else:
            lines.append(f"  - tools.{tool['name']}(args){budget} -- {tool['description']}")
    return lines


def _py_doc(text: str) -> str:
    """Tool docs are written with Lua table literals; show them as Python dicts."""
    return (text.replace("{ok = true", '{"ok": True').replace("{ok = false", '{"ok": False')
            .replace("error = <code>", '"error": <code>').replace("message = <text>", '"message": <text>'))


def generation_user(task: dict, target: str, variant: str) -> str:
    typed = target in ("luau", "python") and variant == "typed"
    parts = [f"GOAL: {task['goal']}"]
    parts.append(f"INPUT SCHEMA:\n{json.dumps(task['input_schema'], indent=2)}")
    parts.append(
        "OUTPUT SCHEMA (return value must conform exactly):\n"
        f"{json.dumps(task['output_schema'], indent=2)}"
    )

    if typed and target == "python":
        parts.append(
            "PREDECLARED TYPES (already in scope — use them, do NOT redeclare or import them):\n"
            f"{pytypes.prompt_types(task)}\n\n"
            "Names from `typing` (Any, Literal…) are NOT in scope: import them if you use them.\n"
            "Annotate the entry point exactly as: def run(input: Input, context: Context) -> Output:"
        )
    elif typed:
        parts.append(
            "PREDECLARED TYPES (already in scope — use them, do NOT redeclare them):\n"
            f"{task_types(task)}\n\n"
            "Annotate the entry point exactly as: function run(input: Input, context: Context): Output"
        )

    tool_lines = _tool_lines(task, typed, target)
    if tool_lines:
        parts.append("AVAILABLE TOOLS (only these — nothing else):\n" + "\n".join(tool_lines))
    else:
        parts.append("AVAILABLE TOOLS: none — pure computation only (no I/O)")

    parts.append(f"\nRespond with JSON matching this schema exactly:\n{RESPONSE_SCHEMA[target]}")
    return "\n\n".join(parts)


def repair_system(target: str) -> str:
    if target == "python":
        return f"""\
You are a Python script repair specialist for RiteSmith.
Your sole job: fix the listed validation errors while preserving the original logic.

REPAIR RULES
- Keep the function signature: run(input, context)
- Do NOT add new features or change the script's purpose
- Fix ONLY the problems listed — do not refactor unrelated code
- Forbidden stays forbidden: system imports, open, eval, exec, __import__, globals
- Do NOT raise and do NOT use assert
- Return a dict — never None, never a bare primitive

{_LANGUAGE["python"]}

Respond with valid JSON matching the schema in the user message — nothing else."""
    lang = LANG_NAME[target]
    extra = (
        "\n- The script must remain valid Luau (see the language notes)" if target == "luau" else ""
    )
    return f"""\
You are a {lang} script repair specialist for RiteSmith.
Your sole job: fix the listed validation errors while preserving the original logic.

REPAIR RULES
- Keep the function signature: run(input, context)
- Do NOT add new features or change the script's purpose
- Fix ONLY the problems listed — do not refactor unrelated code
- Forbidden tokens remain forbidden: {_FORBIDDEN[target]}
- Do NOT call error() or assert()
- Return a table — never nil, never a bare primitive{extra}

{_LANGUAGE[target]}

Respond with valid JSON matching the schema in the user message — nothing else."""


def repair_user(
    task: dict, script: str, errors: list[str], attempt: int, target: str, variant: str
) -> str:
    errors_text = "\n".join(f"  [{i + 1}] {e}" for i, e in enumerate(errors))
    context = generation_user(task, target, variant).split("\nRespond with JSON")[0]
    return f"""\
ORIGINAL REQUEST:
{context}

REPAIR ATTEMPT: {attempt}

SCRIPT WITH ERRORS:
```{target}
{script}
```

VALIDATION ERRORS TO FIX:
{errors_text}

Fix the script and respond with JSON matching this schema:
{REPAIR_SCHEMA[target]}"""
