# RiteSmith

> The missing layer between LLM planning and deterministic execution.

<p align="center"><img src="assets/ritesmith.jpg" width="480"></p>

---

## The agent loop problem

Most LLM systems today follow the same pattern:

```
think → act → observe → think again
```

Over and over. The LLM is in the critical path for every step.

This loop exists because the system has no structure. The only way to move forward is to keep asking the model what to do next. This creates systems where:

- execution is non-deterministic — the same request can take a different path each time
- logic is recomputed on every call
- behavior drifts over time
- state lives in ephemeral prompt context
- there is no reliable audit trail
- there is no clear boundary between deciding and executing

This is not an AI problem. It's a systems problem.

---

## The shift

Instead of:

```
decide → execute → decide again
```

You need:

```
decide → define → execute
```

The LLM's role changes from **runtime controller** to **system generator**.

It is called once to produce a plan. Execution no longer depends on it.

Once the plan exists:
- behavior is consistent
- execution is cheap (no LLM cost per step)
- the system is auditable and reusable

The loop does not disappear; it moves. While RiteSmith **builds** the system, the model
works like a coding agent: it looks up contracts, inspects devices, validates its plan and
runs it in an offline simulator, then fixes what failed. That loop is bounded (turns,
tokens, a deadline) and runs once. The system it produces runs without it.

---

## Where RiteSmith fits

```
LLM / Agent  →  understands intent
RiteSmith    →  generates the system
Trama        →  executes it reliably
```

RiteSmith is the materialization layer.

Given an intent, it searches for existing artifacts, builds a verified recipe when the request matches one, and otherwise generates what is missing: a semantic plan that it compiles into a [Trama](https://trama.run) workflow, or a strictly typed Luau script executed by the embedded [LunarDyson](https://github.com/thiagoribeiro/lunardyson) runtime. Everything is validated under guardrails (closed capability contracts, type check, policy, simulation, sandbox tests), registered in a versioned artifact registry, and handed to Trama for durable execution.

The LLM is no longer controlling the system while it runs. It defines the system before it runs.

---

## Example: Bitcoin price monitor

**Intent:** "Tell me if Bitcoin drops 3% today."

Without RiteSmith, an agent loops: it calls the LLM and the market API at every check.

With RiteSmith there are three ways to get the workflow, from cheapest to most general:

**1. Reuse.** The registry already has a matching artifact: no generation at all.

**2. Recipe.** The caller (or the generation agent) recognizes a known shape and fills
its parameters. RiteSmith validates them against a closed model and emits the final Trama
nodes in code: no model call, under a second.

```json
POST /plans
{
  "intent": "Tell me if Bitcoin drops 3% today",
  "requested_artifact_types": ["trama_workflow"],
  "context": {
    "template": {
      "name": "price_variation",
      "params": {"kind": "crypto", "symbol": "BTC", "currency": "USD",
                 "percent": 3, "direction": "down",
                 "interval_seconds": 900, "window_seconds": 86400}
    }
  }
}
```

The recipe captures the base price on the first pass, checks every 15 minutes, notifies
once when the drop crosses 3%, and ends, either then or at the deadline.

**3. Semantic plan.** For anything without a recipe, the model writes a short plan:
calls, waits, conditions, parallel branches, bounded repeats. Inputs and outputs are
checked against each capability's closed contract.

```json
{
  "version": "workflow-plan-v1",
  "name": "btc_morning_digest",
  "steps": [
    {"kind": "repeat", "id": "daily", "interval_seconds": 86400, "count": 7,
     "steps": [
       {"kind": "call", "id": "price", "capability_name": "market.coin_price",
        "args": {"symbol": "btc", "currency": "usd"}},
       {"kind": "call", "id": "notify", "capability_name": "telegram.send",
        "args": {"text": {"type": "template",
                          "parts": ["BTC now: ", {"ref": "price", "path": "price"}]}}}
     ]}
  ]
}
```

RiteSmith compiles the plan into Trama nodes in code: the counters, joins, first-pass seeds,
continuation blocks and typed JSON references. The model never writes `switch`,
`max_iterations` or loop bookkeeping, which is where hand-written graphs used to break.
The result is registered and reused by later similar requests, and Trama runs it without
calling the LLM again.

---

## How generation works

`POST /plans` and `POST /generate` resolve a request in this order:

1. **Reuse.** Full-text recall over the registry, plus an LLM judge when a candidate is
   close. `reuse_policy: force_new` skips it.
2. **Recipe.** When the request matches a recipe in `context.template`, the recipe is
   built in code (see [Recipes](#recipes)).
3. **Generation.** `RITESMITH_GENERATION_MODE` selects one of two generators. Both
   return the same proposal, so the rest of the pipeline (certification, policy,
   approval, persistence) does not change:
   - `agent`: the tool-using loop described below. It is the better generator;
     see the measurements.
   - `proposal` (default): a single structured answer from the model, validated
     afterwards and repaired with one extra call when it fails.

### Agent mode

The model works like a coding agent with a bounded budget: it finds out what it needs, checks its own
work, and submits only what passes. The tools it gets are:

| Tool | What it does |
|---|---|
| `find_capabilities`, `get_contracts` | Searches the catalog and returns the closed input/output schemas, instead of sending the whole catalog in the prompt |
| `list_devices` | Lists home devices: alias, room, capabilities, a state example, IR buttons, virtual devices (people, trackers) |
| `try_capability` | Runs a **read-only** capability now (`grafana`, `web`, `market`, `runtime`, `stat`) to learn real metric names, API shapes or prices; at most 6 probes |
| `use_template` | Builds a recipe; the result is final |
| `validate_workflow` | Compiles a semantic plan and runs every pipeline validator |
| `simulate_workflow` | Runs the compiled workflow offline with a virtual clock and scripted responses, and reports calls per capability, elapsed time and termination |
| `check_script` | Type-checks a Luau script, applies the pipeline's static gates (profile tools, forbidden tokens, size) and runs it against the model's own cases |
| `submit` | Finishes with a workflow, a script, a recipe or a refusal; it re-validates and returns errors as a tool result |

Several rules are part of the prompt:
- Contracts are closed.
- Every loop has an explicit end.
- Each real event produces one notification, not one per polling pass.
- Deliverables go through their capability (a report through `report.write_html`, a
  message through `telegram.send`).
- Vague phrasing such as "tonight" gets a stated default, not a refusal.
- When a request matches several devices of the same kind and does not name one, the
  agent refuses and lists the options, because a wrong physical action is worse than a
  question.

Design choices:
- **Cache-friendly prompt.** The system prompt and tool definitions are identical on every
  request; only the request comes last. From the second turn on, about 84% of input
  tokens are cached.
- **Responses API** with `previous_response_id`, so each turn sends only the new tool
  results.
- **Bounded.** The loop is limited by `RITESMITH_GENERATION_AGENT_MAX_TURNS`, by output
  tokens per turn, and by `RITESMITH_GENERATION_DEADLINE_SECONDS`.
- **Isolated tools.** Validation, compilation, simulation and script checks run in a
  separate process with a memory limit and a timeout
  (`RITESMITH_GENERATION_AGENT_TOOL_MEMORY_MB`, `..._TOOL_TIMEOUT_SECONDS`). A runaway
  plan costs one tool error, not the server.
- **Transcript.** Every turn (tool, arguments, result, tokens, duration) is stored as an
  audit event. `GET /plans/{plan_id}/transcript` returns it, including for failed
  generations, whose 422 carries the `plan_id`.
- **Local time.** `RITESMITH_LOCAL_TIMEZONE` gives the model the current local time, so
  "until midnight" means the user's midnight.

### Measured

These are 23 requests from a private home-automation deployment: device actions,
monitors, presence, reports, computation, web lookups, and edge cases that must be
refused. Each was run once with the same server and capabilities. Checks are static: an
artifact was produced, it has the right type, it uses the required capabilities, and it
uses none of the forbidden ones. The judge is a separate LLM that scores 1–5 whether the
artifact does what was asked.

| | Proposal mode, gpt-5.5 | Agent mode, gpt-6.1-terra |
|---|---|---|
| Passed checks | 10 / 23 | **22 / 23** |
| Judge score (1–5) | 1.82 | **4.35** |
| Latency p50 / p90 | 55 s / 109 s | **21 s / 52 s** |
| Input tokens per request (cached) | 13.4k (13%) | 37.9k (84%) |
| Output tokens per request | 3.9k | **1.4k** |
| Cost for the 23 requests | US$ 3.73 | **US$ 0.34** |

The agent reads more tokens than the single-shot proposal, but most of them are cached,
and it writes far fewer. It also ran on a model that costs a fifth as much per token.

These results are not an independent evaluation:
- The agent's prompt rules were tuned on failures from this same subset.
- With gpt-6.2-sol instead of terra, the agent scored 21/23 and 3.65 for US$ 1.40.

Recipes are not in this table; they use no model at all.

The 0.2.1 proposal-mode scorecard (51 tasks, gpt-5-mini) is in
[Generation latency](#generation-latency).

---

## Recipes

A recipe is a verified workflow shape. The caller sends
`context.template = {"name": ..., "params": {...}}` with `POST /plans` (or
`POST /generate/trama-workflow`), and RiteSmith works through these steps:
1. It validates the params against a closed model, so unknown keys are rejected.
2. It emits the final Trama nodes in code.
3. It skips artifact reuse for that request.

Generation is deterministic, takes under a second and uses no tokens. In agent mode,
the model calls the same recipes through `use_template` when a request fits one.

Every polling recipe ends on its own, after its single action or at a wall-clock
deadline fixed when the plan is built. A missing, failed or malformed reading never
counts as a match.

| Recipe | Use | Main params |
|---|---|---|
| `threshold_action` | "When a device reading crosses a limit, act and/or notify once." Also covers boolean or text states such as motion, door or presence | `read{capability,target,input}`, `metric_path`, `op` (`<` `<=` `>` `>=` `==` `!=`), `value`, `action?`, `notify?` (`{value}` = the reading), `timeout_notify?`, `interval_seconds`, `window_seconds` (≤ 14 days), `start_delay_seconds` |
| `price_variation` | "Tell me if a coin or currency moves N%." The base price is captured on the first pass | `kind` (`crypto`/`fiat`), `symbol`, `currency`, `percent`, `direction` (`up`/`down`/`both`), `interval_seconds`, `window_seconds`, `label?` |
| `presence_change` | "When someone arrives, or everyone leaves, act and/or notify once" | `people[1..4]`, `event` (`any_home`/`all_away`), `require_transition` (default true: first see the opposite state, so "when X arrives" waits for a real arrival), `delay_seconds`, `actions[≤6]`, `notify?`, `interval_seconds`, `window_seconds` |
| `presence_reminder` | A one-shot text reminder on arrival (`context.presence_reminder` is still accepted) | `person_alias`, `message`, `channel: "telegram"` |

Device recipes read and act through `home.execute` (LoomHarbor), and notifications go
through `telegram.send`. The plan is refused if the required providers are not
configured.

---

## Why not just agent loops?

| | Agent loop | RiteSmith |
|---|---|---|
| LLM cost | every step | once, at generation (a recipe needs none) |
| Behavior | drifts across runs | consistent — same artifact executes |
| Audit trail | prompt context | versioned artifacts + execution history |
| Reuse | none — regenerated each time | registry search before generation |
| Execution | LLM-dependent at runtime | deterministic, LLM-free |
| Observability | implicit, hard to trace | Prometheus metrics + audit log |

---

## When NOT to use RiteSmith

- You need a general-purpose workflow engine → use [Trama](https://trama.run) directly
- You need a full agent framework → use LangGraph, CrewAI, etc.
- Your tasks are truly one-off and reuse adds no value
- You don't need auditability, versioning, or policy controls

---

## Core concepts

| Concept | What it is |
|---|---|
| **Capability** | A named, versioned, schema-validated script (`luau_script` by default, `lua_script` legacy) with input/output contracts, risk level, and side-effect declaration |
| **Artifact** | A stored capability or workflow definition — versioned, searchable, reusable |
| **Plan** | A proposed set of capabilities + workflow generated from an intent, with optional approval gate |
| **Semantic plan** | What the model writes for a workflow: calls, waits, conditions, parallel branches, bounded repeats. Compiled to Trama in code |
| **Recipe** | A verified workflow shape (threshold, price swing, presence change, arrival reminder) built from validated parameters without a model call |
| **Execution** | A durable record of running an artifact — status, input, output, duration, audit metadata |

---

## Tool providers

Providers are the single source of truth for both surfaces: Lua host functions (injected into the sandbox) and MCP tools (callable directly by the agent).

| Namespace | What it provides | Requires |
|---|---|---|
| `runtime` | current time, timezone | — |
| `web` | search (Brave/Exa), page fetch, JSON fetch | `RITESMITH_WEB_SEARCH_API_KEY` |
| `market` | BTC/coin prices, FX rates (CoinGecko) | — |
| `telegram` | send notifications | `RITESMITH_TELEGRAM_BOT_TOKEN` |
| `calendar` | Google Calendar read/write | `RITESMITH_GOOGLE_TOKEN_JSON` |
| `email` | Gmail search/read | `RITESMITH_GOOGLE_TOKEN_JSON` |
| `duckdb` | local SQL analytics (read-only) | `RITESMITH_DUCKDB_PATH` |
| `obsidian` | Obsidian vault search/read | `RITESMITH_OBSIDIAN_VAULT_PATH` |
| `report` | render/list HTML reports | `RITESMITH_REPORTS_PATH` |
| `grafana` | dashboards, Prometheus queries | `RITESMITH_GRAFANA_URL` |
| `casp` | device/resource control (query/resolve/execute) | `RITESMITH_CASP_PROVIDERS` |
| `home` | LoomHarbor device control, findtag zones | `RITESMITH_LOOMHARBOR_URL` |
| `stat` | running min/max, counters (pure) | — |
| `llm` | `llm.evaluate`: judge or summarize text inside a workflow | `OPENAI_API_KEY` |

Active providers are registered in the artifact registry at startup and returned by `GET /providers`.

---

## MCP server

The `mcp-server/` directory is a standalone MCP server that gives the agent two surfaces in one:

**Domain tools** — direct calls to provider implementations:
`web_search`, `web_fetch`, `market_bitcoin_price`, `market_exchange_rate`, `calendar_get_today`, `calendar_create_event`, `email_search`, `telegram_send`, `duckdb_query`, `obsidian_search`, …

**RiteSmith meta-tools** — delegate to the HTTP API:
`ritesmith_plan`, `ritesmith_generate`, `ritesmith_search_artifacts`, `ritesmith_execute`, `ritesmith_get_execution`, `ritesmith_list_capabilities`

The agent calls domain tools directly for fast, stateless lookups. It calls `ritesmith_plan` when the task needs generation, validation, registration, or durable execution. `ritesmith_generate` automatically decides whether to produce a script or a Trama workflow from the given intent.

Run via stdio (spawned as subprocess by the agent):

```bash
cd mcp-server && python server.py
```

---

## Architecture

```mermaid
flowchart LR
  U[User] --> A[Agent / LLM]
  A -->|intent| RS[RiteSmith API]
  A -->|MCP| MS[MCP Server]
  MS -->|domain tools| P[Tool Providers]
  MS -->|meta-tools| RS
  RS --> REG[Artifact Registry\nPostgreSQL · FTS]
  RS --> REC[Recipes\nbuilt in code]
  RS --> GEN[Generation\nagent loop or single proposal]
  GEN -->|tools| CHK[Contracts · devices · validate\nsimulate · check_script\nisolated process]
  GEN --> CMP[Semantic plan compiler]
  REC --> VAL[Certification + Policy]
  CMP --> VAL
  VAL --> REG
  REG --> LUA[Script Runtime\nLunarDyson/Luau · lupa/Lua legacy]
  REG --> TR[Trama\ndurable execution]
  TR -->|/trama/execute| RS
  RS --> P
```

---

## API reference

### Generate

```http
POST /generate
```

```http
POST /generate/lua
POST /generate/trama-workflow
```

`POST /generate` accepts `intent` + optional `save`, `context`, `constraints`, `input_schema`, `output_schema`. Runs intent analysis to decide whether to produce a script (Luau/Lua) or a Trama workflow, then runs the full generate → validate → repair → register loop and returns the validated artifact.

```json
{
  "intent": "calculate SHA-256 of a string",
  "save": true,
  "constraints": {"reuse_policy": "prefer_reuse", "allow_network": false}
}
```

`constraints.reuse_policy`: `prefer_reuse` (default) | `force_new`. All other fields are forwarded to the relevant generation service depending on artifact type.

### Plan

```http
POST /plans
POST /plans/{plan_id}/approve
POST /plans/{plan_id}/reject
POST /plans/{plan_id}/cancel
POST /plans/{plan_id}/complete
POST /plans/{plan_id}/artifacts
GET  /plans/{plan_id}
GET  /plans/{plan_id}/transcript
```

`POST /plans` is the high-level entry point. It accepts `intent`, plus optional
`requested_artifact_types`, `reuse_policy`, `constraints`, `context` and `callback_url`.
It searches, generates and validates, then returns a plan with its required capabilities
and workflow definition, optionally behind an approval gate. `context.template` selects a
[recipe](#recipes). In agent mode, `/transcript` returns every generation turn.

Every plan carries a `readback`, a plain-language description (pt-BR) of what its workflows
will do. It is derived from the graph, never from the intent:
- each effect: a message, a device action, a report;
- when it happens: at wall-clock times, or after which observed conditions (an arrival, a
  reading crossing a limit) and how often they are checked;
- when the plan gives up.

Show this text to the user as the confirmation. Repeating the request back can confirm a
plan that does something else.

**Intent verification** (`RITESMITH_INTENT_VERIFICATION=off|shadow|enforce`, default
`off`): before activation, a model compares the readback with the user's literal
messages, sent as `context.user_request` (a string or a list, oldest first). It does not
compare against `intent`, which is a caller's paraphrase and can carry the
misunderstanding. The verdict (`match`/`mismatch`/`unsure`/`error`, plus a reason) is
returned in `Plan.verification` and audited as `plan.intent_verified`.
- `shadow` only records the verdict.
- `enforce` blocks a `mismatch` with the reason. `unsure` passes, and the caller should ask
  the user.
- Errors fail open.

The model defaults to `RITESMITH_LLM_WORKFLOW_MODEL`, with
`RITESMITH_INTENT_VERIFICATION_EFFORT=low`. On a 26-case home-automation calibration set,
`gpt-6.1-terra` caught 14/14 deliberately wrong plans, including the misrouted reminder
that motivated it. Of 12 correct plans, it marked 10 `match`, 2 `unsure` and none
`mismatch`. Cost was about US$ 0.0015 and 3 s (p50) per check. That is one run of a
nondeterministic model, so watch it in `shadow` before you `enforce`.

### Artifacts

```http
GET  /artifacts?query=extract+invoice&artifact_type=lua_script
GET  /artifacts/{artifact_id}
GET  /artifacts/{artifact_id}/versions
GET  /artifacts/{artifact_id}/versions/{version}
POST /artifacts
POST /artifacts/{artifact_id}/versions/{version}/approve
```

`POST /artifacts/{id}/versions/{v}/approve` issues the execution `approval_token` for
that exact version (an HMAC the server verifies). An execution that policy gates on
approval only runs when given a matching token; an arbitrary string no longer works.

Full-text search via `query` parameter (PostgreSQL `tsvector`).

### Execute

```http
POST /executions
GET  /executions/{execution_id}
```

Runs a Lua capability directly or delegates a workflow to Trama.

```json
{
  "artifact_id": "art_01HX...",
  "input": {"text": "hello   world"},
  "idempotency_key": "req_abc123",
  "approval_token": "<from /artifacts/{id}/versions/{v}/approve, when policy requires it>"
}
```

### Trama execution bridge

```http
POST /trama/execute
```

Called by Trama workflow task nodes to invoke RiteSmith capabilities without calling external APIs directly. Requires `Authorization: Bearer <RITESMITH_TRAMA_TOKEN>`.

```json
{
  "capability_name": "market.bitcoin_price",
  "input": {"currency": "usd"}
}
```

Or for registered Lua artifacts:

```json
{
  "artifact_id": "art_01HX...",
  "input": {"value": 42}
}
```

Response: `{"output": {...}}`

### Other

```http
GET /capabilities
GET /capabilities/{capability_id}
GET /providers
GET /providers/{namespace}
POST /search
POST /validate
POST /policies/evaluate
POST /memory/search
GET /health
GET /metrics/
```

---

## Script contract

RiteSmith generates two script runtimes, selected by `RITESMITH_SCRIPT_LANGUAGE`
(`luau`, the default, or `lua`). New capabilities are `luau_script`; existing
`lua_script` artifacts keep running on the legacy lupa runtime unchanged. Both
expose a single `run` function and the same tool surface.

**Luau (default)** — executed by the embedded [LunarDyson](https://github.com/thiagoribeiro/lunardyson)
runtime. Tools and provider functions are called as `tools.<ns>.<fn>({ ... })`, and
generation injects typed signatures so the program is type-checked (`--!strict`)
before it is ever run:

```luau
function run(input: Input, context: Context): Output
  local p = tools.market.coin_price({ symbol = input.symbol })
  if p.error then return { error = p.error, message = p.message } end
  return { price = p.price }
end
```

**Lua (legacy)** — executed by the lupa sandbox, host functions as bare globals:

```lua
function run(input, context)
  return { result = "ok" }
end
```

Rules (both runtimes):
- `input` is validated against the input schema before execution; the return value
  against the output schema after
- unsafe stdlib modules are disabled; host functions are explicit and profile-controlled
- timeout: `RITESMITH_LUA_TIMEOUT_MS` (default 1000 ms); memory: `RITESMITH_LUA_MEMORY_LIMIT_MB` (default 32 MB)

Host functions depend on the sandbox profile (`transform_only`, `readonly_network`,
`notification`, `sensitive_personal`, `analytics_local`, `filesystem_write`,
`reporting`, `side_effects`, `trusted_internal`).

## Why Luau + LunarDyson

RiteSmith started on Lua (lupa). It moved to Luau, executed by a purpose-built
runtime (LunarDyson), for reasons specific to running LLM-generated code safely and
cheaply:

- **Type check before execution.** Luau is gradually typed. Generation feeds the
  tool signatures (derived from each provider's JSON Schema) into the prompt and into
  `luau-analyze`, so whole classes of bugs — using a tool result without checking its
  `ok`/`error`, a wrong field, a return that violates `output_schema` — are caught at
  generation time, before anything runs and without another LLM call. This matters
  because most production artifacts have no hand-written test cases; the type check is
  the semantic gate.
- **Real resource limits.** LunarDyson enforces a wall-clock/VM deadline that actually
  interrupts a runaway program, a memory budget via a custom allocator, and per-tool
  and per-effect-class budgets — things the in-process lupa sandbox could not enforce
  (its timeout could not kill a spinning thread).
- **Capability-first, default-deny.** A generated program starts with no I/O. It can
  only reach the outside world through the `tools.*` a host registered; a tool that was
  not registered does not exist in the program's environment.
- **A stable, pinned language.** Luau is pinned to one upstream version, so the
  language the saved artifacts are written in does not drift when a dependency moves.
  (The lupa path had silently become Lua 5.5.)
- **A benchmark decided it.** A 450-sample run showed the production model generates
  Luau that passes tests at the same rate as Lua (100% vs 100% on the primary model),
  so the move cost nothing in generation quality while adding the guarantees above.

LunarDyson is a separate, generic runtime (capability-first execution for untrusted
Luau programs); RiteSmith is its first consumer. Not in production yet: the host-side
sandbox/auth hardening tracked in the roadmap.

---

## Workflow contract

The model does not write this format: it writes a [semantic plan](#example-bitcoin-price-monitor), and the compiler (or a recipe) emits the Trama definition below. Compiled workflows use Trama's mandatory typed JSON references and require **Trama 3.0.1** or later. Each step is an HTTP call; there is no intermediate abstraction.

Node kinds: `task` (HTTP call), `switch` (JSON Logic branch), `sleep` (time pause), and `split`/`join` (parallel fan-out: a split runs its branches as independent child executions; the join barriers on all of them and the parent reads the aggregated results via `nodes.<join>.response.body.branches`).

```json
{
  "name": "example_workflow",
  "version": "2.0.0",
  "entrypoint": "first_node",
  "nodes": [
    {
      "id": "first_node",
      "kind": "task",
      "action": {
        "mode": "sync",
        "request": {
          "url": "https://api.example.com/resource",
          "verb": "POST",
          "body": {"field": "{{ execution.input.field }}"}
        },
        "successStatusCodes": [200, 201]
      },
      "next": "branch"
    },
    {
      "id": "branch",
      "kind": "switch",
      "cases": [
        {
          "name": "positive",
          "when": {">": [{"var": "nodes.first_node.response.body.value"}, 0]},
          "target": "handle_positive"
        }
      ],
      "default": "handle_negative"
    },
    {"id": "pause", "kind": "sleep", "durationSeconds": 60, "next": "first_node"}
  ]
}
```

Template variables: `execution.input.<field>`, `nodes.<id>.response.body.<field>`, `runtime.callback.url`.

Workflow steps call RiteSmith through `POST /trama/execute` with `{"capability_name": ..., "input": ...}` or `{"artifact_id": ..., "input": ...}` for a registered script.

`max_iterations` at the workflow root allows back-edges for polling loops; it is a safety cap, not a poll count.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Create `.env`:

```env
RITESMITH_DATABASE_URL=postgresql+asyncpg://ritesmith:ritesmith@localhost:5432/ritesmith
OPENAI_API_KEY=sk-...
```

Start PostgreSQL (if needed):

```bash
docker run --rm --name ritesmith-postgres \
  -e POSTGRES_USER=ritesmith -e POSTGRES_PASSWORD=ritesmith -e POSTGRES_DB=ritesmith \
  -p 5432:5432 postgres:16
```

Run migrations and start:

```bash
alembic upgrade head
uvicorn ritesmith.api.app:app --host 0.0.0.0 --port 8081 --reload
```

API: `http://localhost:8081` — Prometheus metrics: `http://localhost:8081/metrics/`

---

## Deployment

Docker Compose in `deploy/`:

```bash
cd deploy
# edit .env: set RITESMITH_DATABASE_URL and OPENAI_API_KEY
docker compose build && docker compose up -d
```

The `ritesmith-migrate` service runs `alembic upgrade head` before the app starts. The app joins the `deploy_default` network and exposes port **8081**.

To update a running deployment:

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  /path/to/RiteSmith/ user@host:~/ritesmith/
ssh user@host "cd ~/ritesmith/deploy && docker compose build && docker compose up -d"
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `RITESMITH_DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |
| `OPENAI_API_KEY` | — | Required for generation |
| `RITESMITH_LLM_MODEL` | `gpt-5-mini` | Generation model (fallback for the two below) |
| `RITESMITH_LLM_WORKFLOW_MODEL` / `RITESMITH_LLM_SCRIPT_MODEL` | — | Model per artifact type; agent mode uses the workflow model |
| `RITESMITH_LLM_REASONING_EFFORT` | `low` | Reasoning effort; `..._WORKFLOW_EFFORT` / `..._SCRIPT_EFFORT` override it |
| `RITESMITH_LLM_MODEL_FAST` | `gpt-4.1-mini` | Intent analysis / fast calls |
| `RITESMITH_GENERATION_MODE` | `proposal` | `agent` turns on the tool-using generation loop |
| `RITESMITH_GENERATION_DEADLINE_SECONDS` | `60` | Wall-clock budget per generation, repairs included (agent mode needs more, e.g. 240) |
| `RITESMITH_GENERATION_AGENT_MAX_TURNS` | `12` | Agent turns before it must submit |
| `RITESMITH_GENERATION_AGENT_TURN_TOKENS` | `8000` | Output tokens per agent turn |
| `RITESMITH_GENERATION_AGENT_ISOLATE_TOOLS` | `true` | Run validate/simulate/check_script in a separate process |
| `RITESMITH_GENERATION_AGENT_TOOL_MEMORY_MB` / `..._TOOL_TIMEOUT_SECONDS` | `512` / `30` | Memory and time limit of that process |
| `RITESMITH_LOCAL_TIMEZONE` | `UTC` | The user's time zone, for "tonight" and "until midnight" |
| `RITESMITH_SCRIPT_LANGUAGE` | `luau` | Language for new scripts: `luau` (LunarDyson) or `lua` (lupa); falls back to `lua` if the lunardyson package is missing |
| `RITESMITH_LUA_TIMEOUT_MS` | `1000` | Script VM timeout (both runtimes) |
| `RITESMITH_LUA_MEMORY_LIMIT_MB` | `32` | Script memory budget |
| `RITESMITH_LUA_SANDBOX_WORKERS` | `8` | Parallel script workers |
| `RITESMITH_GENERATION_MAX_ATTEMPTS` | `2` | Repair loop limit (proposal mode) |
| `RITESMITH_POLICY_DEFAULT` | `deny` | Default policy decision |
| `RITESMITH_WORKFLOW_ENGINE_URL` | — | Trama endpoint for delegation |
| `RITESMITH_PUBLIC_URL` | `http://ritesmith:8081` | RiteSmith's public URL (embedded in generated workflows) |
| `RITESMITH_TRAMA_TOKEN` | — | Shared secret for `POST /trama/execute` (required for Trama integration) |
| `RITESMITH_WEB_SEARCH_API_KEY` | — | Brave Search (web provider) |
| `RITESMITH_TELEGRAM_BOT_TOKEN` | — | Telegram provider |
| `RITESMITH_TELEGRAM_CHAT_ID` | — | Telegram provider |
| `RITESMITH_GOOGLE_TOKEN_JSON` | — | Path to Google OAuth token (Calendar + Gmail) |
| `RITESMITH_GOOGLE_CALENDAR_IDS` | — | `"personal:primary,family:id@..."` |
| `RITESMITH_DUCKDB_PATH` | — | DuckDB file path |
| `RITESMITH_OBSIDIAN_VAULT_PATH` | — | Obsidian vault root |
| `RITESMITH_LOOMHARBOR_URL` | — | LoomHarbor endpoint (`home` provider, devices for agent mode) |

All variables use the `RITESMITH_` prefix (loaded from `.env` via pydantic-settings).

Initial Google OAuth token: `python -m ritesmith.tools.google_auth`

---

## Observability

Prometheus metrics at `GET /metrics/` (trailing slash required):

| Family | Instruments |
|---|---|
| HTTP | `ritesmith_http_requests_total`, `ritesmith_http_request_duration_seconds` |
| LLM | `ritesmith_llm_request_duration_seconds`, `ritesmith_llm_tokens_total`, `ritesmith_llm_errors_total` |
| Generation | `ritesmith_generation_attempts_total`, `ritesmith_artifact_reuse_total`, `ritesmith_validation_failures_total` |
| Execution | `ritesmith_execution_duration_seconds`, `ritesmith_execution_status_total`, `ritesmith_lua_timeout_total` |
| Policy | `ritesmith_policy_decisions_total` |
| Sandbox | `ritesmith_sandbox_queue_depth` |
| Audit | `ritesmith_audit_write_failures_total` |

A Grafana dashboard is at `deploy/grafana/ritesmith.json`.

---

## Roadmap

### Implemented

- FastAPI HTTP API
- PostgreSQL artifact registry with FTS (`tsvector` + GIN index) and versioning
- LLM generation loop — Lua scripts and Trama v2 workflows
- Generate → validate → repair loop with attempt budget and backoff
- Unified `POST /generate` — intent analysis routes automatically to Lua or workflow generation
- Lua sandbox (`lupa` — configurable timeout, memory limit, workers)
- Schema validation (input/output) at generation and execution time
- Policy engine (risk-level based, deny-by-default)
- `POST /plans` — high-level intent → plan endpoint
- Workflow delegation adapter (Trama)
- Execution status tracking and history
- Idempotency keys
- Audit log
- Luau script runtime (LunarDyson) — default; type-checked before execution, with VM time / memory / tool / effect budgets; Lua (lupa) kept for legacy artifacts
- Server-issued approval tokens (HMAC per artifact version) gating policy-restricted executions
- Split/join parallel nodes in generated Trama workflows
- Tool providers (14 namespaces) — dual Lua / MCP surface
- MCP server (`mcp-server/`)
- Alembic migrations
- Prometheus metrics (HTTP, LLM, generation, execution, audit) + Grafana dashboard
- Docker Compose deployment
- Rate limiting on generation endpoints (10 req/min per IP)
- X-Request-ID correlation across requests, audit events, and logs
- LLM retry with exponential backoff (transient errors)
- Per-operation LLM `max_tokens` and `temperature` tuning
- Graceful shutdown (drains Lua executor, disposes DB pool)
- HTTP connection pooling for all host function providers
- Semantic workflow plans compiled to Trama (counters, joins, continuation in code)
- Closed capability contracts, enforced at generation and at `/trama/execute`
- Recipes built without a model call: `threshold_action`, `price_variation`, `presence_change`, `presence_reminder`
- Agent-mode generation: tool-using loop with contracts, devices, read-only probes, validation, offline simulation and script checks; cacheable prompt; isolated tool process; per-plan transcript

### Next

- Learn from what worked: offer plans that executed successfully as examples through a `find_similar` tool
- Make agent mode the default once it is measured on the full corpus
- Host-side security hardening — API authentication, compile-only `/validate`, DNS-resolving SSRF guard
- Embeddings-based artifact search
- Provider/tool manifests UI
- Generated test suggestions
- Signed artifact versions

### Later

- Subprocess / WASM sandbox isolation
- Multi-tenant authorization → see **Contextual authorization** below
- Policy-as-code
- Python and TypeScript SDKs
- Hosted control plane

#### Design note: contextual authorization

Runtime **profiles** (`transform_only` … `trusted_internal`) are coarse capability
sets — they decide *which kinds of tool* a script may call, not *which resource*
it may touch. A `sensitive_personal` script can read "a calendar"; nothing today
scopes it to *this* user's calendar rather than someone else's. That is adequate
only because the deployment is single-user.

The intended model, deferred until there is a real multi-user boundary: each tool
call carries a **resource scope** (e.g. `calendar_id`, `vault_path`, `device_id`)
that the host validates against the **caller's identity** before dispatch —
authorization by resource, orthogonal to the profile's capability grant. The
PolicyEngine stays the capability/approval gate; contextual authz is a second,
per-resource check at the host boundary. Not implemented in this cycle; captured
here and in the Obsidian design notes so the profile system is not mistaken for
per-resource access control.

---

## Generation latency

This section describes proposal mode, the single-answer generator; agent mode is
described under [How generation works](#how-generation-works).


Generation uses a process-scoped OpenAI client, configurable script/workflow
models and effort, locally selected Trama examples, and an internal typed graph
that Python expands into the existing Trama JSON. Automatic requests can combine
intent analysis with the first candidate; explicit artifact types skip analysis.
Tests derived independently from intent/schemas run alongside generation. Reuse,
validation, certification, policy, approval and persistence remain in their services.

`context.workflow_examples` accepts `linear`, `bounded_polling`, `fixed_samples`,
`continuation`, `parallel`, `state_tracking`, `content_monitor`, `async_callback`,
and `compensation`. Omit it for local selection, pass a list to select examples,
or pass `[]` for essential rules only. Unknown identifiers return HTTP 422.
Examples guide generation without restricting allowed node kinds. The option is
documented in HTTP and MCP schemas.

Set `RITESMITH_LLM_SCRIPT_MODEL` and `RITESMITH_LLM_WORKFLOW_MODEL` separately;
generation and repair inherit `RITESMITH_LLM_REASONING_EFFORT=low`. Classification
and reuse judgment use `RITESMITH_LLM_MODEL_FAST`. The original
`RITESMITH_LLM_MODEL` remains the fallback. Independent switches are documented in
[the latency evaluation guide](benchmarks/generation_latency/README.md).

Workflows are generated as **semantic plans** by default
(`RITESMITH_GENERATION_SEMANTIC_WORKFLOWS=true`): the model describes calls, waits,
conditions, parallel branches and bounded repeats, and Python compiles counters,
joins, first-pass seeds and continuation blocks into native Trama. Set it to `false`
to fall back to model-written compact graphs, which still cover nested or multiple
repeats. Generation stops after `RITESMITH_GENERATION_DEADLINE_SECONDS` (default 60).
Compiled workflows require **Trama 3.0.1** or later (mandatory typed JSON rendering, and
resumed loops that read results after a `switch`);
upgrade the engine before or together with RiteSmith.
The [scorecard](benchmarks/generation_latency/README.md#pass-rate-scorecard) for 0.2.1, graded
against the closed capability contracts, measured 68% valid workflows (median 8.5 s,
9.4k tokens per valid workflow; 64%, 20.1 s and 22.5k before) and 96% valid Luau scripts.

The **95% valid and complete within 10 seconds, with at least 50% lower estimated
cost per valid artifact than baseline** target is an acceptance gate,
not a guaranteed property of these defaults. HTTP timing includes searches,
test generation, repairs, fallback and requested persistence. Provider cache
tokens are recorded separately. Qualification and promotion require measured
latency, quality and cost; seven-day monitoring follows a successful promotion.
The current validation uses a bounded representative subset and preserved
baseline results. It does not establish a full-corpus SLO.

### Generation improvements and controlled validation

See [generation improvements](docs/generation-improvements.md) for independent
fixture tests, bounded recovery, optional Luau body assembly and semantic Trama
plans. [The staging overlay](deploy/generation-staging.env.example) enables the
new formats on an isolated instance. Performance/cost targets remain unqualified;
historical benchmark evidence remains separate from subsequent improvements.

The optional [typed decisions experiment](docs/typed-decisions-validation.md)
adds schema-constrained responses and parameterized Trama constructors, disabled
by default, with a frozen comparison capped at twelve requests and US$1.
The [comparison report](benchmarks/generation_latency/results/typed-decisions-20261008/RESULTS.md)
records its early accounting stop, observed failures and experimental recommendation.

The subsequent [reliability corrections](docs/generation-reliability-validation.md)
reject unusable artifacts with diagnostics and preserve strict certification.
[The Trama integration](docs/trama-typed-json-integration.md) uses mandatory typed
JSON with explicit initial state and retained continuation work, requiring the
compatible engine revision. The [final offline review](benchmarks/generation_latency/results/integration-review-20261009/RESULTS.md)
records 992 passing tests; it does not establish LLM accuracy or latency/cost targets.

For the owner's direct r2d2 update and the executable image/configuration rollback,
see [generation deployment on r2d2](docs/generation-deployment-r2d2.md). The rollback
retains current application data; a separate database backup is available for recovery.

---

## License

Apache License 2.0
