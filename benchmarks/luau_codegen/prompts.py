"""Generation/repair prompts for the benchmark.

Derived from ritesmith/llm/prompts.py (lua_generation_system / lua_repair_*),
adapted to the `tools.ns.fn({...})` ABI proposed for LunarDyson. Lua and Luau
share the same text except for the LANGUAGE section, so the comparison
isolates the language rather than the prompt.
"""

import json

from benchmarks.luau_codegen.preamble import task_types

LANG_NAME = {"lua": "Lua", "luau": "Luau"}

_FORBIDDEN = {
    "lua": "require, os, io, debug, load, loadfile, dofile,\n  rawget, rawset, package, collectgarbage, newproxy",
    "luau": "require, os, io, debug, load, loadfile, dofile,\n  rawget, rawset, package, collectgarbage, newproxy,\n  getfenv, setfenv, loadstring",
}

_LANGUAGE = {
    "lua": """\
LANGUAGE: standard Lua 5.4
  - Only the standard string, table and math libraries are available""",
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


def generation_system(target: str) -> str:
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


def _tool_lines(task: dict, typed: bool) -> list[str]:
    lines = []
    for tool in task.get("tools", []):
        budget = (
            f" (at most {tool['max_calls']} calls per execution)" if "max_calls" in tool else ""
        )
        if typed:
            lines.append(f"  - tools.{tool['name']}: {tool['signature']}{budget}")
            lines.append(f"      -- {tool['description']}")
        else:
            lines.append(f"  - tools.{tool['name']}(args){budget} -- {tool['description']}")
    return lines


def generation_user(task: dict, target: str, variant: str) -> str:
    typed = target == "luau" and variant == "typed"
    parts = [f"GOAL: {task['goal']}"]
    parts.append(f"INPUT SCHEMA:\n{json.dumps(task['input_schema'], indent=2)}")
    parts.append(
        "OUTPUT SCHEMA (return value must conform exactly):\n"
        f"{json.dumps(task['output_schema'], indent=2)}"
    )

    if typed:
        parts.append(
            "PREDECLARED TYPES (already in scope — use them, do NOT redeclare them):\n"
            f"{task_types(task)}\n\n"
            "Annotate the entry point exactly as: function run(input: Input, context: Context): Output"
        )

    tool_lines = _tool_lines(task, typed)
    if tool_lines:
        parts.append("AVAILABLE TOOLS (only these — nothing else):\n" + "\n".join(tool_lines))
    else:
        parts.append("AVAILABLE TOOLS: none — pure computation only (no I/O)")

    parts.append(f"\nRespond with JSON matching this schema exactly:\n{RESPONSE_SCHEMA[target]}")
    return "\n\n".join(parts)


def repair_system(target: str) -> str:
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
