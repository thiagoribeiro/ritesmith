# Generation latency evaluation

Current target: at least 95% of complete, valid artifacts within **10 seconds**
and **at least 50% lower estimated cost per valid artifact than baseline**.
Errors, repairs and fallback count in both time and cost. Unknown billed usage
cannot pass the cost gate. Validation now uses a representative subset; the
original full matrix and its 5-second target were superseded at the user's request.

## Bounded validation (current protocol)

`subset.py` makes at most **43 new HTTP requests**, sequentially. It reuses the
preserved four-model comparison and evaluates only `gpt-5-mini` and
`gpt-5.4-mini`, both low, for the new compact/Mermaid comparison. Three workflow
scenarios (reminder, bounded week/continuation and parallel prices) receive
three repetitions across the three HTTP interfaces. Existing script triage
(Celsius, percentage change and notifications) is reassessed under the new
latency/cost gates without new model calls. One dependent plan and one missing
caller-test request are smoke cases with fresh, matching baseline requests.

Cost is total estimated token cost **including failed attempts and repairs**
divided by valid artifacts, not cost per successful API call. Compare matching
tasks, artifact type, path, concurrency and caller-test groups. Source revisions
and cache tokens are retained. No matching baseline, zero valid artifacts, or
unknown cost prevents qualification. Small groups and single-run smoke cases
do not establish a full-corpus/production SLO. No automatic promotion occurs.

```bash
python -m benchmarks.generation_latency.subset \
  --baseline-directory results/campaign-final-code-20261008 \
  --target-seconds 10 --cost-reduction 0.5 \
  --output results/subset-10s-cost50-20261008
```

The corpus contains the 31 existing Luau tasks and 20 Trama scenarios. Six Luau
and four workflow cases are held out from prompt tuning. Workflows cover every
example pattern, combinations of patterns, and artifact references. The separate
compound-plan regression verifies that scripts are validated before workflows
and that persisted IDs, contracts, certification and completion callbacks survive.
Two additional held-out compound-plan scenarios generate a new script and bind
the returned workflow to its persisted ID; these run only through `/plans`.

## Configuration and rollback

| Setting | Default | Effect |
|---|---|---|
| `RITESMITH_LLM_MODEL` | `gpt-5-mini` | Original model and fallback |
| `RITESMITH_LLM_SCRIPT_MODEL` | inherit | Script generation/repair |
| `RITESMITH_LLM_WORKFLOW_MODEL` | inherit | Workflow generation/repair |
| `RITESMITH_LLM_REASONING_EFFORT` | `low` | Generation/repair effort |
| `RITESMITH_LLM_SCRIPT_EFFORT` | inherit | Script override |
| `RITESMITH_LLM_WORKFLOW_EFFORT` | inherit | Workflow override |
| `RITESMITH_LLM_FALLBACK_ENABLED` | `true` | Retry with original model |
| `RITESMITH_GENERATION_SPECIALIZED_PROMPTS` | `true` | Select short examples locally |
| `RITESMITH_GENERATION_COMPACT_WORKFLOWS` | `true` | Generate typed shorthand and compile |
| `RITESMITH_GENERATION_SEMANTIC_WORKFLOWS` | `true` | Semantic plans compiled to Trama; `false` falls back to compact graphs |
| `RITESMITH_GENERATION_DEADLINE_SECONDS` | `60` | Whole-request generation deadline, including recovery |
| `RITESMITH_GENERATION_UNIFIED_PROPOSAL` | `true` | Intent and first candidate in one call |
| `RITESMITH_GENERATION_PARALLEL_TESTS` | `true` | Independent tests alongside candidate |
| `RITESMITH_GENERATION_AVOID_SPECULATIVE_WORKFLOW_TESTS` | `true` | Defer Lua tests for clear workflow intents; scripts still get required tests |
| `RITESMITH_GENERATION_LATENCY_TARGET_SECONDS` | `10` | Configurable HTTP telemetry target |

Disable switches individually for ablation or rollback. To reproduce omitted
effort, set `RITESMITH_LLM_REASONING_EFFORT=null`; `null` is parsed as Python None.
Keep per-operation effort overrides unset when rolling back the shared effort.
No winning model is hardcoded before qualification. A local model hint chooses
the script model for explicit code/contracts and the workflow model otherwise;
the unified response still decides the required artifact types.

## Pass-rate scorecard

`scorecard.sh` measures the checked-out revision with its shipped defaults through
`/generate`: a 6-request smoke stage, then all 51 tasks × `REPS` after confirmation.
Each stage has a hard estimated-spend cap (`run.py --max-usd`): before every
request the worst observed request cost is reserved, and unknown usage is charged at
that reservation. It needs `OPENAI_API_KEY`, podman (disposable Postgres) and
exposes Telegram with placeholder settings, since capabilities are only listed to the
model when their provider is configured; it aborts if a required provider is missing.

```bash
OPENAI_API_KEY=... SMOKE_USD=0.30 FULL_USD=2.00 REPS=3 \
  benchmarks/generation_latency/scorecard.sh
python -m benchmarks.generation_latency.scorecard results/<run>/full.jsonl
# Map newer native fields (durationMillis, async callbacks) to the v2 grader's names:
python -m benchmarks.generation_latency.regrade results/<run>/*.jsonl --exclude wf_artifact
```

`wf_artifact` needs `art_benchmark_normalize` to exist in the registry and is
excluded until the harness seeds it. The v2 grader still requires `stat.tick` and
`max_iterations == 20` for some scenarios, which semantic plans express differently.

2026-10-10 results (gpt-5-mini low, N=3, `wf_artifact` excluded):

| Configuration | Luau valid | Workflow valid | Workflow p50 / p95 |
|---|---:|---:|---:|
| PR #6 head, compact graphs | 86% | 56% | 13.5 s / 30.0 s |
| Primitive micro-examples (compact) | — | 53% | 15.2 s / 29.9 s |
| Placeholder/null fixes | 89% | 63% | 10.8 s / 51.1 s |
| Strict semantic plans + compiler fixes | 94% | 77% | 7.3 s / 17.4 s |

Grader v3 (2026-10-11) also checks task args and output references against the closed
provider contracts. Regraded, the 0.2.0 row is **67%** valid workflows: its extra failures
read `output.now` from `runtime.now`, a field the bridge never returns. 0.2.1 (closed
contracts in validation, bridge and registry; corrected provider schemas):

| Configuration (grader v3) | Luau valid | Workflow valid | Workflow p50 / p95 | Contract violations accepted |
|---|---:|---:|---:|---:|
| 0.2.0 | 94% | 67% | 7.3 s / 17.4 s | 6 workflows |
| **0.2.1** | **96%** | **68%** | **8.5 s / 22.0 s** | **0** |

Evaluator version 2 accepts `market.bitcoin_price` for BTC-only requests and
detects cyclic counters that reset instead of advancing. The original campaign
used version 1. `review.py` writes a separate assessment with hashes of the raw
files and each changed verdict; it never rewrites responses or timings. Results
must identify their evaluator version. These checks remain a limited semantic
assessment; full live workflow execution is not certified by this harness.

## Isolated r2d2 evaluation (extended protocol available)

`benchmarks.generation_latency.app:app` is a staging-only entrypoint. Run it in a
separate container/database bound to loopback; never import it in production.
The staging entrypoint disables the production HTTP rate limiter to run the
controlled matrix; this does not establish production throughput under that limiter.
It calls the production HTTP services and OpenAI provider with the same pooled
client. It preserves database search timing but withholds retrieved artifacts
from reuse and few-shot prompts. Workflow provider contracts remain complete.

Scripts use production LunarDyson with the original corpus's pure tool stubs.
Type checking, functional expected outputs, call budgets, forbidden tokens and
production size/run-function checks are enforced. The reference self-check is:

```bash
python -m benchmarks.generation_latency.native
```

No production tool or workflow is executed. Workflow acceptance checks validate
the graph and explicit semantic requirements, including schedules, split/join,
continuation, artifact binding, callbacks and compensation. They do not constitute
a live Trama integration test of external services. Native stub overhead and
validation duration are reported separately; external tool latency is outside
this substituted-tool qualification and needs separate production observation.

By default the caller supplies test inputs; expected outputs and call budgets
remain hidden in the evaluator. Use `--missing-client-tests` for a group that
also includes independent LLM test generation and checks those generated cases.
Do not combine the two groups when claiming an SLO.

The first repetition has a unique system prefix to prevent provider prompt-cache
hits. Subsequent repetitions allow normal caching. Actual `cached_tokens` are
recorded, and cached/uncached results are reported separately. Artifact reuse and
few-shot reuse are disabled for every repetition. Concurrency is one or two
complete HTTP requests; unrelated runs must not overlap.

```bash
# Server in the separate container, after its DB migrations:
uvicorn benchmarks.generation_latency.app:app --host 0.0.0.0 --port 8081

# 3 repeats per representative scenario, all models, all four ablations.
# Then best 2 models per kind on the full corpus, 5 repeats, concurrency 1/2.
# Includes the full comparable baseline, compound plans and missing-test groups.
python -m benchmarks.generation_latency.campaign --output results/campaign

# Explicit full evaluation; entrypoints rotate across the five repetitions.
python -m benchmarks.generation_latency.run --phase full --model gpt-5.4-mini \
  --tasks all --repetitions 5 --concurrency 2 \
  --entrypoints specialized generate plans --rotate-entrypoints \
  --output results/full-mini-c2.jsonl

# Missing-test path, including its model call and validation in elapsed time.
python -m benchmarks.generation_latency.run --phase full --model gpt-5.4-mini \
  --tasks triage --kind luau_script --repetitions 5 --missing-client-tests \
  --entrypoints specialized generate plans --rotate-entrypoints \
  --output results/missing-tests.jsonl

python -m benchmarks.generation_latency.run --phase full --model gpt-5.4-mini \
  --tasks compounds --repetitions 5 --entrypoints plans --concurrency 2 \
  --output results/compound-plans.jsonl
```

Rotating entrypoints means **five repetitions per scenario**, distributed across
the three interfaces; it does not mean five repetitions per interface. Omit
`--rotate-entrypoints` for five per scenario **per interface**. The report always
keeps those groups separate. Failed calls are retained, including timeouts and
fallback. Interrupted runs resume recorded attempts instead of retrying failures
to improve the score. Summaries use the empirical nearest-rank p95.
Account-quota exhaustion stops pending requests, preserves the failed response,
and writes a partial summary instead of marking the arm complete. Completed
requests are never rescored in place. A source change requires a fresh campaign
directory; preserve the older run, including its errors, for comparison.

Stages are `baseline` (original generation prompt/default effort), `model` (only
model/low), `filtered` (short selected examples in native JSON), and `full`
(compact catalog/graph and unified proposal). The frozen baseline user prompt
preserves its old first-25 slice; production uses the complete deduplicated
catalog. The shared HTTP/storage/runtime infrastructure is the candidate code in
all arms, so this isolates LLM/prompt effects, not the older client's TCP overhead.

Candidate ordering requires triage validity at least as high as baseline, then
p95, then success rate, then estimated cost. Standard token prices are recorded
in `pricing.py` with a verification date and the [official model catalog](https://developers.openai.com/api/docs/models).
Cost estimates include cached input and completion/reasoning tokens; account
discounts, tax and service-tier premiums are excluded. Finalists are evaluation
candidates, not deployment recommendations. The campaign never promotes a model.

Inspect `report.json`, raw JSONL, per-arm summaries and `finalists.json`. Invalid
responses and every response at/over 5 seconds appear in the failure list. Record
the software revision, effective settings, runtime version and host before any
production qualification. Do not promote if any required group is below 95% or
functional/strict certification rates regress relative to a comparable baseline.
Each new response records the staging software hash. Capture its full source
and runtime manifest with `python -m benchmarks.generation_latency.version`.

The r2d2 evaluations and the reduced protocol are documented in [RESULTS.md](RESULTS.md).

## Monitoring after promotion

Collect `ritesmith_generation_target_requests_total` and
`ritesmith_generation_duration_seconds` per entrypoint/type for seven days.
Request logs retain each call's model, effort, actual provider model, API attempts,
tokens, reasoning tokens, cached tokens, duration, selected examples and fallback,
plus stage durations. Generation jobs store actual repair-loop attempts and audit
timestamps. A fallback's duration remains in the complete response time.

```promql
# Valid within ten seconds, including invalid responses in the denominator:
sum by (entrypoint, artifact_type) (
  increase(ritesmith_generation_target_requests_total{target_seconds="10",outcome="valid_within_target"}[7d])
) / sum by (entrypoint, artifact_type) (
  increase(ritesmith_generation_target_requests_total{target_seconds="10"}[7d])
)

histogram_quantile(0.95, sum by (le, entrypoint, artifact_type) (
  rate(ritesmith_generation_duration_seconds_bucket[1h])
))
```

Also compare certification, functional test results, fallback and token cost
from audit/log records. Require cost per valid artifact at most half of the
matching baseline. The fixed-five-second counter remains available for historical
comparison. A deploy followed by seven days of observation is a
separate operational step; starting an evaluation does not satisfy that step.
