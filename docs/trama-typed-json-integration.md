# RiteSmith integration with mandatory typed JSON in Trama

## Status

This offline correction targets [Trama PR #54](https://github.com/thiagoribeiro/trama/pull/54),
commit `fa3aef0346cf2b1a7e8e075dfc470e9239a34eb8`. Neither service was deployed.
RiteSmith's public artifacts remain complete Luau scripts and native Trama JSON.
Models, effort and experimental generation switches are unchanged.

JSON bodies have one rendering behavior. RiteSmith no longer emits
`X-Trama-Template-Mode`; it removes that obsolete header from prepared requests.
The old `generation_workflow_typed_json` setting remains accepted for configuration
compatibility but does not select a rendering mode. Both values produce the same
prepared workflow. A compatible Trama engine is required; these checks do not
establish compatibility with the unchanged production engine.

## Explicit initialization

Missing JSON references are errors, not implicit null values. A repeat cannot
read its own result before executing. Semantic repeats declare initial state:

```json
{
  "kind": "repeat",
  "id": "monitor",
  "count": 28,
  "interval_seconds": 21600,
  "initial_state": {"minimum": null},
  "state": {"minimum": {"ref": "minimum", "path": "min_value"}},
  "steps": [
    {"kind": "call", "id": "fetch", "capability_name": "market.coin_price", "args": {"symbol": "btc"}},
    {"kind": "state", "id": "minimum", "capability_name": "stat.min_value", "args": {
      "current": {"ref": "fetch", "path": "price"},
      "previous_min": {"ref": "minimum", "path": "min_value"}
    }}
  ]
}
```

Python expands up to twenty business iterations per graph. The first minimum
uses the explicit null seed; later iterations reference completed predecessors.
Counters likewise start with explicit numbers. Generated native graphs are
larger than the former cyclic graphs; the LLM still writes the compact semantic
plan. This tradeoff has not been measured in a new latency benchmark.

The `stat.min_value` and `stat.max_value` contracts now accept the explicit nullable seeds
already supported by their implementations. Runtime algorithms are unchanged.

## Continuation and termination

The example executes twenty samples, waits the next six-hour interval and
continues with eight remaining samples. It carries the original semantic plan,
remaining work, state and original deadline, where applicable. Compatible cached
continuations compile locally without another LLM call.

An incompatible cached version or contract hash fails with HTTP 422
`generation_failed`; it does not silently discard the cache and generate a new
workflow. Correct the incompatibility explicitly while retaining remaining work
and the original deadline. A changed intent still uses the normal generation path.

A finite continuation missing remaining work, a duration continuation missing
its original deadline, or a continuation missing required retained state is
rejected with a diagnostic. Values cannot silently reset to the original total
or initial minimum. Zero remaining work runs the final actions without sampling.
If the deadline expires early, final actions use the last completed state, or
the explicit retained seed when no new sample has run.

Literal state, intent, constraints and cached future plans are protected with
Trama's `__trama_literal_json__` envelope where needed. Data containing template
text or compiler-reserved keys remains data; it is not recursively evaluated.
The envelope is removed by the engine before the receiver sees the value.

## Acceptance and evidence

Preparation validates JSON source and reference syntax before publication.
Semantic checks reject unavailable body references, including first-pass
self-references in advanced compact graphs. Diagnostics name the consuming node
and reference. Optional data guarded by supported JSONLogic conditions remains
valid; non-JSON body destinations do not acquire JSON-specific requirements.

Offline checks use the actual compiled Trama JSON renderer and JSONLogic engine
with controlled tool responses and a virtual clock. They cover the nine patterns,
parallel result envelopes, deadline exits, compensation, callbacks, literal data,
dependent certified scripts and HTTP generation acceptance. They are not a full
distributed workflow deployment test or a measurement of future LLM accuracy.

See the [verification report](../benchmarks/generation_latency/results/ritesmith-explicit-seeds-20261009/RESULTS.md)
for the initial counts, source hashes and exclusions, and the
[follow-up review](../benchmarks/generation_latency/results/integration-review-20261009/RESULTS.md)
for subsequent corrections and final regression evidence. No paid requests were made.
Production and rollback images/configurations remain unchanged. A coordinated
release and any further paid generation verification remain separate decisions.
