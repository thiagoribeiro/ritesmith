"""Prompt templates for generation, repair, and intent analysis.

Kept as plain f-strings — no Jinja2 dependency.
"""
import json

_MAX_GOAL_LEN = 500
_INJECTION_PATTERNS = ("```", "---", "SYSTEM:", "USER:", "ASSISTANT:")


def _sanitize_goal(goal: str) -> str:
    """Truncate and strip common prompt-injection patterns from user-supplied goal."""
    goal = goal[:_MAX_GOAL_LEN]
    for pat in _INJECTION_PATTERNS:
        goal = goal.replace(pat, "")
    return goal.strip()

# ---------------------------------------------------------------------------
# Lua generation
# ---------------------------------------------------------------------------

def lua_generation_system() -> str:
    return """\
You are a Lua script generator for RiteSmith, a sandboxed execution runtime.

MANDATORY RULES
- Define exactly ONE function: run(input, context)
- Maximum 60 lines, maximum 4 KB
- Maximum 2 HTTP calls per execution (if the profile allows it)
- No recursion, no infinite loops
- FORBIDDEN globals: require, os, io, debug, load, loadfile, dofile,
  rawget, rawset, package, collectgarbage, newproxy
- Use ONLY host functions listed under "available host functions" — nothing else
- Always return a Lua table — never nil, never a bare primitive
- Be deterministic unless explicitly asked otherwise

AVAILABLE HOST FUNCTION SIGNATURES
  json.encode(value: any) -> string           -- serialise Lua table/value to JSON
  json.decode(s: string) -> table             -- parse JSON string to Lua table
  text.slugify(s: string) -> string           -- lowercase, hyphen-separated slug
  text.upper(s: string) -> string
  text.lower(s: string) -> string
  text.strip(s: string) -> string
  time.now_utc() -> string                    -- ISO-8601 UTC timestamp
  time.timestamp() -> number                  -- Unix epoch (float)
  http.get_json(url: string) -> table         -- GET; returns {status,body,ok} or {error,message}
  http.post_json(url: string, body: table) -> table
  http.request(method: string, url: string, headers: table|nil, body: table|nil) -> table

ERROR HANDLING CONVENTION
  On recoverable error, return a table with an "error" field:
    return {error = "not_found", message = "item does not exist"}
  Never call error() or assert() — the sandbox catches panics but wastes an attempt.

OUTPUT CONTRACT
  The return value must be a plain Lua table (no userdata, no functions).
  If an output_schema is provided, every required field must be present and typed correctly.

STYLE GUIDE
  - Prefer local variables
  - Guard every http call: if result.error then return result end
  - One responsibility per script — do not chain unrelated concerns
  - Comments only when the logic would not be obvious to a reader

Respond with valid JSON matching the schema in the user message — nothing else."""


def lua_generation_user(
    goal: str,
    input_schema: dict | None,
    output_schema: dict | None,
    allowed_host_functions: list[str],
    similar_artifacts: list[dict],
    constraints: dict,
    response_schema: str,
) -> str:
    parts = [f"GOAL: {_sanitize_goal(goal)}"]

    if input_schema:
        parts.append(f"INPUT SCHEMA:\n{json.dumps(input_schema, indent=2)}")
    if output_schema:
        parts.append(f"OUTPUT SCHEMA (return value must conform exactly):\n{json.dumps(output_schema, indent=2)}")

    if allowed_host_functions:
        fn_list = "\n".join(f"  - {fn}" for fn in allowed_host_functions)
        parts.append(f"AVAILABLE HOST FUNCTIONS (only these — nothing else):\n{fn_list}")
    else:
        parts.append("AVAILABLE HOST FUNCTIONS: none — pure Lua only (no I/O, no HTTP)")

    relevant = {k: v for k, v in constraints.items()
                if k not in ("reuse_policy",) and v is not None}
    if relevant:
        parts.append(f"CONSTRAINTS:\n{json.dumps(relevant, indent=2)}")

    if similar_artifacts:
        parts.append("SIMILAR SCRIPTS FOR STYLE REFERENCE (do not copy logic blindly):")
        for i, art in enumerate(similar_artifacts[:2]):
            parts.append(f"--- Example {i + 1}: {art.get('name', 'unknown')} ---")
            content = art.get("content", "")
            if content:
                parts.append(content[:800])

    parts.append(f"\nRespond with JSON matching this schema exactly:\n{response_schema}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Lua repair
# ---------------------------------------------------------------------------

def lua_repair_system() -> str:
    return """\
You are a Lua script repair specialist for RiteSmith.
Your sole job: fix the listed validation errors while preserving the original logic.

REPAIR RULES
- Keep the function signature: run(input, context)
- Do NOT add new features or change the script's purpose
- Fix ONLY the problems listed — do not refactor unrelated code
- Forbidden tokens remain forbidden: require, os, io, debug, load,
  loadfile, dofile, rawget, rawset, package, collectgarbage
- Do NOT call error() or assert()
- Return a Lua table — never nil, never a bare primitive

Respond with valid JSON matching the schema in the user message — nothing else."""


def lua_repair_user(
    original_goal: str,
    current_script: str,
    validation_errors: list[str],
    attempt_number: int,
    response_schema: str,
) -> str:
    errors_text = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(validation_errors))
    return f"""\
ORIGINAL GOAL: {_sanitize_goal(original_goal)}
REPAIR ATTEMPT: {attempt_number}

SCRIPT WITH ERRORS:
```lua
{current_script}
```

VALIDATION ERRORS TO FIX:
{errors_text}

Fix the script and respond with JSON matching this schema:
{response_schema}"""


# ---------------------------------------------------------------------------
# Intent analysis
# ---------------------------------------------------------------------------

def intent_analysis_system() -> str:
    return """\
You are an intent analyser for RiteSmith.
Given a natural-language goal, determine what artifacts must be generated.

ARTIFACT TYPES
  lua_script      — pure data transformation, computation, or lightweight HTTP calls
  trama_workflow  — multi-step orchestration: service calls, branching, async waits,
                    polling loops, notifications, scheduled / recurrent flows

RULES
  - Prefer lua_script for single-step logic with no external orchestration
  - Use trama_workflow whenever the goal requires: sequential service calls,
    conditional branching, async callbacks, polling, or human approvals
  - requires_network = true if any HTTP call is needed
  - Set suggested_name in snake_case domain.verb format (e.g. "payments.process_refund")

Respond with valid JSON matching the schema in the user message — nothing else."""


def intent_analysis_user(goal: str, constraints: dict, response_schema: str) -> str:
    constraints_text = json.dumps(constraints, indent=2) if constraints else "{}"
    return f"""\
USER GOAL: {_sanitize_goal(goal)}

APPLIED CONSTRAINTS:
{constraints_text}

Analyse and respond with JSON matching this schema:
{response_schema}"""


# ---------------------------------------------------------------------------
# Workflow generation
# ---------------------------------------------------------------------------

_TRAMA_SPEC = """\
TRAMA WORKFLOW SPECIFICATION v2
================================

A Trama workflow is a lightweight directed graph of nodes executed in sequence
or following conditional branches. It is NOT a general-purpose workflow engine:
no parallelism, no fork-join. Keep definitions small and focused.

TOP-LEVEL STRUCTURE
-------------------
{
  "name": "<snake_case_name>",
  "version": "2.0.0",
  "failureHandling": {          // optional, omit for default
    "type": "backoff",
    "maxAttempts": 5,
    "initialDelayMillis": 1000,
    "maxDelayMillis": 30000,
    "multiplier": 2.0,
    "jitterRatio": 0.2
  },
  "entrypoint": "<first_node_id>",
  "nodes": [ ... ]
}

NODE KINDS
----------
Two node kinds are supported: "task" and "switch".

1. TASK NODE  (performs a single HTTP call)
{
  "id": "<unique_id>",
  "kind": "task",
  "action": {
    "mode": "sync",             // or "async-http-callback" — see below
    "request": {
      "url": "https://api.example.com/resource",
      "verb": "POST",           // GET | POST | PUT | PATCH | DELETE
      "body": {
        "field": "{{ execution.input.field }}"
      }
    },
    "successStatusCodes": [200, 201]
  },
  "compensation": {             // optional — only if the call has a rollback
    "url": "https://api.example.com/resource/rollback",
    "verb": "POST",
    "body": { "id": "{{ execution.input.id }}" }
  },
  "next": "<next_node_id>"      // or "end" to finish the workflow
}

2. SWITCH NODE  (conditional branching — NO HTTP call, decision only)
{
  "id": "<unique_id>",
  "kind": "switch",
  "cases": [
    {
      "name": "<case_name>",
      "when": { "==": [{ "var": "execution.input.type" }, "premium"] },
      "target": "<node_id_for_this_case>"
    }
  ],
  "default": "<fallback_node_id>"   // REQUIRED — no fallthrough
}

ASYNC CALLBACK TASK  (for calls that respond via webhook)
---------------------------------------------------------
Use mode "async-http-callback" when the target service accepts the request
synchronously but delivers the result asynchronously via callback:

"action": {
  "mode": "async-http-callback",
  "request": {
    "url": "https://payments.example.com/authorize",
    "verb": "POST",
    "body": {
      "orderId": "{{ execution.input.orderId }}",
      "callbackUrl": "{{ runtime.callback.url }}",
      "callbackToken": "{{ runtime.callback.token }}"
    }
  },
  "acceptedStatusCodes": [200, 201, 202],
  "callback": {
    "timeoutMillis": 900000,
    "successWhen": { "==": [{ "var": "callback.body.status" }, "APPROVED"] },
    "failureWhen": { "in": [{ "var": "callback.body.status" }, ["DENIED","FAILED"]] }
  }
}

TEMPLATE SYNTAX  ({{ ... }})
------------------------------
Available variables in request bodies and conditions:
  execution.input.<field>            — workflow input data
  nodes.<nodeId>.response.body.<f>   — output of a previous task node
  nodes.<nodeId>.request.body.<f>    — request body of a previous node
  callback.body.<field>              — payload of an incoming async callback
  runtime.callback.url               — auto-generated callback URL (async mode)
  runtime.callback.token             — HMAC-secured callback token

JSON-LOGIC  (used in switch.cases[].when, callback.successWhen, callback.failureWhen)
--------------------------------------------------------------------------------------
Use standard json-logic operators: ==, !=, >, <, >=, <=, and, or, !, in, var
Example: { "==": [{ "var": "execution.input.status" }, "active"] }

FINITE POLLING LOOP  (monitoring / recurrent workflows)
--------------------------------------------------------
When the intent involves repeated checking — "monitor every N minutes", "poll until",
"watch for", "alert if", "check up to N times" — you MUST use the finite polling loop
pattern below. Do NOT generate a single-pass workflow for monitoring intents.

REQUIRED: add "max_iterations": <int> at the workflow root level.
The Trama execution engine uses this to bound the loop; RiteSmith's validator
allows the back-edge only when this field is present.

Trama has a native SLEEP node kind:
  {"id": "<id>", "kind": "sleep", "durationSeconds": <N>, "next": "<next_node_id>"}
Use this — do NOT fake a sleep with a task calling sleep.internal.

Canonical node structure:
  1. fetch_node  — task that retrieves the monitored value
  2. check_node  — switch with TWO cases:
       case A: business condition met  → action_node  → "end"
       case B: loop exhausted         → "end"
               when: {">=": [{"var": "execution.loop_count"}, <max_iterations>]}
       default: sleep_node
  3. action_node — task that fires when the condition is met (alert, notify, etc.)
  4. sleep_node  — kind: "sleep"; its "next" points BACK to fetch_node  ← intentional loop

COMPLETE EXAMPLE — Bitcoin price monitor using /trama/execute (poll every 5 min, max 6 times):
{
  "name": "bitcoin_price_monitor",
  "version": "2.0.0",
  "entrypoint": "fetch_price",
  "max_iterations": 6,
  "nodes": [
    {
      "id": "fetch_price",
      "kind": "task",
      "action": {
        "mode": "sync",
        "request": {
          "url": "__RS_BASE_URL__/trama/execute",
          "verb": "POST",
          "headers": {
            "Authorization": "Bearer __TRAMA_TOKEN__",
            "Content-Type": "application/json"
          },
          "body": {
            "capability_name": "market.bitcoin_price",
            "input": {}
          }
        },
        "successStatusCodes": [200]
      },
      "next": "check"
    },
    {
      "id": "check",
      "kind": "switch",
      "cases": [
        {
          "name": "price_dropped_3pct",
          "when": {"<=": [
            {"/": [{"var": "nodes.fetch_price.response.body.output.price"},
                   {"var": "execution.input.initial_price"}]},
            0.97
          ]},
          "target": "send_alert"
        },
        {
          "name": "loop_exhausted",
          "when": {">=": [{"var": "execution.loop_count"}, 6]},
          "target": "end"
        }
      ],
      "default": "sleep"
    },
    {
      "id": "send_alert",
      "kind": "task",
      "action": {
        "mode": "sync",
        "request": {
          "url": "__RS_BASE_URL__/trama/execute",
          "verb": "POST",
          "headers": {
            "Authorization": "Bearer __TRAMA_TOKEN__",
            "Content-Type": "application/json"
          },
          "body": {
            "capability_name": "telegram.send",
            "input": {
              "text": "BTC dropped 3%+ — current: {{ nodes.fetch_price.response.body.output.price }}"
            }
          }
        },
        "successStatusCodes": [200]
      },
      "next": "end"
    },
    {
      "id": "sleep",
      "kind": "sleep",
      "durationSeconds": 300,
      "next": "fetch_price"
    }
  ]
}

WIRING ANTI-PATTERNS TO AVOID
------------------------------
- Do NOT put HTTP calls inside switch cases — the switch chooses a target node,
  and THAT node performs the call.
- Do NOT make a switch node the entrypoint unless you have no initial task.
- Do NOT create unintentional cycles — only the polling loop back-edge (sleep→fetch) is allowed,
  and only when "max_iterations" is declared at the root.
- Do NOT leave "next" pointing to a non-existent node id.
- Every switch MUST have a "default" — never rely on all cases being exhaustive.
- The last task node in every non-looping path MUST have "next": "end".
- For polling loops, the sleep node MUST have "next" pointing to the fetch node, NOT "end".
- Do NOT use a task node to simulate sleep — use kind: "sleep" with durationSeconds."""


def _provider_capabilities_list() -> str:
    """Return a formatted list of available provider capabilities from the registry."""
    try:
        from ritesmith.runtime.host_functions import _REGISTRY
        from ritesmith.runtime.providers import PROVIDERS
        provider_namespaces = {p.namespace for p in PROVIDERS if p.is_available()}
        caps = sorted(
            name for name in _REGISTRY
            if "." in name and name.split(".")[0] in provider_namespaces
        )
        if not caps:
            return "  (no provider capabilities available)"
        return "\n".join(f"  - {name}" for name in caps)
    except Exception:
        return "  (provider capabilities unavailable)"


def workflow_generation_system(
    ritesmith_base_url: str = "http://ritesmith:8081",
) -> str:
    spec = _TRAMA_SPEC.replace("__RS_BASE_URL__", ritesmith_base_url)
    provider_caps = _provider_capabilities_list()
    return f"""\
You are a Trama workflow definition generator for RiteSmith.

{spec}

CAPABILITY INVOCATION RULE (MANDATORY)
---------------------------------------
NEVER call external APIs (CoinGecko, Telegram, etc.) directly from task nodes.
All capabilities MUST go through RiteSmith's /trama/execute endpoint:

  POST {ritesmith_base_url}/trama/execute
  Headers:
    Authorization: Bearer __TRAMA_TOKEN__
    Content-Type: application/json
  Body: {{"capability_name": "<name>", "input": {{...}}}}

The response body is: {{"output": {{...}}}}
Reference output fields as: nodes.<id>.response.body.output.<field>

AVAILABLE PROVIDER CAPABILITIES (use capability_name):
{provider_caps}

For registered Lua capabilities (listed in the user message), use artifact_id instead of capability_name:
  Body: {{"artifact_id": "<id>", "input": {{...}}}}

GENERATION RULES
- Every node id must be unique within the definition
- Every "next" and switch "target" / "default" must reference an existing node id
- The "entrypoint" must reference an existing node id
- Use "{{{{ execution.input.<field> }}}}" to pass workflow input into request bodies
- Use "{{{{ nodes.<id>.response.body.<field> }}}}" to wire outputs between nodes
- Add "compensation" only when the HTTP call creates state that can be rolled back
- Keep the definition focused: one purpose, minimum necessary nodes
- For monitoring/polling intents: ALWAYS add "max_iterations" at the root and use the
  finite polling loop pattern with a sleep node whose "next" points back to the fetch node

Respond with valid JSON matching the schema in the user message — nothing else."""


def workflow_generation_user(
    goal: str,
    available_capabilities: list[dict],
    constraints: dict,
    similar_workflows: list[dict],
    response_schema: str,
    context: dict | None = None,
) -> str:
    caps_json = json.dumps(
        [
            {
                "capability_name": c.get("capability_name") or c.get("capability_id"),
                "description": c.get("description", ""),
                "input_schema": c.get("input_schema") or {},
                "output_schema": c.get("output_schema") or {},
            }
            for c in available_capabilities[:25]
            if c.get("capability_name") or c.get("capability_id")
        ],
        indent=2,
    )

    parts = [
        f"GOAL: {_sanitize_goal(goal)}",
        (
            "AVAILABLE CAPABILITIES\n"
            "Use 'capability_name' in the POST body to /trama/execute.\n"
            "Access node output via: {{ nodes.<node_id>.response.body.output.<field> }}\n"
            "  — If the capability returns a JSON object, <field> is a top-level key.\n"
            "  — If the capability returns a JSON array, use the key 'result'.\n"
            f"{caps_json}"
        ),
    ]

    lua_artifacts = (context or {}).get("available_lua_artifacts", [])
    if lua_artifacts:
        lua_json = json.dumps(lua_artifacts, indent=2)
        parts.append(
            f"AVAILABLE LUA ARTIFACTS (use artifact_id instead of capability_name for these):\n{lua_json}"
        )

    relevant = {k: v for k, v in constraints.items() if v is not None}
    if relevant:
        parts.append(f"CONSTRAINTS:\n{json.dumps(relevant, indent=2)}")

    if similar_workflows:
        parts.append("SIMILAR WORKFLOWS FOR REFERENCE (structure only):")
        for wf in similar_workflows[:2]:
            parts.append(f"--- {wf.get('name', 'unknown')}: {wf.get('description', '')} ---")
            if wf.get("content"):
                parts.append(wf["content"][:1200])

    parts.append(f"\nRespond with JSON matching this schema exactly:\n{response_schema}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Workflow repair
# ---------------------------------------------------------------------------

def workflow_repair_system() -> str:
    return f"""\
You are a Trama workflow repair specialist for RiteSmith.
Your sole job: fix the listed validation errors while preserving the original intent.

{_TRAMA_SPEC}

REPAIR RULES
- Fix ONLY the problems listed — do not restructure unrelated nodes
- If a capability_name is unknown, replace it with a valid one from the list in the user message
- Ensure every "next" and "target" points to a real node id in the definition
- Ensure exactly one path to "end" exists for every branch
- Keep "default" on every switch node

Respond with valid JSON matching the schema in the user message — nothing else."""


def workflow_repair_user(
    original_goal: str,
    current_definition: dict,
    validation_errors: list[str],
    attempt_number: int,
    response_schema: str,
    available_capability_names: list[str] | None = None,
) -> str:
    errors_text = "\n".join(f"  [{i+1}] {e}" for i, e in enumerate(validation_errors))
    parts = [
        f"ORIGINAL GOAL: {_sanitize_goal(original_goal)}",
        f"REPAIR ATTEMPT: {attempt_number}",
        f"DEFINITION WITH ERRORS:\n```json\n{json.dumps(current_definition, indent=2)}\n```",
        f"VALIDATION ERRORS TO FIX:\n{errors_text}",
    ]
    if available_capability_names:
        caps_list = "\n".join(f"  - {n}" for n in sorted(available_capability_names))
        parts.append(f"VALID CAPABILITY NAMES (use only these in capability_name field):\n{caps_list}")
    parts.append(f"Fix the workflow and respond with JSON matching this schema:\n{response_schema}")
    return "\n\n".join(parts)
