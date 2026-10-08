# Historical r2d2 evaluation without credits — October 8, 2026

Implementation was available in the workspace. **The target of 95% valid artifacts
under 5 seconds was not demonstrated. No production model was selected and no
promotion occurred at this stage.** The later reduced protocol and owner-authorized
[direct deployment](../../docs/generation-deployment-r2d2.md) supersede the original
next steps below; these results are preserved historical evidence.

The evaluation used a separate `ritesmith-latency` container and database, with
HTTP bound to loopback. Production was preserved. Requests included generation,
LunarDyson validation, searches and persistence. Scripts used substitute tools
from the existing corpus; workflows executed no external effects. Artifact reuse
and examples from previous artifacts were disabled. Provider caching was recorded
separately.

## Available results

Triage used three Luau and three Trama scenarios, three repetitions per scenario,
concurrency one, and specialized endpoints. p95 uses empirical nearest-rank;
invalid responses remain in the denominator.

| Configuration | Type | Requests | p95 | Valid | Valid under 5s |
|---|---|---:|---:|---:|---:|
| Baseline: gpt-5-mini, omitted effort | Luau | 9 | 23.11s | 9/9 | 0/9 |
| Baseline: gpt-5-mini, omitted effort | Trama | 9 | 82.84s | 8/9 | 0/9 |
| Only gpt-5-mini with low | Luau | 9 | 8.91s | 9/9 | 5/9 |
| Only gpt-5-mini with low | Trama | 9 | 23.97s | 6/9 | 1/9 |

The baseline weekly workflow failed after 38.34s because it omitted the required
price-query capability. The low-effort control improved latency but also reduced
workflow functional success. It therefore did not satisfy promotion criteria.

Before the campaign, a full-configuration `gpt-5.4-mini` smoke test produced six
valid responses in 1.88–3.20s: Celsius conversion and a simple reminder through
all three interfaces. This covered just two scenarios, one repetition each:
**it does not establish full-corpus p95**. One `/plans` generation received provider
cache; the other first generations did not. Prompts ranged from 898 to 6,699 tokens.
Typical prompts below 3,000 tokens were also not demonstrated for automatic
interfaces with the full catalog.

## Evaluation blocker

The OpenAI API began returning HTTP 429. A diagnostic confirmed
`type=insufficient_quota`, `code=credit_balance_exhausted`: the account had no
credits. The campaign continued after SSH disconnected until it was stopped;
those failures were preserved. At least one request was in progress without a
complete HTTP runner record when interrupted; it is not counted as a success.

Later arms, including `gpt-5.2`, `gpt-5.4-mini`, filtered examples and the full
configuration, were affected by 429 and cannot compare model latency or quality.
Many requests became invalid after approximately 49 seconds of retries/backoff.
Finalist files from the older runner are unqualified results, not model selection.
The updated report records `status=blocked` and `promotion=false`.

Code now distinguishes exhausted credit from transient rate limiting: exhausted
credit causes no retry or fallback to another model on the same account. The
runner also stops its queue and writes a partial summary on this error.
A [diagnostic on the separate instance](results/diagnostics/quota-fast-fail.jsonl)
confirmed HTTP 502 after 2.91s, one API call and no fallback. It counts as failure,
not as a valid artifact under 5s.

## Evidence and original next steps

- [Report with all groups and slow/invalid requests](results/campaign/report.json).
- [Raw baseline](results/campaign/triage-baseline-gpt-5-mini-mixed-c1.jsonl).
- [gpt-5-mini low control](results/campaign/triage-model-gpt-5-mini-mixed-c1.jsonl).
- [Initial smoke test of all three interfaces](results/adapter-smoke.jsonl).
- [Protocol, settings, rollback and monitoring](README.md).

These measurements preceded final changes to numeric example selection, finite
continuation rules, analysis normalization, cancelled-call tracking and exhausted
credit handling. They do not qualify the final code. New runs record a software
hash to prevent mixing different source revisions.

The original follow-up was to replenish credits or configure another key and run
a fresh campaign while preserving this one. Outstanding original work included
all four ablations, two quality-preserving finalists per artifact type, five
repetitions over 31 tasks and 20 workflows, concurrency one/two, held-out cases,
dependent plans and independent test generation without caller-supplied tests.
Promotion originally required per-group gates and functional/strict comparisons
against the full baseline. Seven-day observation had not begun. The user later
replaced that extensive campaign with a smaller evaluation and then requested
implementation without further benchmarks; do not treat this historical protocol
as authorization to start another campaign.
