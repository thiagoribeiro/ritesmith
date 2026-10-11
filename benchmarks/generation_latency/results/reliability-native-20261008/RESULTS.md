# Reliability recovery verification

Source: `3d281777d0d956cc797ce41bdca644ebdecc381e424fc86fef1918abed3a54de`.

Production was not deployed or modified by this verification.

| Stage | Executed / planned | Correct and ≤10s | Known token cost |
|---|---:|---:|---:|
| 1 | 2/3 | 1/2 | $0.008176 |
| 2 | 0/24 | Not measured | $0.000000 |

Stopping reason: incorrect_delivery

Unexecuted cells are not failed deliveries and provide no accuracy evidence.
These observations do not qualify the 95%/10-second target or prove 50% savings.

Recommendation: `retain_restored_production`.

Full request data, diagnostics, usage and original provider responses are in the adjacent JSON files.

## What happened

The Celsius delivery passed. The inactive-customer delivery failed acceptance and
returned HTTP 422; no notification artifact was published. The campaign stopped
after that second request, as required. The weekly monitoring request and all
24 Stage 2 requests remain **unexecuted**, not failures.

| Request | Interface | Functional delivery | Strict certification | Total time | Calls | Known cost |
|---|---|---|---|---:|---:|---:|
| Celsius conversion with supplied tests | `/generate/lua` | Correct | Passed | 5.499 s | 1 | $0.00096525 |
| Inactive-customer notification without supplied tests | `/generate` | No acceptable artifact; HTTP 422 | Failed candidate | 7.716 s | 2 | $0.00721110 |
| Seven-day BTC monitoring | `/generate` | Unexecuted | Not applicable | — | 0 | $0 |

Observed acceptance is **1/2 executed requests**, not 1/3 or a production accuracy
estimate. Both requests finished within 10 seconds, but a fast rejection is not
a successful delivery. Stage 1 therefore did not meet its 3/3 gate.

## Why the notification request failed

1. The local router treated “every customer” as scheduling. Although the request
   supplied script schemas, it received mixed script/workflow prompts and the
   workflow model. This is unnecessary generation work for this request.
2. Luau instructions required checking `result.error` even when the declared
   success variant does not contain that field. The model followed that pattern,
   causing strict union-type errors. The candidate also incremented the send
   counter only on success, which would not enforce the ten-attempt requirement.
3. Independent tests supplied `message: null` against a string contract and put
   the same failed customer in both `notified` and `failed`. Other expectations
   conflicted with the controlled eligibility and send responses. Removing nulls
   alone would not make those expectations reliable.

The validation failure correctly prevented a program repair against inconsistent
tests. There was **no repair or fallback call**. All three provider calls settled
with known usage, so the final cost is $0.00817635 and no reservation remains unknown.

| Call | Model / effort | Input tokens | Output tokens | Reasoning tokens included in output | Cached input | Provider time | Cost |
|---|---|---:|---:|---:|---:|---:|---:|
| Celsius generation | `gpt-5-mini` / low | 1437 | 303 | 128 | 0 | 4.755 s | $0.00096525 |
| Notification proposal | `gpt-5.4-mini` / low | 3778 | 907 | 406 | 0 | 5.137 s | $0.00691500 |
| Independent notification tests | `gpt-4.1-nano` | 1737 | 306 | 0 | 0 | 3.042 s | $0.00029610 |

Proposal and independent tests ran in parallel. Provider times therefore must
not be summed to infer request latency. Validation and requested persistence are
included in the measured total. Original responses and usage are preserved in
`stage1-spending.json`; stage/call diagnostics are also extracted in `traces.json`.
Standard token prices were checked against the official model pages listed in
`verification.json`. There is no matching valid baseline comparison in this run.

## Offline qualification before paid calls

- 912 local non-E2E checks passed, with 3 skipped, 5 expected failures and 14 E2E
  cases excluded. Lint and formatting passed.
- 46 directed checks passed on ARM, with 6 database/HTTP cases excluded from that
  container run and covered by local regression.
- 68 directed Trama tests passed, including typed rendering, HTTP task dispatch,
  JSONLogic, split/join and executor behavior.
- The isolated engine adds explicit `typed-json-v1` rendering. The verifier uses
  its actual compiled renderer and JSONLogic evaluator for typed values, indexed
  join results and preserved continuation state. Controlled constructor checks
  exercise nine patterns and compositions. They do not replace a full production
  scheduler execution, and this paid run contains no workflow delivery observation.

The qualification source and native binary hashes match the frozen candidate.
The complete two-stage manifest was written before the first paid generation.
The original typed-decisions and failed-offline reports remain unchanged.

## Corrections after the stopped campaign

The follow-up code is a **different, offline-only revision**. It does not alter
the frozen paid configuration, observations, provider responses or candidate image.

- Routing distinguishes “every customer” from timed recurrence while retaining
  composed script/workflow requests. The mocked notification proposal now selects
  script-only prompts and `gpt-5-mini`.
- Prompt version `generation-v5` narrows tool unions using their declared
  discriminant, counts failed calls toward limits, and requires schema-valid,
  fixture-derived test expectations without forbidden null fields.
- Hash-linked regressions reproduce the original strict and expectation failures.
  An explicitly corrected reference passes strict checking and the unchanged
  controlled cases, including ten attempts with one failed send. A mutation that
  counts only successful sends fails that same expectation.

The follow-up revision passed **924 non-E2E regression checks on ARM**, with 3
skipped, 5 expected failures and 14 E2E cases excluded, plus lint and formatting.
It used the retained image with the three corrected source files mounted read-only
and an isolated test database; no OpenAI key was supplied. Details and the source
hash are recorded in `post-campaign-verification.json`.

These checks establish the corrected routing and reference behavior. They do
**not** establish that a new model response will follow the revised instructions.
Further paid verification of a corrected revision requires separate authorization;
there were no additional paid calls after the stop.

## Release recommendation and retained evidence

**Retain restored production and continue correction/verification. Do not deploy.**
The 3/3 gate was not met, so Stage 2 was never authorized by its condition.
Neither the general 95% success target nor a 50% cost reduction is established.

The typed transport candidate requires coordinated RiteSmith and Trama support;
enabling its setting against the deployed legacy Trama alone is unsupported.
Both features remain isolated, and the new transport setting defaults to disabled.

The isolated database snapshot contains one persisted Celsius artifact, two
generation jobs (completed and failed), and both candidates/validation records.
There are no plans or dependent artifacts from unexecuted requests. Raw responses,
artifacts, diagnostics, frozen manifests, logs and the Trama source patch are
retained alongside this report. Frozen source archives and candidate images remain
on r2d2 under `/home/thiago/ritesmith/reliability-native-20261008`.

Production remains on the restored strict-enabled RiteSmith image and unchanged
Trama image. The candidate images and strict-enabled rollback configuration are
preserved for review. Deployment remains a separate decision.
