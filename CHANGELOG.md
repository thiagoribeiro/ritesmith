# Changelog

All notable changes to RiteSmith. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/) and versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor releases may break).

## [Unreleased]

### Added
- **`stop` in semantic plans**: `{kind:"stop"}` inside a repeat (typically in a condition)
  ends it after the steps before it. "Watch X; when Y do A and B, tell me, and stop" is
  now an ordinary generated plan with any number of actions; before, only recipes could
  act once and stop.
- **`threshold_action.actions[]`**: several actions in order (up to 6); `action` still works.
- **Regeneration on mismatch** (`enforce`): a model-built workflow that the intent
  verifier rejects is generated once more with `context.verifier_feedback`
  (requested vs built), then verified again. Recipes are never regenerated.
- **Execution outcome in completion callbacks** (`outcome`): what the workflow actually
  did, from Trama's step log. Example: "Fez: 21/11 06:12 executa ac.turn_off em hugo" or
  "Não agiu: a condição não aconteceu em 97 verificações".
- Readback: "— uma vez, e encerra" for act-once effects, and the full horizon of
  duration-based continuations.

### Changed
- **The agent no longer bends requests to fit recipes.** It lists every requirement
  first and uses a recipe only if all of them map onto its params; otherwise it writes a
  plan. `context.user_request` (the user's words) wins over the paraphrase.
- **Plan readback** (`Plan.readback`): what the plan's workflows will do, derived
  deterministically from the graph. It covers effects (messages, device actions, reports)
  with their wall-clock times, or their trigger conditions and poll interval, plus the
  deadline. Arrivals and departures read as such, and unrolled repeats collapse. Callers
  should confirm with this text instead of echoing the intent.
- **Intent verification** (`RITESMITH_INTENT_VERIFICATION`, default `off`): the readback
  is compared with the user's own words (`context.user_request`) before activation.
  `shadow` records the verdict in `Plan.verification` and in the audit log; `enforce`
  blocks a mismatch. It uses the Responses API with the workflow model at low effort and
  fails open on errors.
- **`scheduled_reminder` recipe**: a text at a wall-clock date and time, optionally
  repeating every N days/weeks/months/years (`count`, `until`, or an open series capped
  at 10 years). Occurrences are computed with calendar months when the plan is built;
  each gap is one durable Trama sleep.

- **Agent-mode generation** (`RITESMITH_GENERATION_MODE=agent`; the default stays
  `proposal`).
  - The model works in a bounded tool loop over the Responses API.
  - It finds capabilities and their closed contracts, and lists devices with state
    examples, IR buttons and virtual people/trackers.
  - It probes read-only capabilities with `try_capability` (at most 6).
  - It builds recipes, validates semantic plans, simulates them offline with a virtual
    clock, and checks Luau scripts against its own cases.
  - It submits only what re-validates.
  - Instructions and tool definitions are request-independent, so the prompt cache
    serves them; about 84% of input tokens are cached.
  - New settings: `generation_agent_max_turns` (12), `generation_agent_turn_tokens`
    (8000), `local_timezone` (UTC).

  On a 23-request home-automation subset:

  | Run | Checks passed | Judge (1–5) | p50 | Total cost |
  |---|---|---|---|---|
  | Proposal mode, gpt-5.5 | 10/23 | 1.82 | 55 s | US$ 3.73 |
  | Agent mode, gpt-6.1-terra | 22/23 | 4.35 | 21 s | US$ 0.34 |

  The prompt rules were tuned on that subset's failures.
- **Recipes:** `context.template = {name, params}` builds a verified workflow without a
  model call. Params are validated against closed models, and every polling recipe ends at
  a deadline. The presence reminder now lives in the same registry
  (`context.presence_reminder` still works). The recipes are:
  - `threshold_action`: a numeric, boolean or text state crosses a limit; an optional
    start delay.
  - `price_variation`: a crypto or fiat price moves ±N%.
  - `presence_change`: someone arrives or everyone leaves, after a real transition, with
    an optional delay and up to 6 actions.
- **`GET /plans/{plan_id}/transcript`:** every agent turn (tool, arguments, result,
  tokens, duration) is stored as an audit event. A failed generation commits its
  transcript before the 422, which carries the `plan_id`.
- **Agent prompt rules:**
  - Deliverables go through their capability (`report.write_html`, `telegram.send`),
    never as returned text.
  - Virtual devices that report offline still count.
  - When the request names a kind of device and several devices of that kind match, the
    agent refuses and lists the options instead of picking one.

### Fixed
- `context.presence_reminder` sent together with schedule fields (`schedule_hours`,
  `first_at`, `repeat`, …) or a `template` is now refused. It used to become an arrival
  reminder silently, dropping the date and the repetition.
- **A model-shaped plan could exhaust host memory.** Validate, compile, simulate and
  `check_script` now run in a spawned worker. It has a soft `RLIMIT_DATA`
  (`generation_agent_tool_memory_mb`, 512) and a timeout
  (`generation_agent_tool_timeout_seconds`, 30). A runaway becomes a tool error and the
  event loop never blocks. Simulation is capped at 2000 steps.
- **Scripts passed `check_script` and then failed the pipeline's size limit.**
  `check_script` now applies the same static gates first: profile tools, forbidden
  tokens, size.
- **Workflows returned without the response envelope were rejected.** The bare candidate
  is now unwrapped.
- **Fixtures matched tool arguments case-sensitively** (`BRL` vs `brl`). Matching now
  ignores case.
- **The simulator evaluated both sides of `and`/`or`.** It now short-circuits like
  JSONLogic, so a null guard protects the comparison after it.
- **Traces from newer models reported no cost.** Prices per 1M tokens (input / cached /
  output):
  - gpt-5.5: $5 / $0.50 / $30
  - gpt-6.1-terra: $1 / $0.05 / $5
  - gpt-6.2-sol: $2 / $0.10 / $10
  - gpt-5.3-codex: $1.75 / $0.175 / $14

### Changed
- `openapi.json` / `openapi.yaml` regenerated (they predated 0.2.0 and lacked
  `/plans/{plan_id}/transcript`).
- **Default fast model is now `gpt-4.1-mini`.** OpenAI retired `gpt-4.1-nano` (`404
  model_not_found`), which broke `analyze_intent` and `llm.evaluate`. `gpt-4.1-mini` is
  non-reasoning, as `llm_model_fast` requires. It is priced in `observability/costs.py`
  and allowed by the spend budget. The preserved benchmark baselines still name nano.

## [0.2.1] - 2026-10-11

Capability contracts are closed end to end. A workflow that passes an argument or reads a
field its capability does not declare is rejected at generation (and repaired), not
accepted and then broken in Trama. Requires **Trama 3.0.1**.

### Fixed
- **Workflows accepted with arguments or fields their capability does not have.** The
  validator ignored unknown inputs and output fields unless a schema set
  `additionalProperties: false`, which none did. Two r2d2 workflows failed this way
  (`previous_state` passed to `llm.evaluate`; `output.result` read from
  `market.exchange_rate`, whose field is `rate`). Unknown inputs and fields are now errors
  that list what the contract accepts; array and scalar outputs are read as `output.result`.
- **`/trama/execute` returned 500 for bad arguments.** It now checks the input against the
  capability contract and returns **422** `invalid_input` (unknown/missing keys, or a
  `TypeError` from the callable), which Trama does not retry. `report.*` single-argument
  functions are called correctly.
- **The capability registry stored empty schemas** for every provider capability, so
  `/capabilities` and the MCP listing showed no contracts. Real input/output schemas and
  descriptions are stored and refreshed on startup.
- **Provider schemas that did not match the code.** 17 output schemas described an object
  where the function returns a list, string or number (calendar, email, obsidian, duckdb,
  runtime, web.fetch_json), and 5 input schemas differed from the callable
  (`obsidian.read` takes `relative_path`; calendar `target`/`targets`; `telegram.send`
  `parse_mode`). The contract test now checks both directions and the output type.

### Changed
- **Requires Trama 3.0.1.** Trama ≤ 3.0.0 drops the result of a node that runs right after
  a `switch` once the execution resumes. Semantic repeats with a condition hit this on
  their first resumed pass (thiagoribeiro/trama#56).
- Workflow prompts state the closed-contract rule and turn numeric thresholds into
  `condition`s instead of `llm.evaluate` calls; repair receives the contracts of the
  capabilities the failed candidate used.
- Benchmark grader v3 checks args and references against the contracts. Regraded, the
  0.2.0 configuration was 67% valid workflows (not 77%); 0.2.1 measures 68% workflows and
  96% Luau scripts with **zero contract violations** in accepted workflows.

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
