# Typed decisions and Structured Outputs

This experiment borrows the principle of bounded decisions composed in code.
It does not use Jev, the TypeSafe SDK, confidence scores, or a new classifier.
Application types and constructors belong to RiteSmith; provider adapters own
the transport. Public HTTP/MCP artifacts remain complete scripts and native
Trama JSON. No database migration is required.

The [completed bounded comparison](../benchmarks/generation_latency/results/typed-decisions-20261008/RESULTS.md)
stopped after eight requests because a cancelled independent test had unknown
usage. One parameterized parallel workflow was valid within 10 seconds; the
other requests failed. Both features remain experimental and disabled. The
report distinguishes known charges from the retained unknown-call reservation.

## Independent controls

| Environment variable | Default | Behavior |
|---|---|---|
| `RITESMITH_GENERATION_STRUCTURED_OUTPUTS` | `false` | Use typed response contracts and native schema constraints when supported |
| `RITESMITH_GENERATION_STRUCTURED_OUTPUT_OPERATIONS` | `null` | All operations; a JSON list selects operations; `[]` selects none |
| `RITESMITH_GENERATION_PATTERN_PARAMETERS` | `false` | Offer parameterized Trama constructors in the existing generation call |
| `RITESMITH_GENERATION_PATTERN_PARAMETER_OPERATIONS` | `null` | All applicable operations, or an explicit JSON list |

Operation names include `lua_gen`, `luau_gen`, `lua_repair`, `luau_repair`,
`workflow_gen`, `workflow_repair`, `proposal_script`, `proposal_workflow`,
`intent`, `test_gen`, and `reuse_judge`. Pattern parameters apply only to
workflow generation/repair and proposals. They can be used independently of
native schema constraints; JSON-mode transports then receive the generated
schema and undergo the same local validation.

Disable either feature independently to roll back its behavior. Keep the previous
image and configuration for a complete application rollback. Existing generation
deadline, recovery, semantic-workflow and Luau-assembly settings remain separate.

## Contracts and guarantees

`typed-response-v1` uses application-owned Pydantic response types. Workflow
operations have separate shapes. Repetition has one explicit termination rule:
sample count, duration, absolute deadline, or continuous execution. Transport
nulls are removed before the existing domain validators see the candidate.

OpenAI schema lowering closes objects, requires their properties, removes
defaults, and preserves nullable alternatives. Unsupported schema/model routing
happens before dispatch and is recorded without an additional retry loop.
See the [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

Open JSON data uses explicit strings such as `args_json`, `when_json`,
`graph_json`, and fixture `output_json`. Decode rejects malformed JSON,
duplicate keys, and non-JSON numeric constants. Client schemas and data are
preserved without truncation; the existing compiler, registry, strict Luau
checker and fixture validators still enforce their business contracts.
Provider constraints validate these fields as strings, **not their JSON contents**.
Code inside a string and schema-valid business decisions can still be wrong.

`pattern-parameters-v1` supports the nine existing patterns. Business operations
and references remain explicit; constructors supply repeated control flow.
Finite termination, state and final actions are retained across continuation
chunks. Unsupported compositions may choose `semantic_plan` or `compact_graph`
in the same response. Representation escape is recorded, not counted as a
second generation call. Mermaid behavior is unchanged.

## Frozen comparison

- A: current JSON mode, semantic plans and Luau body assembly.
- B: typed transport and strict schema, with the same generation responsibilities.
- C: B plus pattern parameters.

Use the frozen twelve-cell matrix in
`benchmarks/generation_latency/typed_decisions.py`: weekly monitoring and parallel
prices in A/B/C; supplied-test Celsius and fixture-controlled missing-test
notifications in A/B; one held-out dependent plan in A/C. Models stay
`gpt-5-mini` for scripts, `gpt-5.4-mini` for workflows, and `gpt-4.1-nano` for
existing fast operations; generation effort remains low.

Run only after offline regression and lint pass, on r2d2 with a disposable
`typed_validation` database and substituted external tools. The runner invokes
the production HTTP routes in-process, includes validation/persistence in elapsed
time, withholds artifact reuse, and runs one request at a time. Functional
workflow checks use a virtual clock, both sides of the price threshold, branch
aggregation and plan completion. Notification cases verify eligibility, failed
sends, listing errors and the ten-message limit. No external effect is executed.

```bash
python -m benchmarks.generation_latency.typed_decisions --output /results
```

The entire campaign has at most **12 requests and US$1 in standard text-token
cost**. Every nested call reserves an input byte-bound plus framing headroom
and the full output-token limit, including reasoning, before dispatch. Known
usage releases unused reservation. Unknown pricing/usage or interrupted calls
stop the campaign and retain reservations. The durable ledger refuses duplicate
requests and unfinished calls after restart. The runner refuses to reuse an
existing manifest; failed cells are never rerun.

Standard pricing was checked against the official model pages on 2026-10-08:
[GPT-5 Mini](https://developers.openai.com/api/docs/models/gpt-5-mini),
[GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), and
[GPT-4.1 nano](https://developers.openai.com/api/docs/models/gpt-4.1-nano).
Reservations assume standard service, no provider-hosted tools, and no tax or
account-specific premiums. The paid runner forces standard service.

Preserve the source/configuration/schema manifest, original provider responses,
request responses, stage traces, token/cache/repair accounting, spending ledger,
offline checks and paired report. Cost per valid artifact includes failed calls;
zero valid artifacts or unknown usage cannot substantiate savings.

## Decision rule

Offline compatibility and functional checks are mandatory. A new functional
failure blocks promotion of the affected feature. Report output-token reductions,
repair reductions, latency and cost separately. Lower output tokens alone do not
establish lower latency or cost. Compare A/B for schema effects and B/C for
constructor effects wherever matching cells exist.

Enable only operations with preserved behavior and a favorable observed tradeoff;
mixed results remain experimental. One observation per cell is insufficient to
qualify the general **95% valid within 10 seconds / 50% lower cost** target.
Further evidence comes from ordinary production requests, not another artificial
campaign. Controlled simulation covers a subset of Trama semantics and does not
certify execution of every native Trama feature.
