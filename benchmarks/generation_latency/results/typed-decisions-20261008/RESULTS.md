# Typed decisions: bounded r2d2 comparison

**Recommendation: retain both features experimentally, disabled by default.**
Parameterized parallel workflows showed a useful direction. Strict schemas
preserved transport structure but did not prevent incorrect code, references or
workflow decisions. These observations do not support production promotion or
qualify the 95%/10-second or 50%-lower-cost target.

After this comparison, the separate generation-improvements production release
was withdrawn. The previous image was restored with strict certification kept
enabled. See [the recovery record](../../../../docs/generation-rollback-r2d2.md).
The frozen comparison evidence below was not changed or rerun.

## Execution and spending boundary

The frozen twelve-cell matrix started on r2d2 on October 8, 2026. It executed
**8 generation requests and 17 nested provider calls**, sequentially. There was
**1 valid delivery**, also within 10 seconds. Failures were not repeated.

Sixteen calls have known usage totaling **US$0.067389**. The independent test
call in request 7 was cancelled when the proposal failed; its usage is unknown.
Its **US$0.003147** reservation remains occupied. Known charges plus that full
reservation total **US$0.070536**, below the authorized US$1 token budget.
The exact campaign cost is **unknown**, not US$0.067389 or US$0.070536.
There is no valid matching baseline for a cost-per-valid-artifact reduction.

Unknown usage stopped the campaign. Weekly monitoring C, parallel prices A,
dependent plan A and dependent plan C were **unexecuted**. Available request and
spending capacity was not used to rerun failures or continue after the stop.

Each dispatch reserved a conservative input bound and the full output limit.
The shared durable ledger included tests, fallback and repair calls; it also
blocked duplicate requests and unsupported pricing. Standard text pricing,
without account-specific premiums or tax, was frozen before dispatch.

## Every observed request

Tokens are summed across all completed calls in each request. Output includes
reasoning; reasoning is shown separately and must not be added to output again.
Cache is a subset of input. Values marked `+?` omit the cancelled call's unknown
usage. A recovery includes regeneration/fallback or repair; it is distinct from
the independent test call.

| Cell | Scenario / interface | Variant | Valid | Seconds | Calls | Recoveries | Input | Output | Reasoning | Cache | Known USD |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | Weekly BTC / `/generate` | A | No | 22.953 | 2 | 1 | 7014 | 2880 | 1088 | 0 | 0.01223700 |
| 1 | Parallel BTC/ETH / specialized workflow | C | Yes | 6.653 | 1 | 0 | 3581 | 793 | 534 | 0 | 0.00625425 |
| 2 | Celsius / specialized script | B | No | 8.558 | 2 | 1 | 2730 | 900 | 384 | 1280 | 0.00219450 |
| 3 | Inactive notifications, no supplied tests / `/generate` | A | No | 13.564 | 3 | 1 | 9027 | 2175 | 504 | 0 | 0.00963540 |
| 4 | Weekly BTC / `/generate` | B | No | 24.725 | 2 | 1 | 10266 | 3752 | 2037 | 0 | 0.01624450 |
| 5 | Parallel BTC/ETH / specialized workflow | B | No | 9.632 | 2 | 1 | 5461 | 1286 | 754 | 0 | 0.00988275 |
| 6 | Celsius / specialized script | A | No | 6.284 | 2 | 1 | 2186 | 644 | 192 | 1024 | 0.00160410 |
| 7 | Inactive notifications, no supplied tests / `/generate` | B | No | 10.358 | 3 | 1 | 10648+? | 1330+? | 161+? | 0+? | Unknown; 0.00933650 settled |

Timings cover the production HTTP route invoked in-process, recall, generation,
requested persistence and validation. Independent tests overlap proposals when
applicable; summed provider duration is not end-to-end latency. Provider network
time is included, but external HTTP ingress and real external effects are not.

## Validity diagnostics

- **Weekly A:** the first response mislabeled semantic operations as a compact
  graph. Fallback compiled, but controlled execution rejected the unsupported
  JSONLogic operator `greater`; the required finite continuation was not proved.
- **Parallel C:** selected `pattern_parameters`. The constructor generated the
  split/join and rewrote structured branch references. Controlled execution
  verified both prices appeared in one aggregate notification. No repair was used.
- **Celsius A and B:** every attempt returned a `luau_body` containing a complete
  `function run(...) ... end`. The assembler correctly rejected redeclaration.
  The service returned an empty draft with no validation or strict certification;
  the evaluator counted each delivery as invalid despite HTTP 200. Neither
  candidate reached strict certification. This failure occurs in both variants.
- **Notifications A and B:** proposals repeated the same `run` redeclaration
  mistake. Both returned HTTP 502 before functional validation. A's independent
  test call completed. B's was cancelled without usage, stopping the campaign.
  Supplied tool fixtures did not cause real notifications or external calls.
- **Weekly B:** both responses conformed to the transport type, but generation
  chose incorrect graph/reference details. Fallback failed with
  `Node advance_chain: unknown reference monitor_chain_part`. Finite continuation
  was not demonstrated.
- **Parallel B:** initial semantic generation referenced branch results outside
  their scope. Its one repair chose `compact_graph` and referred to
  `branches[0].result.price`, without the required output envelope. Controlled
  execution rejected the aggregate notification for missing branch results.

Original provider responses, returned artifacts and all diagnostics are retained.
No artifact was marked successful merely because it matched a response schema.

## What the comparisons show

**A → B, schema effects:** all nine completed B/C provider responses passed the
application's typed transport validation. Schema constraints did not constrain
the semantics inside code strings or JSON-string fields. Both A and B failed
Celsius, notifications and weekly monitoring. Weekly B took 1.772 seconds more
and used 872 more output tokens than A; Celsius B took 2.274 seconds more and
used 256 more output tokens. This sample does not demonstrate a latency or cost
benefit from strict schemas alone. Notifications B has unknown total cost.

**B → C, constructor effects:** parallel C was valid, whereas B failed after a
repair. Across the complete requests, C used 493 fewer output tokens, 1880 fewer
input tokens, one fewer provider call and 2.978 fewer seconds. Its known request
charge was US$0.00362850 lower. These are request-level differences, **not a
percentage saving per valid artifact**, because B supplied no valid baseline.
Looking only at primary generations, C used 793 output tokens versus B's 631
and 3581 input tokens versus B's 2850. The observed benefit came from avoiding
the failed repair cycle; it does not establish that pattern transport is always
smaller or faster per call.

There is no parallel A observation, weekly C observation or paid `/plans`
observation. No global ranking, p95 qualification, model change or rollout claim
can be derived from this incomplete single-observation matrix.

## Offline verification and reproduction

Before any paid request:

- Local non-E2E regression: **855 passed, 3 skipped, 14 deselected, 5 xfailed**.
- Directed r2d2 ARM checks: **155 passed**.
- Final frozen transport/constructor checks on ARM: **41 passed**.
- Fixture-only reference script checks, Ruff lint and formatting passed.

Checks cover generated schema closure/nulls/operation variants, arbitrary JSON
decoding, refusal/truncation and unsupported routing, independent switches,
all nine constructor equivalences, exact finite continuation and final actions,
fixture isolation, strict script checking and bounded shared accounting.
Existing regression covers public interfaces and dependent materialization.
Paid dependent-plan behavior remains unobserved. Controlled workflow simulation
does not certify the entire native Trama execution engine.

Frozen source SHA-256:
`caacbf7ed764295bf26b33679059a19b658ade6e88294127d3aa85e7ee309f34`.
Runtime image:
`sha256:3674b56b2d208411697311ab41a528a25372cfceba44773128644598ca97b0d7`.
Runtime: r2d2 ARM64, Python 3.12. Models and low effort were unchanged across
variants. Full settings, prices, file hashes, schemas and scenario order are in
[manifest.json](manifest.json); [verification.json](verification.json) links the
offline verification to that exact source. Frozen code was not edited during or
after the comparison. The runner refuses another campaign in this output folder.

Evidence:

- [Every public response and call/stage trace](responses.jsonl).
- [Durable spending ledger and original provider responses](spending.json).
- [Machine-readable paired report](report.json).
- [Derived conformance checks and explicit recommendation](analysis.json).
- [Local regression log](local-regression.log), [ARM regression log](offline.log),
  [final frozen checks](final-offline.log), [campaign log](campaign.log).

## Promotion and next work

| Operation / feature | Decision | Reason |
|---|---|---|
| Scripts and proposals / strict schemas | Retain experimentally; disabled | Shared assembly failures; no valid paid script or favorable paired result |
| Workflows / strict schemas alone | Retain experimentally; disabled | Transport-valid but functionally invalid artifacts and added request work |
| Parallel workflows / pattern parameters | Retain experimentally; disabled | Promising single valid result; no matching valid baseline or repeated evidence |
| Monitoring and dependent plans / pattern parameters | Retain experimentally; disabled | Paid cells unexecuted; offline equivalence alone is insufficient for promotion |
| Independent tests and repairs / strict schemas | Retain experimentally; disabled | Incomplete paid functional evidence; one cancelled test with unknown usage |

The concrete next work is to remove conflicting full-script/body instructions,
provide one unambiguous body-only example, and verify the preserved invalid
responses as offline regression cases. Workflow JSONLogic and reference/aggregate
diagnostics should remain actionable before consuming recovery. These follow-ups
must use a new source version; this experiment was not tuned or rerun.

Production was not replaced. Both new switches remain false and can be disabled
independently; no production database migration was added. Evaluation used its
own empty database and fixture-only tools. The existing r2d2 image/configuration
and rollback script remain available. Subsequent evidence should come from
ordinary requests after a configuration is selected, without another artificial
campaign under this authorization.
