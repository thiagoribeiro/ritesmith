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

HOME DEVICE CONTROL (only when profile = trusted_internal)
  casp.query(resource_type: string, filters: table, capability: string) -> table
    -- Returns {resources: [{id, type, displayName, metadata}]} or {error, message}
    -- Use when user refers to a room or area: filters = {room="cozinha"}
    -- resource_type: "smart-switch" | "presence-sensor" | "ac-unit"
    -- capability: "turn-on" | "turn-off" | "state" | "check"

  casp.resolve(resource_type: string, capability: string, hint: string) -> table
    -- Returns {status="resolved", resource={id,type,displayName}} or {status="ambiguous", candidates=[...]}
    -- Use when user names a specific device: hint = "led do painel"

  casp.execute(resource_id: string, resource_type: string, capability: string, input: table) -> table
    -- Returns {status="ok"} or {status="failed", errorCode, error}
    -- resource_id comes from casp.query or casp.resolve

CASP SELECTION RULES
  - casp.query when user says a room/area ("cozinha", "sala", "varanda")
  - casp.resolve when user names a specific device ("led do painel", "ventilador da suite")
  - Always check result.status or result.error before calling casp.execute
  - For query: iterate result.resources and call casp.execute for each
  - For resolve: only call casp.execute if result.status == "resolved"

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
  - requires_side_effects = true if the script controls physical devices, sends messages, or mutates external state
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
        "field": "{{ payload.field }}"
      }
    },
    "successStatusCodes": [200, 201]
  },
  "compensation": {             // optional — only if the call has a rollback
    "url": "https://api.example.com/resource/rollback",
    "verb": "POST",
    "body": { "id": "{{ payload.id }}" }
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
      "when": { "==": [{ "var": "payload.type" }, "premium"] },
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
      "orderId": "{{ payload.orderId }}",
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
  payload.<field>                    — workflow input data (e.g. payload.threshold)
  nodes.<nodeId>.response.body.<f>   — LATEST result of node <nodeId> across ALL iterations
  nodes.<nodeId>.request.body.<f>    — request body sent to node <nodeId>
  step.<N>.body.<field>              — result of the Nth completed task (N as string: "0","1",...)
  step.<name>.body.<field>           — same as nodes.<name>.response.body.<field>
  steps                              — ordered list of all historical step results
  prev.body.<field>                  — last completed step's output
  callback.body.<field>              — payload of an incoming async callback
  runtime.callback.url               — auto-generated callback URL (async mode only)
  runtime.callback.token             — HMAC-secured callback token (async mode only)

JSON-LOGIC  (used in switch.cases[].when, callback.successWhen, callback.failureWhen)
--------------------------------------------------------------------------------------
Use standard json-logic operators: ==, !=, >, <, >=, <=, and, or, !, in, var
Example: { "==": [{ "var": "payload.status" }, "active"] }
In switch conditions, nodes.<id>.response.body.<f> is also available via var.

STATE PERSISTENCE AND CROSS-NODE DATA FLOW
-------------------------------------------
Step results propagate to all subsequent nodes within the same execution — no sleep
required between nodes for data wiring. {{ nodes.X.response.body.* }} in node Y
correctly reflects X's output as long as X ran before Y in the execution graph.

Sleep nodes serve ONE purpose: introducing real time delays between actions (e.g.
"wait 5 minutes then check again"). They are NOT needed for data passing.

For LOOPS: after a sleep, the next iteration starts fresh. A node can reference its
OWN previous iteration's output via nodes.itself.response.body.* — this resolves to
the result stored from the PREVIOUS iteration (null on first run). This is the
"self-referential state" pattern for carrying aggregate state across iterations.

PATTERN A — SELF-REFERENTIAL LOOP  (aggregate tracking, condition-based termination)
-------------------------------------------------------------------------------------
Use when: "monitor every N minutes", "poll until condition", "track running min/max",
"check up to N times". Do NOT generate a single-pass workflow for these intents.

REQUIRED: add "max_iterations": <int> at the workflow root level so RiteSmith's
validator allows the back-edge. Trama itself does NOT use this value for anything.

Trama has a native SLEEP node kind:
  {"id": "<id>", "kind": "sleep", "durationSeconds": <N>, "next": "<next_node_id>"}
Use this — do NOT fake a sleep with a task calling sleep.internal.

Structure:
  1. fetch_node  — task: retrieves current value via capability
  2. track_node  — task (Lua artifact): receives current value from fetch_node AND reads
                   its OWN prior output (nodes.track_node.response.body.*) for prev state;
                   computes new aggregate; returns { ..., iteration: N }
  3. check_node  — switch: reads CURRENT iteration's track_node result
       case "done":  target → send_result  (N reached or business condition met)
       default:      sleep_node
  4. send_result — task: sends notification, references track_node's current result
  5. sleep_node  — kind: "sleep"; next → fetch_node  ← intentional loop back-edge

COMPLETE EXAMPLE — ETH price monitor (track minimum, check every 5 min, 6 times):
{
  "name": "eth_price_monitor",
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
          "body": { "capability_name": "crypto.eth_price", "input": {} }
        },
        "successStatusCodes": [200]
      },
      "next": "track_min"
    },
    {
      "id": "track_min",
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
            "artifact_id": "<lua_tracker_artifact_id>",
            "input": {
              "current_price": "{{ nodes.fetch_price.response.body.output.price }}",
              "prev_min":      "{{ nodes.track_min.response.body.output.min_price }}",
              "prev_iter":     "{{ nodes.track_min.response.body.output.iteration }}"
            }
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
          "name": "done",
          "when": { ">=": [{ "var": "nodes.track_min.response.body.output.iteration" }, 6] },
          "target": "send_result"
        }
      ],
      "default": "sleep"
    },
    {
      "id": "send_result",
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
              "text": "ETH monitor done (6 checks). Min: ${{ nodes.track_min.response.body.output.min_price }}"
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

The Lua tracker artifact (track_min) handles null prev state on first iteration:
  function run(input, context)
    local price     = tonumber(tostring(input.current_price)) or 0
    local prev_min  = tonumber(tostring(input.prev_min  or ""))
    local prev_iter = tonumber(tostring(input.prev_iter or 0)) or 0
    local new_min   = prev_min and math.min(prev_min, price) or price
    return { min_price = new_min, iteration = prev_iter + 1, last_price = price }
  end

PATTERN B — LINEAR MULTI-FETCH  (collect exactly N samples, then compute)
--------------------------------------------------------------------------
Use when: "sample every N minutes for M minutes" with a fixed count, or "fetch from
N sources and aggregate". Simpler than Pattern A — no loops, no back-edges.

Structure:
  fetch_1 → sleep_1 → fetch_2 → sleep_2 → ... → fetch_N → compute → notify → end

The compute node references nodes.fetch_1.response.body.output.<field> through
nodes.fetch_N.*. No max_iterations needed. No self-referential state needed.

WIRING ANTI-PATTERNS TO AVOID
------------------------------
- Do NOT use "execution.input.*" — the correct variable is "payload.*"
- Do NOT use "execution.loop_count" — this variable does not exist in Trama
- Do NOT put HTTP calls inside switch cases — the switch chooses a target node,
  and THAT node performs the call.
- Do NOT make a switch node the entrypoint unless you have no initial task.
- Do NOT create back-edges (loops) without "max_iterations" at root — the validator
  will reject with "Cycle detected".
- Do NOT leave "next" pointing to a non-existent node id.
- Every switch MUST have a "default" — never rely on all cases being exhaustive.
- The last task node in every non-looping path MUST have "next": "end".
- For polling loops (Pattern A), the sleep node MUST point back to the fetch node.
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
- Use "{{{{ payload.<field> }}}}" to pass workflow input into request bodies
- Use "{{{{ nodes.<id>.response.body.<field> }}}}" to wire outputs between nodes
- Add "compensation" only when the HTTP call creates state that can be rolled back
- Keep the definition focused: one purpose, minimum necessary nodes
- For monitoring/polling intents: ALWAYS use Pattern A (self-referential loop) or Pattern B
  (linear multi-fetch); add "max_iterations" at root for Pattern A to allow the back-edge

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
