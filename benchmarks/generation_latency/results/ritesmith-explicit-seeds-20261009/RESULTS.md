# RiteSmith explicit initialization: offline integration report

## Outcome

RiteSmith now prepares workflows for Trama's mandatory typed JSON rendering.
The first iteration has explicit counter and state values; later iterations read
completed results. A continuation preserves remaining work, state and its original
deadline. No rendering-mode header is emitted or required.

**Final local regression: 951 passed, zero unexpected failures, 3 skipped,
5 expected failures; 14 live E2E tests excluded.** Lint, formatting and diff
whitespace checks passed. There were no paid provider requests, new benchmarks
or deployments. This result verifies offline behavior; it does not measure LLM
generation accuracy or prove the 95% / 10-second or 50% cost targets.

## What changed

- Semantic repeats expand deterministically into at most twenty business passes
  per graph. Explicit `initial_state` replaces references to nonexistent first
  results. Counters do not depend on missing values becoming null.
- Finite continuation carries the remaining count. Duration continuation carries
  the original deadline. Required retained state cannot silently reset. Final
  actions also work when the deadline expires before all expanded passes execute.
- Seed data and cached plans stay literal, including template-like strings and
  compiler-reserved keys. JSON scalars, null and top-level `value` fields preserve
  their transport meaning.
- The nine example patterns use the shared compiler, and prompts explain explicit
  initialization. Nullable minimum/maximum contracts match the existing runtime
  implementations. Internal response transport supports initial state.
- JSON preparation removes the obsolete mode header. The old configuration key
  is accepted but has no effect. Invalid source templates and unavailable body
  references are rejected with diagnostics before acceptance, including advanced
  compact graphs.

Public HTTP/MCP artifact formats, generation models, effort, approval/persistence
behavior and experimental feature defaults were not promoted or changed.

## Evidence and limits

| Check | Result |
|---|---|
| All non-E2E RiteSmith regression tests | 951 passed; 0 failures/errors |
| Initialization and native transport modules | 51 passed; none skipped |
| Weekly continuation | 28 samples at six-hour intervals: 20 + 8; retained minimum and final action |
| Early deadline and zero remaining work | Final actions use the available retained/completed state |
| Cached compatible continuation | Local compilation; no LLM call |
| Incomplete continuation | Rejected instead of restarting work or state |
| Typed body behavior | Actual compiled Trama renderer and JSONLogic, controlled tool responses |
| Patterns and dependent plans | Nine patterns, parallel aggregation, callbacks, compensation and certified Luau dependencies checked offline |
| Lint / formatting / whitespace | Passed |

The three skips are existing shell-endpoint and Lua timeout exclusions. The five
expected failures are existing Obsidian/calendar contract mismatches and callback
blocklist gaps. They remain unresolved. No new native integration check skipped.
The existing `slowapi` deprecation warning remains.

The Java oracle executes Trama's renderer and JSONLogic. A local simulator supplies
control flow, a virtual clock and controlled tool results. This is stronger than
Python-only template simulation, but it does not exercise a deployed distributed
Trama executor or make a new paid model-generation claim.

Bounded expansion increases the native graph size. No new measurement establishes
its latency, storage or cost tradeoff under normal requests. Previously frozen
generation reports and original provider responses were not rewritten.

## Reproduction and source identity

- RiteSmith base commit: `4b6d3ffae7852fae599320120872f55e6c958e23`.
- Corrections are in the **uncommitted working tree**, alongside earlier work;
  the base commit alone does not reproduce this result.
- Trama: `fa3aef0346cf2b1a7e8e075dfc470e9239a34eb8`,
  [PR #54](https://github.com/thiagoribeiro/trama/pull/54); source unchanged this run.
  Its existing branch was rebuilt locally for the oracle.
- Versions: `generation-v6`, `typed-response-v2`, `pattern-parameters-v2`;
  semantic format `workflow-plan-v1` retains an optional initial-state field.
- [verification.json](verification.json) records source and native artifact hashes.
- [regression.log](regression.log) and [regression.xml](regression.xml) preserve
  final output. [attempts](attempts/) preserves earlier checks, including failures
  corrected before the final run; not every intermediate run passed.

Use an isolated disposable PostgreSQL database through `TEST_DATABASE_URL`, not
production storage. Compile `TramaTypedJsonOracle.java` with `javac --release 21`
against the exact candidate engine libraries; set `TRAMA_ORACLE_CLASSPATH` to the
probe and engine library classpath. Then run:

```sh
.venv/bin/pytest ritesmith/tests -q -m 'not e2e' --junitxml=regression.xml
.venv/bin/ruff check .
.venv/bin/ruff format --check .
git diff --check
```

## What remains

Review and version these RiteSmith changes with the compatible Trama change.
Production and rollback images/configurations remain unchanged; deployment is a
separate decision. The stopped paid campaign cannot be resumed as passing evidence.
Any further paid verification needs authorization for the corrected revision and
must qualify actual generation, not just these controlled offline references.

The integration is ready for review. Production generation reliability, the latency
target and cost savings remain unconfirmed for this corrected configuration.
