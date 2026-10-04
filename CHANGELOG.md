# Changelog

All notable changes to RiteSmith. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project has not cut tagged
releases yet, so changes are grouped under `Unreleased`.

## [Unreleased]

### Added
- **Luau script runtime (LunarDyson).** New capabilities are generated as
  `luau_script` and executed by the embedded [LunarDyson](https://github.com/thiagoribeiro/lunardyson)
  runtime: type-checked (`--!strict`) against the tool signatures before execution,
  with a VM time deadline, a memory budget, and per-tool / per-effect-class budgets.
  `RITESMITH_SCRIPT_LANGUAGE` (`luau` default, `lua` legacy) selects the language and
  doubles as a rollback switch; it falls back to `lua` when the `lunardyson` package
  is absent. See "Why Luau + LunarDyson" in the README.
- **Server-issued approval tokens.** `POST /artifacts/{id}/versions/{version}/approve`
  returns an HMAC bound to that artifact version. Executions that policy gates on
  approval now require a matching token.
- **Split/join parallel workflows.** Workflow generation and the validator understand
  Trama `split`/`join` fan-out: branches run as independent child executions and the
  parent reads results via `nodes.<join>.response.body.branches`.
- **Test battery.** Shared test infrastructure (transaction-isolated DB sessions,
  a scripted fake LLM, factories) plus coverage for the policy decision table, the
  plan state machine, self-heal/repair, the CASP client, the `/trama/execute` bridge,
  the Trama adapter, host functions / SSRF guard, and contracts across all providers
  (~400 new tests; coverage 61% → 69%).

### Changed
- **Default models** are now `gpt-5-mini` (generation) and `gpt-4.1-nano` (intent
  analysis), replacing `gpt-4.1` / `gpt-4o-mini`.
- **PolicyEngine** honors `allow_unapproved_low_risk`, and artifacts created by hand
  (`POST /artifacts`) get a risk floor from their runtime profile, so a device-control
  script cannot self-declare low risk to skip approval.
- README rewritten around the Luau/LunarDyson runtime, with the approval endpoint,
  split/join, the full provider list, and `script_language` documented.

### Fixed
- Any non-empty `approval_token` no longer bypasses approval — only a server-issued,
  version-bound token is accepted.
- CI lint is green again: pre-existing `C408` findings (flagged by ruff 0.16) and
  unformatted files are resolved; no behavior change.

### Security
- Known, still-open (tracked under roadmap → Next; the deployment is on a trusted
  network): the Lua sandbox still exposes the `python` global, `POST /validate`
  executes submitted code without the sandbox, no route requires authentication, and
  the SSRF guards are prefix-regex (no DNS resolution). The Luau path and the items
  above are not affected.

## 2026-09 — earlier work (pre-changelog)

Foundation, on `main` before this changelog was added: FastAPI control plane,
PostgreSQL artifact registry with full-text search and versioning, the LLM
generate → validate → repair loop, the unified `POST /generate` dispatcher, the
`POST /plans` planner with an explicit state machine and replanning, the Lua (lupa)
sandbox, the PolicyEngine, Trama v2 workflow generation and delegation, the tool
providers (dual Lua / MCP surface), the standalone MCP server, Alembic migrations,
Prometheus metrics with a Grafana dashboard, rate limiting, request-ID correlation,
and Docker Compose deployment.
