# r2d2 evaluation — October 8, 2026

Implementation and the reduced evaluation were completed. The target is **95%
of complete, valid deliveries within 10 seconds, with estimated cost per valid
principal artifact at least 50% below baseline**, including dependencies, errors
and repairs. **The combined target was not met in every group. No promotion
occurred at this evaluation stage.**

The owner subsequently authorized a direct deployment of further improvements;
see [the deployment record](../../docs/generation-deployment-r2d2.md). That update
ran no new benchmarks or paid LLM generation calls. The measurements below remain
the preserved evaluation evidence, not measurements of the newer implementation.

At the user's request, the extensive matrix and subsequent queue were stopped.
463 complete responses from the campaign after credits were replenished were
preserved. One interrupted request may have unrecorded usage and is not counted
as a success. The [earlier campaign without credits](RESULTS-QUOTA.md) is also preserved.

The reduced round made **43 HTTP requests, 65 LLM calls and an estimated cost of
US$0.2674**, without another extensive campaign. It reused the four-model triage
and compared only `gpt-5-mini` and `gpt-5.4-mini`, both low, in new calls.
It tested three workflows, all three interfaces, a dependent plan and a request
without caller-supplied tests. All new requests were sequential.

## Main result

Scripts with caller-supplied tests: preserved `gpt-5-mini low` triage achieved
**9/9 valid deliveries within 10s, p95 6.58s and 66.9% lower cost** than baseline
for the same scenarios. These were Celsius, percentage change and notifications
through the specialized endpoint; they do not qualify every script or requests
without supplied tests. The other models did not meet both script triage criteria.

Workflows: three scenarios (reminder, weekly monitoring/continuation and parallel
prices), with three repetitions distributed across interfaces. Each row below
contains three responses, one per scenario. Costs use baseline for matching
scenarios, artifact type, interface, concurrency and caller-test availability.

| Model / format | Interface | p95 | Valid within 10s | Cost reduction | Met both criteria |
|---|---|---:|---:|---:|---|
| gpt-5-mini / compact | `/generate` | 14.27s | 2/3 | 87.0% | No |
| gpt-5-mini / compact | `/plans` | 13.06s | 2/3 | 77.8% | No |
| gpt-5-mini / compact | specialized | 16.03s | 2/3 | 63.9% | No |
| gpt-5.4-mini / compact | `/generate` | 8.92s | 3/3 | 71.7% | Yes, in this small group |
| gpt-5.4-mini / compact | `/plans` | 16.71s | 2/3 | 43.0% | No |
| gpt-5.4-mini / compact | specialized | 7.79s | 3/3 | 40.2% | No |
| gpt-5-mini / Mermaid | `/generate` | 30.16s | 1/3 | 79.3% | No |
| gpt-5-mini / Mermaid | `/plans` | 27.31s | 1/3 | 38.7% | No |
| gpt-5-mini / Mermaid | specialized | 34.81s | 1/3 | 47.2% | No |
| gpt-5.4-mini / Mermaid | `/generate` | 6.20s | 3/3 | 74.5% | Yes, in this small group |
| gpt-5.4-mini / Mermaid | `/plans` | 14.84s | 2/3 | 44.5% | No |
| gpt-5.4-mini / Mermaid | specialized | 17.50s | 2/3 | 23.0% | No |

Mermaid improved `/generate` with `gpt-5.4-mini`, but showed no consistent advantage
across interfaces/models. With `gpt-5-mini`, parsing failures caused more fallback.
The option remains experimental and disabled by default.

## Cases that prevented qualification

- Weekly monitoring with compact JSON and `gpt-5-mini`: 13.06–16.03s.
- Parallel prices in `/plans` with `gpt-5.4-mini`: 16.71s compact and 14.84s Mermaid.
- Specialized weekly monitoring in Mermaid with `gpt-5.4-mini`: 17.50s after fallback.
- Mermaid with `gpt-5-mini`: weekly monitoring reached 34.81s; one `/plans` response failed with HTTP 502.
- Notification script **without caller-supplied tests**: 46.14s, invalid, after repairs.
  Generated expectations referred to segments/customers absent from the substitute
  tool's responses. Independent test generation did not see the implementation;
  the program was repaired against those expectations. This path regressed against
  a valid 12.24s smoke baseline and prevented qualification.
- Specialized `gpt-5.4-mini` compact: latency passed, but cost reduction was 40.2%.

Dependent plans are separate smoke cases: `gpt-5.4-mini` compact/Mermaid returned
a certified script and linked workflow in 3.74/3.95s. With `gpt-5-mini`, they took
12.86/13.64s. Baseline did not generate the required dependency, so no baseline
cost per valid delivery exists to substantiate a percentage reduction for this
scenario. No failed request was repeated to improve the score.

## Implementation and verification at evaluation time

Per-operation models/effort, a shared client, locally selected examples, a full
catalog, typed compact graphs, deterministic Mermaid compilation, unified
proposals, shared recall and independent tests were implemented. Configuration
switches support comparison and rollback. Public contracts were preserved;
`context.workflow_examples` documents example selection.

Clear workflow requests no longer start Lua tests that would be cancelled.
Scripts, including ones initially interpreted as workflows, retain required tests.
Telemetry supports a configurable 10s target; the fixed 5s counter remains for
historical comparison. Unknown cost cannot pass the cost-reduction gate.

The nine Mermaid patterns passed Trama equivalence and the official Mermaid
11.12.0 parser. The previous regression had 759 passed tests, three skips and five
expected failures. After cost changes, 95 targeted generation/Mermaid/cost tests
passed, followed by six final cost tests. Lint and formatting passed.
[Verification](results/verification.json).

The [local microbenchmark](results/formats-micro-local.json) measured conversion
plus structural-validation p95 below 0.2ms. Mermaid used more bytes than compact
JSON for the examples. This does not prove LLM speed or quality.

## Evidence and limitations

- [Final report: groups, costs, cache and every failure](results/subset-10s-cost50-20261008/report.json).
- [Versioned review of preserved baseline](results/subset-10s-cost50-20261008/preserved-review-v2.json).
- [r2d2 code/runtime manifest](results/subset-10s-cost50-20261008/code-manifest.json).
- [Extensive campaign interrupted by the scope change](results/campaign-final-code-20261008/report.json).
- [Reduced protocol and rollback](README.md).

Evaluator v2 accepts `market.bitcoin_price` for BTC-only requests without accepting
it for ETH, and detects loops whose counters reset. The review records hashes and
each changed verdict without overwriting responses/timings. Cost analysis keeps
dependent plans separate from ordinary workflows.

The evaluation used a separate r2d2 instance/database and preserved production.
LunarDyson used substitute tools. Workflows had structural and limited semantic
validation without executing external effects. The HTTP limiter was disabled only
in staging. Provider cache usage was recorded separately.

**Three-response subsets and smoke cases do not establish the general SLO or
concurrency-two performance.** Strict script certification does not prove full
functional workflow execution in Trama. Typical prompts below 3,000 tokens are
not demonstrated for automatic interfaces with the full catalog. At this stage,
required groups failed the combined target and quality preservation, so no
promotion or seven-day production observation began as part of this evaluation.

## Subsequent typed-decisions experiment

The separate [typed-decisions comparison](results/typed-decisions-20261008/RESULTS.md)
tested current JSON mode, strict schemas and parameterized constructors after
offline verification. It stopped at 8 of 12 authorized requests when cancellation
left one independent test call's usage unknown. One parameterized parallel
workflow was valid in 6.65 seconds; the other seven requests failed. Known usage
was US$0.067389, with US$0.003147 still reserved for the unknown call. Neither
feature was promoted. These results do not replace the historical measurements
above or qualify the general latency/cost target.

## Reliability recovery qualification — 2026-10-08

[Recovery report and frozen stage manifests](results/reliability-20261008/RESULTS.md)
record 888 passing local regression checks and the native Trama transport blocker.
No new paid request was dispatched; neither stage supplies a measured generation
success percentage. Production remains on the restored strict-enabled image.

A subsequent probe against the deployed Trama renderer found that the earlier
parallel C oracle did not establish native execution: indexed join references
render empty, and a Telegram message object was accepted as text. The historical
comparison report and responses are preserved; its valid row cannot qualify
production functionality. Further work must correct native typed transport
before spending on the frozen progressive verification.

## Native transport recovery verification — 2026-10-09

The [new frozen report](results/reliability-native-20261008/RESULTS.md) preserves
the previous evidence and records a coordinated isolated Trama/RiteSmith transport
correction. Offline qualification passed 912 local checks, 46 ARM checks and 68
directed Trama tests before provider dispatch.

Stage 1 executed **two of three** authorized requests: Celsius passed strict and
functional checks in **5.499 seconds**; inactive-customer generation returned
**HTTP 422 in 7.716 seconds**, with strict union errors and inconsistent independent
test expectations. Known cost for all three nested calls was **US$0.00817635**.
The campaign stopped immediately. Weekly monitoring and all 24 Stage 2 cells
remain unexecuted. No production generation accuracy follows from this 1/2 sample.

Offline follow-up corrects quantified-customer routing, tool-result discriminant
instructions, failed-attempt counting instructions and fixture expectation rules.
Hash-linked regressions preserve the rejection and verify a corrected strict
reference against unchanged expectations. This is a separate source revision,
without further paid calls or a deployment. Retain restored production; the
3/3 gate was not met, and latency/cost targets remain unqualified.
