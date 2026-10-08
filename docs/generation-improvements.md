# Generation improvements

The objective is 95% complete, valid deliveries within 10 seconds and at least
50% lower cost per valid principal artifact than the preserved baseline.
Dependencies, failures, repair, tests and requested persistence count. Ten seconds
is an SLO; the default request deadline is 30 seconds. This implementation has
not been qualified against that SLO. No new paid evaluation or benchmark campaign
was performed. The owner subsequently authorized a direct r2d2 update with
rollback; see [the deployment record](generation-deployment-r2d2.md).

## Request preparation and model calls

Clear script requests use Luau rules only; clear workflow requests use Trama
rules only. Ambiguous/composed requests retain a combined analysis and proposal.
Explicit artifact types bypass classification. `/generate` still returns one
artifact; `/plans` can materialize script dependencies.

A complete capability inventory accompanies selected full contracts. Literal
names, explicit requirements and confident local matches select contracts.
Ambiguity sends all contracts. The complete registry remains authoritative at
validation. Schemas and complete example code are never truncated. Request-local
recall and registered capability preparation are shared across services.

Scripts remain on `gpt-5-mini low`. The r2d2 deployment now selects
`gpt-5.4-mini low` for workflows through environment overrides. Repository
defaults remain independently configurable; the deployment record includes rollback.
Input/output headroom remains available, including reasoning tokens.

## Recovery and deadlines

Defaults allow one candidate and one recovery per artifact, plus one independent
test-generation call per script. Transport retries consume the same recovery as
repair/fallback. The SDK has automatic retries disabled. Quota exhaustion and
nonretryable failures stop immediately. Repairs use the primary model; a missing,
unusable candidate may use the configured fallback. Repeated candidates and
diagnostics stop recovery. Fixture failures never trigger code repair.

Nested services, independent tests and persistence share a monotonic deadline.
Recovery admission reserves at least three seconds and the observed duration of
the preceding generation call when available. This is an estimate, not a promise
that the next call will finish in that time. A timeout is a failed delivery.
Late valid artifacts remain SLO violations. `generation_bounded_recovery=false`
restores configurable attempt limits and legacy provider retries.

## Independent tests and LunarDyson

`context.test_cases` retains the existing input/expected-output representation
and accepts optional `context`, `assertions`, `tool_fixtures`, `expected_calls`
and `source` (`client`, `fixture`, `intent`). Input-only cases are execution smoke
checks and do not satisfy a required functional test gate.

`context.test_fixtures` supplies known tool responses to the independent test
generator. It receives intent, schemas and tool contracts, never generated code.
Absent known data, it defines controlled synthetic fixtures before deriving
expectations. Supplied fixture records override synthetic responses for the same
tools; unrecorded arguments remain unknown. Schemas, expected IDs, notification
counts and call counts are checked before execution.

Example:

```json
{
  "test_cases": [{
    "input": {"segment": "dormant"},
    "expected_output": {"notified": ["c1"], "failed": [], "count": 1},
    "tool_fixtures": [
      {"tool": "crm.list", "args": {"segment": "dormant"},
       "output": {"items": [{"id": "c1"}]}, "times": 1}
    ],
    "expected_calls": {"crm.list": 1},
    "source": "client"
  }]
}
```

Fixture names/outputs must match the actual profile's contracts. All external
calls require fixtures. A missing response is a fixture failure, never a live
call. Deterministic text/stat helpers can execute locally. Each case resets
fixture usage and call counters. Checking and execution share an isolated
LunarDyson session, with CPU, memory and tool-call bounds. Normal execution
runtimes remain separate. Empty tables become arrays only where the JSON schema
requires arrays; object fields retain their original representation.

Assertions use `{path, op, value}`. Supported operations are `equals`, `length`,
`max_length`, `contains`, and `subset`. Paths address output fields/array indexes.
Expected call counts are additional checks, not a replacement for output evidence.

With `generation_luau_assembly=true`, an internal candidate can return
`script: {format: "luau_body", helpers, body}`. Python supplies the strict `run`
signature; existing predeclared types remain in scope. Complete script text is
still accepted. Both forms undergo the same validation and certification.

## Semantic Trama plans

With `generation_semantic_workflows=true`, an internal definition may be:

```json
{
  "format": "semantic_plan",
  "plan": {
    "version": "workflow-plan-v1",
    "name": "three_prices",
    "steps": [{
      "kind": "repeat", "id": "monitor", "count": 3,
      "interval_seconds": 60,
      "steps": [{"kind": "call", "id": "fetch",
                 "capability_name": "market.coin_price",
                 "args": {"symbol": "btc"}}]
    }]
  }
}
```

Operations support calls, sequences, waits, conditions, parallel branches, state
helpers, callbacks, compensation and repetition. References use
`{ref: "operation_id", path: "output_field"}` or payload/runtime/callback roots.
The compiler constructs HTTP actions, links, counters, joins and continuation.
Public responses and stored artifacts remain native Trama JSON.

Repetition requires exactly one of count, duration, continuous mode or an absolute
deadline. Counts decrement across chunks of at most 20 samples. Duration starts
at execution and becomes an absolute deadline, preserved across continuations.
Expired deadlines stop before business actions. The first sample is immediate;
intervals separate samples and chunks. `stat.collect` retains finite samples;
`stat.chain_step` progresses counters and carries state.

The nine catalog patterns have semantic constructors/examples. Patterns can
compose conditions, state and parallel actions inside a repeated body. Nested,
multiple or branch-local repetitions, and one-time prelude actions before a
chunked monitor, use the existing `compact_graph` escape:
`{format: "compact_graph", graph: {name, entrypoint, nodes, ...}}`. This is chosen
in the same model response. Requirements must never be discarded to fit a pattern.

Each parallel branch sees only payload and its own nodes. The parent reads only
terminal branch results through the join. Conditions and all generated references
are validated. Semantic validation also checks reachability, literal input
contracts and counter resets. The offline simulator supports the generated
control-flow subset with explicit responses and a virtual clock; it is not a full
Trama runtime replacement.

Compatible continuation context recompiles the semantic plan without an LLM.
Changed intent or contract/version information uses ordinary generation. Script
IDs are bound after validation, including references embedded in continuations,
and rebound when artifacts are persisted. Completion is injected before final
validation, after the parent flow rather than inside parallel branches.

Mermaid remains experimental and disabled by default. Supported visual labels
can be quoted or unquoted. Semantic JSON annotations and explicit edges remain
mandatory. Unsupported syntax reports its line; invalid candidates are retained
for targeted repair. No general Mermaid syntax support or performance advantage
is claimed.

## Configuration, telemetry and verification

Independent controls: typed prompts, Luau assembly, semantic workflows, compact
workflows, Mermaid and bounded recovery. See `.env.example` and the isolated
`deploy/generation-staging.env.example` overlay. Schema/fixture checks remain
active when format flags are rolled back.

Telemetry records model/effort, provider usage/cache, actual API attempts,
prompt/contract/format versions, stage duration, diagnostics, fallback, client
cases and dependencies. MCP bridges label generation self-calls with a fixed
interface header; telemetry uses `mcp:/generate` and `mcp:/plans` separately from
the HTTP entrypoints. Each transport attempt is accounted for separately.
Cancelled calls or missing usage make the request cost unknown. Estimated USD
uses the historical price snapshot from the preserved baseline, explicitly
versioned; it is not an invoice or a claim of current account pricing.

Offline verification covers fixture oracle failures, strict checking, assembly,
parallel scope, finite/indefinite chains, duration/state, callbacks, compensation,
recovery/deadline behavior, public contracts and existing regressions. It makes
no new latency or cost qualification. The owner authorized direct deployment on
the single-user r2d2 host. Ordinary traffic supplies latency and cost evidence by
interface/artifact/test/dependency group. Groups without a valid baseline cannot
claim a percentage cost reduction.

## Verification record — 2026-10-08

Local execution used Python 3.14 and the x86_64 LunarDyson wheel, with a dedicated
PostgreSQL database on r2d2. The database and role were created only for these
checks and removed afterward. Production services/configuration were not changed
during that verification stage; the subsequent direct deployment is recorded separately.

- Full non-E2E regression: **815 passed, 3 skipped, 5 expected failures**;
  14 E2E cases were deselected. This run preceded the final MCP instrumentation
  and notification fixture additions.
- Final affected-service selection (`test_generation_improvements.py`,
  `test_generation_latency.py`, `test_workflow_engine.py`): **136 passed**.
- Final pure validation selection after compatibility checks for generated ID
  transformations and optional notification counts: **44 passed**, 6 deselected.
- Ruff lint, Ruff formatting and `git diff --check`: passed.

Counts overlap and must not be added together. The tests use scripted model
responses, explicit tool fixtures and simulated clocks; none invokes a paid LLM
or measures provider latency. SlowAPI emitted existing Python 3.14 deprecation
warnings. These results establish offline regression coverage, not achievement
of the 10-second delivery or 50% cost-reduction targets. Subsequent strict ARM
runtime and deployment checks are recorded in the r2d2 deployment note.
Ordinary-traffic performance remains to be observed.
