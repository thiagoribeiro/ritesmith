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

---

## Where RiteSmith fits

```
LLM / Agent  →  understands intent
RiteSmith    →  generates the system
Trama        →  executes it reliably
```

RiteSmith is the materialization layer.

Given an intent, it searches for existing capabilities, generates new Lua scripts and workflow definitions when needed, validates them under guardrails (schema, policy, sandbox tests), registers them in a versioned artifact registry, and delegates durable execution to [Trama](https://trama.run).

The LLM is no longer controlling the system while it runs. It defines the system before it runs.

---

## Example: Bitcoin price monitor

**Intent:** "Tell me if Bitcoin drops 3% from its current price."

Without RiteSmith, an agent loops — calling the LLM and market API at every check interval.

With RiteSmith:

1. Search artifact registry → finds or generates a `math.percentage_change` Lua capability — deterministic, schema-validated, sandboxed
2. Generate a Trama workflow (nodes are HTTP calls — Lua capabilities are called via `POST /executions`):

```json
{
  "name": "bitcoin_drop_monitor",
  "version": "2.0.0",
  "entrypoint": "fetch_price",
  "max_iterations": 288,
  "nodes": [
    {
      "id": "fetch_price",
      "kind": "task",
      "action": {
        "mode": "sync",
        "request": {
          "url": "http://ritesmith:8081/trama/execute",
          "verb": "POST",
          "headers": {
            "Authorization": "Bearer <trama_token>",
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
          "name": "dropped_3pct",
          "when": {"<=": [
            {"/": [{"var": "nodes.fetch_price.response.body.output.price"}, {"var": "execution.input.initial_price"}]},
            0.97
          ]},
          "target": "notify"
        },
        {
          "name": "loop_exhausted",
          "when": {">=": [{"var": "execution.loop_count"}, 288]},
          "target": "end"
        }
      ],
      "default": "sleep"
    },
    {
      "id": "notify",
      "kind": "task",
      "action": {
        "mode": "sync",
        "request": {
          "url": "http://ritesmith:8081/trama/execute",
          "verb": "POST",
          "headers": {
            "Authorization": "Bearer <trama_token>",
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
    {"id": "sleep", "kind": "sleep", "durationSeconds": 300, "next": "fetch_price"}
  ]
}
```

3. Validate the Lua capability and the workflow — schemas, policy, sandbox
4. Register both artifacts — reused on every future similar request
5. Delegate to Trama — the LLM is no longer in the loop

The `math.percentage_change` Lua script:

```lua
function run(input, context)
  local initial = input.initial_value
  local current = input.current_value
  if initial == 0 then error("initial_value cannot be zero") end
  return { percentage_change = ((current - initial) / initial) * 100 }
end
```

---

## Why not just agent loops?

| | Agent loop | RiteSmith |
|---|---|---|
| LLM cost | every step | once (generation + repair) |
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
| **Capability** | A named, versioned, schema-validated Lua script with input/output contracts, risk level, and side-effect declaration |
| **Artifact** | A stored capability or workflow definition — versioned, searchable, reusable |
| **Plan** | A proposed set of capabilities + workflow generated from an intent, with optional approval gate |
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

Active providers are registered in the artifact registry at startup and returned by `GET /providers`.

---

## MCP server

The `mcp-server/` directory is a standalone MCP server that gives the agent two surfaces in one:

**Domain tools** — direct calls to provider implementations:
`web_search`, `web_fetch`, `market_bitcoin_price`, `market_exchange_rate`, `calendar_get_today`, `calendar_create_event`, `email_search`, `telegram_send`, `duckdb_query`, `obsidian_search`, …

**RiteSmith meta-tools** — delegate to the HTTP API:
`ritesmith_plan`, `ritesmith_generate`, `ritesmith_search_artifacts`, `ritesmith_execute`, `ritesmith_get_execution`, `ritesmith_list_capabilities`

The agent calls domain tools directly for fast, stateless lookups. It calls `ritesmith_plan` when the task needs generation, validation, registration, or durable execution. `ritesmith_generate` automatically decides whether to produce a Lua script or a Trama workflow from the given intent.

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
  RS --> DISP[GenerationDispatcher\nintent analysis → route]
  DISP --> GEN[LLM Generation Loop\nLua · Trama workflows]
  GEN --> VAL[Validation + Repair\nschema · policy · sandbox]
  VAL --> REG
  REG --> LUA[Lua Runtime\nlupa sandbox]
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

Accepts `intent` + optional `save`, `context`, `constraints`, `input_schema`, `output_schema`. Runs intent analysis to decide whether to produce a Lua script or a Trama workflow, then runs the full generate → validate → repair → register loop and returns the validated artifact.

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
GET  /plans/{plan_id}
```

`POST /plans` is the high-level entry point. Accepts `intent` + `constraints`. Searches, generates, validates, and returns a plan with required capabilities and workflow definition. Optionally gates on approval.

### Artifacts

```http
GET  /artifacts?query=extract+invoice&artifact_type=lua_script
GET  /artifacts/{artifact_id}
GET  /artifacts/{artifact_id}/versions
POST /artifacts
```

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
  "idempotency_key": "req_abc123"
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
GET /providers
POST /validate
POST /policies/evaluate
POST /memory/search
GET /health
GET /metrics/
```

---

## Lua script contract

Every Lua capability exposes a single `run` function:

```lua
function run(input, context)
  -- input matches the declared input_schema
  -- return value must match output_schema
  return { result = "ok" }
end
```

Rules:
- `input` is validated against the input schema before execution
- return value is validated against the output schema after
- unsafe stdlib modules are disabled
- host functions are explicit and profile-controlled
- timeout: `RITESMITH_LUA_TIMEOUT_MS` (default 1000 ms)
- memory: `RITESMITH_LUA_MEMORY_LIMIT_MB` (default 32 MB)

Available host functions depend on the sandbox profile (`transform_only`, `readonly_network`, `notification`, `sensitive_personal`, `analytics_local`, `trusted_internal`).

---

## Workflow contract

RiteSmith generates [Trama v2](https://trama.run) workflow definitions. Each step is an HTTP call — there is no intermediate abstraction.

Node kinds: `task` (HTTP call), `switch` (JSON Logic branch), `sleep` (time pause).

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

To call a RiteSmith Lua capability from a workflow step, use `POST /executions` as the task URL with `{"artifact_id": "...", "input": {...}}` as the body.

Use `max_iterations` at the workflow root for polling loops (bypasses the cycle detector).

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
| `RITESMITH_LLM_MODEL` | `gpt-4.1` | Generation model |
| `RITESMITH_LLM_MODEL_FAST` | `gpt-4o-mini` | Repair / fast calls |
| `RITESMITH_LUA_TIMEOUT_MS` | `1000` | Sandbox timeout |
| `RITESMITH_LUA_SANDBOX_WORKERS` | `8` | Parallel Lua workers |
| `RITESMITH_GENERATION_MAX_ATTEMPTS` | `5` | Repair loop limit |
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

### Implemented (V0)

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
- Tool providers (8 namespaces) — dual Lua / MCP surface
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

### Next

- Embeddings-based artifact search
- Provider/tool manifests UI
- Approval flow API
- Generated test suggestions
- Signed artifact versions

### Later

- Subprocess / WASM sandbox isolation
- Multi-tenant authorization
- Policy-as-code
- Python and TypeScript SDKs
- Hosted control plane

---

## License

Apache License 2.0
