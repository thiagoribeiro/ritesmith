# Changelog

All notable changes to RiteSmith. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/) and versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor releases may break).

## [Unreleased]

## [0.2.0] - 2026-10-10

First tagged release. Generated workflows now require **Trama 3.0.0** (mandatory typed
JSON rendering); see Upgrade notes.

### Upgrade notes
- **Upgrade Trama to 3.0.0 before or together with RiteSmith**, and roll them back
  together. Workflows compiled by this release use typed JSON references and literal
  envelopes that older Trama versions cannot render. Cancel running plans that
  RiteSmith 0.1.x compiled with cyclic graphs (`POST /plans/{id}/cancel`); they may read
  references that are no longer implicitly null.
- **Run migration `0005`** (`alembic upgrade head`): artifact `usage_description`
  and a reweighted full-text trigger for reuse recall.
- **Settings.** `generation_semantic_workflows` defaults to `true` and
  `generation_deadline_seconds` to 60. Removed `RITESMITH_GENERATION_MERMAID_WORKFLOWS`,
  `..._STRUCTURED_OUTPUTS(_OPERATIONS)` and `..._PATTERN_PARAMETERS(_OPERATIONS)` are
  ignored if still present. `generation_workflow_typed_json` is accepted but has no effect.
- **Exhausted generation returns HTTP 422 `generation_failed`** on `/generate`, the
  specialized endpoints, MCP and plan materialization, instead of an unusable artifact.

### Added
- **Semantic workflow generation (default).** The model writes a semantic plan
  (calls, waits, conditions, parallel branches, bounded repeats); Python compiles
  counters, joins, first-pass seeds and continuation blocks into native Trama.
  Loop state is read as `{ref: <repeat id>, path: <key>}`. Requires Trama with
  mandatory typed JSON rendering (Trama 3.0.0). `RITESMITH_GENERATION_SEMANTIC_WORKFLOWS=false`
  falls back to model-written compact graphs.
- **Generation scorecard.** `benchmarks/generation_latency/scorecard.sh` measures pass
  rate, latency and cost per valid artifact with a hard spend cap (`run.py --max-usd`).
  2026-10-10 results: 94% valid Luau scripts and 77% valid workflows (p50 7.3 s), up
  from 86% / 56% (p50 13.5 s) before the semantic-plan and parser fixes.
- **Plan lifecycle.** `GET /plans` (status filter, limit), `POST /plans/{id}/cancel`,
  and `callback_url` fired on terminal status (retry, SSRF guard,
  `RITESMITH_CALLBACK_ALLOWED_HOSTS`). Executions carry an `X-RS-Plan` header and
  `/trama/execute` returns 409 for cancelled plans, so running workflows stop. (#3)
- **Certification tiers.** Luau artifacts are certified `strict`, `nonstrict` or `lua`;
  `nonstrict` needs approval to execute and `RITESMITH_REQUIRE_STRICT_TYPECHECK` makes it
  a generation failure. Medium+ risk artifacts must pass executed test cases
  (`RITESMITH_REQUIRE_TESTS_MIN_RISK`), and `RITESMITH_REQUIRE_LUAU` makes a missing
  LunarDyson runtime a startup failure. Luau budgets are split into VM CPU, per-tool
  (`RITESMITH_LUAU_TOOL_TIMEOUT_MS`) and wall-clock (`RITESMITH_LUAU_WALL_CLOCK_MS`). (#2)
- **Three-stage reuse.** Full-text recall over `usage_description`, a deterministic
  contract-compatibility gate, then an LLM relevance judge (`RITESMITH_REUSE_LLM_JUDGE`). (#2)
- **Faster generation.** Shared OpenAI client and recall, per-operation models and
  effort (`RITESMITH_LLM_SCRIPT_MODEL`, `..._WORKFLOW_MODEL`, `..._EFFORT`), locally
  selected examples, a unified intent + first-candidate proposal, independent tests in
  parallel, and one bounded recovery controller with a request deadline. (#5)
- **Typed test specifications.** Controlled tool fixtures and isolated LunarDyson
  validation; fixture problems are reported as non-repairable diagnostics. (#5)
- **Bounded continuation.** Long repeats run in blocks of at most 20 passes and
  continue through `/plans`, preserving remaining work, state and the original deadline.
  Compatible cached continuations compile locally without an LLM call. (#5, #6)
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
- **Generation deadline** defaults to 60 s (was 30 s).
- **Generation acceptance is truthful.** Exhausted generation returns HTTP 422
  `generation_failed` with diagnostics and preserved rejected candidates; unusable
  artifacts are not published and dependent plans are not marked ready. (#6)
- **Trama transport.** The `X-Trama-Template-Mode` header is no longer sent; JSON
  bodies rely on Trama's mandatory typed rendering. Missing or invalid references and
  malformed graphs are rejected before acceptance. (#6)
- **`ValidationPipeline`** no longer reports a policy check: `valid` means validated,
  never approved; the PolicyEngine is the only execution gate. (#2)
- **Default models** are now `gpt-5-mini` (generation) and `gpt-4.1-nano` (intent
  analysis), replacing `gpt-4.1` / `gpt-4o-mini`.
- **PolicyEngine** honors `allow_unapproved_low_risk`, and artifacts created by hand
  (`POST /artifacts`) get a risk floor from their runtime profile, so a device-control
  script cannot self-declare low risk to skip approval.
- README rewritten around the Luau/LunarDyson runtime, with the approval endpoint,
  split/join, the full provider list, and `script_language` documented.

### Removed
- Mermaid workflow generation, Structured Outputs transport and pattern-parameter
  constructors (and their `RITESMITH_GENERATION_*` settings). None improved validity
  in the 2026-10-10 scorecard; stale settings in `.env` are ignored.

### Fixed
- Script generation without caller-supplied test cases no longer fails with HTTP 500
  (independent test generation read schema attributes built-in functions lack).
- Count-only repeats no longer make final actions reachable before the last pass.
- Copied prompt placeholders and null-valued keys no longer discard valid candidates.
- A Luau tool timeout is reported as a timeout instead of a runtime error, so a slow
  tool no longer auto-deprecates the artifact. (#2)
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
