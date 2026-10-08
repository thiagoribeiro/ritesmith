# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

RiteSmith is a control plane that turns an LLM-expressed intent into deterministic, reusable, versioned artifacts (Lua scripts or Trama workflow definitions), rather than keeping the LLM in the runtime loop. Flow: **decide → define → execute**. The LLM is called once to generate/repair an artifact; a Postgres-backed registry stores it (full-text search for reuse); a sandboxed Lua runtime (`lupa`) or a delegated [Trama](https://trama.run) workflow engine executes it without further LLM involvement.

Read `README.md` for the full concept, API surface, and example (Bitcoin price monitor). It contains the canonical architecture diagram and the Lua/workflow contracts — don't duplicate that here, just be aware it exists and is kept current.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp deploy/.env.example .env   # then edit RITESMITH_DATABASE_URL, OPENAI_API_KEY

# Migrations
alembic upgrade head
alembic revision -m "description"          # new migration (edit manually — no autogenerate wired)

# Run the API server
uvicorn ritesmith.api.app:app --host 0.0.0.0 --port 8081 --reload

# Run the standalone MCP server (stdio, spawned as a subprocess by an agent)
cd mcp-server && python server.py

# Lint / format
ruff check .
ruff format --check .

# Tests — requires a running Postgres reachable via TEST_DATABASE_URL
export TEST_DATABASE_URL="postgresql+asyncpg://ritesmith:ritesmith@localhost:5432/ritesmith_test"
pytest -m "not e2e"                                        # unit + integration
pytest -m "not e2e" --cov=ritesmith --cov-report=term-missing
pytest -m e2e                                               # needs a real OPENAI_API_KEY too
pytest ritesmith/tests/unit/test_generation.py -k some_case # single test
```

`OPENAI_API_KEY` is read directly by the `openai` SDK (no `RITESMITH_` prefix) — every other setting is `RITESMITH_`-prefixed and loaded from `.env` via pydantic-settings (`ritesmith/config.py`). CI sets `RITESMITH_OPENAI_API_KEY=placeholder`, which is a no-op — non-`e2e` tests must not require a live key.

There is no local Postgres bootstrap script; either run the Docker Compose stack in `deploy/`, or start a bare container:
```bash
docker run --rm --name ritesmith-postgres -e POSTGRES_USER=ritesmith -e POSTGRES_PASSWORD=ritesmith -e POSTGRES_DB=ritesmith -p 5432:5432 postgres:16
```

## Architecture

### Request flow (generation)

```
POST /generate or POST /plans
  → GenerationDispatcher.dispatch()          (ritesmith/core/generation_dispatcher.py)
      → LLMProvider.analyze_intent()          decides lua_script vs trama_workflow
      → GenerationService.generate_lua()       or WorkflowGenerationService.generate_workflow()
          → reuse.check_reuse()                FTS search first; skip generation if a good match exists
          → LLM.generate_lua() / repair_lua()   loop up to settings.generation_max_attempts
          → ValidationPipeline.run()            syntax, forbidden tokens, size, schema, allowed-fns, tests
          → RegistryService.create_artifact()   persisted only if save=True and valid
```

`POST /plans` (`ritesmith/core/planning.py::PlanBuilder`) is the higher-level entry point: it runs the same generate/reuse loop per required artifact type, then evaluates each artifact through `PolicyEngine`, aggregates a plan-level status (`blocked`/`proposed`/`approved`), and persists a `Plan` row with an explicit state machine (`_ALLOWED_TRANSITIONS`). Plans also support **replanning** (`PlanBuilder.replan`) — capped by `RITESMITH_CASP_MAX_REPLAN_ATTEMPTS` — used when a downstream execution fails and CASP wants a corrected plan.

### Request flow (execution)

`ExecutionService` (`ritesmith/core/execution.py`) is the only path that actually runs an artifact:
1. Idempotency-key short-circuit.
2. `PolicyEngine.evaluate()` — `deny` stops the request; `require_approval` without an `approval_token` parks it in `waiting`.
3. Routes by `artifact_type`: `lua_script` → `LuaScriptRuntime.execute()` (in-process sandbox); `trama_workflow` → either delegated to a real Trama engine (`RITESMITH_WORKFLOW_ENGINE_URL` + `workflow_delegation_enabled`) or stubbed with a synthetic delegated id.
4. On a Lua **contract crash** (error message prefixed `Runtime error:` / `Syntax/load error:`) the artifact is auto-deprecated and a background regeneration task is kicked off (`_deprecate_and_regen` / `_background_regen`) using the original `GenerationJob.goal` — this is how the system self-heals a bad artifact without a human in the loop.

### PolicyEngine

Ordered rule evaluation, first match wins (`ritesmith/core/policy.py`): shell scripts always denied → risk level `critical` denied, `high`/`medium` require approval, `low` auto-allowed (if `allow_unapproved_low_risk`) → `trama_workflow` delegation requires approval → fallback to `RITESMITH_POLICY_DEFAULT` (default `deny`). This same engine is invoked both at plan-build time and at execution time — plans can look "approved" but execution still re-checks policy independently.

### Tool providers — the dual Lua/MCP surface

`ritesmith/runtime/providers/` implements one class per namespace (`market`, `web`, `telegram`, `calendar`, `email`, `duckdb`, `obsidian`, `grafana`, `loomharbor`, `casp`, `reports`, `stat`...), each a `ToolProvider` subclass (`ritesmith/runtime/providers/base.py`). A provider is the **single source of truth** for both surfaces:
- `lua_functions()` → injected into the Lua sandbox as `namespace.function` globals, gated by `profile` (see below).
- `mcp_tools()` → registered directly in the standalone MCP server (`mcp-server/server.py`).

`PROVIDERS` (`ritesmith/runtime/providers/__init__.py`) is the master list; `is_available()` gates registration on required env vars/config being present. `ritesmith/core/provider_registration.py` upserts provider manifests + derived capabilities into the DB registry at FastAPI startup (`_lifespan` in `ritesmith/api/app.py`). Adding a new integration means adding one `ToolProvider` subclass and appending it to `PROVIDERS` — everything else (manifest registration, capability listing, Lua injection, MCP tool listing) wires itself up automatically.

### Lua sandbox and runtime profiles

`ritesmith/runtime/sandbox.py` creates a fresh `lupa` Lua runtime per execution, strips dangerous globals (`io`, `os`, `debug`, `load`, `require`, ...), and injects only the host functions allowed for the artifact's declared **runtime profile** (`ritesmith/runtime/host_functions.py::PROFILES`): `transform_only` < `readonly_network` < `notification` < `sensitive_personal`; separately `analytics_local`, `filesystem_write`, `reporting`, `side_effects`; and `trusted_internal` (superset, used for CASP device-control scripts). Execution runs on a `ThreadPoolExecutor` with a timeout (`RITESMITH_LUA_TIMEOUT_MS`) — note the sandbox doc-comment: the executing thread is **not force-killed** on timeout, real isolation is deferred to a future subprocess/WASM sandbox.

Every Lua capability is a single `function run(input, context)` — input/output validated against JSON Schema before/after execution (`ritesmith/core/validation.py`).

### CASP subsystem

`ritesmith/runtime/casp/` is a protocol client for external "CASP providers" (e.g. LoomHarbor — device/resource control). `client.py` does HTTP discovery/query/resolve/execute against `RITESMITH_CASP_PROVIDERS` (a JSON list of `{url, resource_types, weight, priority}`), routed by resource type and priority; `host_bridge.py` registers `casp.*` as `trusted_internal`-profile Lua host functions. When any CASP provider is configured, `PlanBuilder._handle_lua_artifact` unconditionally uses the `trusted_internal` profile so generated scripts *can* reach `casp.*` (the LLM decides whether to use it). `home.*` was the legacy LoomHarbor namespace; app startup scans persisted artifacts and logs a warning if any still reference it (see `_lifespan` in `app.py`).

### Registry / storage

SQLAlchemy async models in `ritesmith/registry/models.py`; migrations under `ritesmith/storage/migrations/versions/` (plain Alembic, hand-written — no autogenerate). Artifacts are versioned (`Artifact` + `ArtifactVersion`); full-text search (`ritesmith/registry/search.py`) uses a Postgres `tsvector` column maintained by a DB trigger (see `_TRIGGER_FN` in `ritesmith/tests/conftest.py` for its exact definition — tests recreate it against the test DB since it's not part of `Base.metadata`).

### API layout

`ritesmith/api/app.py::create_app()` wires middleware (request-ID, Prometheus timing, rate limiting via `slowapi`) and routers under `ritesmith/api/routes/` (one module per resource: artifacts, capabilities, executions, generations, plans, policies, providers, search, trama, memory, validation, admin, health). The MCP sub-app (`ritesmith/api/mcp_handler.py`) is mounted at `/mcp` **outside** the `BaseHTTPMiddleware` stack deliberately — SSE streaming needs raw ASGI `send` and conflicts with `BaseHTTPMiddleware`. `/trama/execute` (`ritesmith/api/routes/trama.py`) is the bridge Trama task nodes call back into to run RiteSmith capabilities; it's authenticated with a static bearer token (`RITESMITH_TRAMA_TOKEN`), not per-user auth — there is no multi-tenant auth model yet (see README roadmap).

### MCP server (`mcp-server/`)

A separate, minimal package (own `pyproject.toml`) meant to run as a stdio subprocess spawned by an agent process — not imported by the main API. It re-exposes provider `mcp_tools()` directly (fast, stateless domain calls) plus a handful of meta-tools (`ritesmith_plan`, `ritesmith_generate`, `ritesmith_search_artifacts`, `ritesmith_execute`, `ritesmith_get_execution`, `ritesmith_list_capabilities`) that call the RiteSmith HTTP API (`mcp-server/_ritesmith.py`) for anything needing generation/validation/registration/durable execution.

### LLM abstraction

`ritesmith/llm/base.py` defines the `LLMProvider` protocol (`analyze_intent`, `generate_lua`, `repair_lua`, workflow equivalents); `ritesmith/llm/openai_provider.py` is the only implementation. Prompts live in `ritesmith/llm/prompts.py`. Swapping providers means implementing `LLMProvider`, not touching call sites — `GenerationService`/`WorkflowGenerationService`/`PlanBuilder` all depend on the abstract protocol.

## Conventions worth knowing

- Some core modules (`generation.py`, `planning.py`, `policy.py`, `host_functions.py`, `validation.py`) have Portuguese module/inline docstrings describing their flow step-by-step — read them, they're accurate design docs, not filler.
- Tests need a **real** Postgres (`TEST_DATABASE_URL`), not sqlite/mocks — `conftest.py` drops/recreates the full schema per session and rolls back per test. There's no way to run the unit suite without a DB.
- `pytest.mark.e2e` tests hit the real OpenAI API and are excluded from the default CI run (`pytest -m "not e2e"`).
- IDs are ULIDs with a type prefix (`art_`, `gen_`, `plan_`, `exec_`, `trama_`) generated via `ritesmith/core/ids.py::generate_id`.
- Outbound URLs (plan callback URLs, Lua `http.*` host functions) are checked against a private/loopback IP blocklist before use (`_is_safe_callback_url` in `planning.py`, `_BLOCKED_PATTERNS` in `host_functions.py`) — keep that check if you touch either call path, it's an SSRF guard, not incidental.
