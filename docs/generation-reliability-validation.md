# Generation reliability recovery

This work does not deploy to production. The restored r2d2 image and the explicit
strict certification requirement remain in place.

## Corrections implemented

- Specialized and automatic Luau prompts distinguish statements-only bodies
  from complete scripts. Body assembly includes a complete example. Repairs use
  the complete-script format and receive the rejected candidate and diagnosis.
- Invalid body candidates retain their original response and offending field.
  Duplicate entry points fail acceptance; comments and literals do not count as
  declarations. Syntax validation compiles code without executing it.
- Exhausted scripts, workflows, automatic generation and plan materialization
  return HTTP 422 `generation_failed`. MCP returns an error result containing
  the same envelope. Unusable artifacts are not published. Failed script jobs
  and workflow jobs retain attempts and diagnostics.
- Independent tests are drained within the remaining deadline. Known controlled
  fixtures override invented data; fixture failures do not trigger program
  repairs. The notification regression includes twelve eligible customers,
  ineligible customers, listing failure, sending failure and a ten-attempt cap
  that includes failed sends.
- Semantic conditions use structured references and recognized JSONLogic
  operators. Input containers and known output references are checked against
  capability contracts. Text composition compiles to native string arguments.
- The controlled simulator has an explicit native-template check that refuses
  to present typed JSON or indexed-list behavior as equivalent to Trama.

## Frozen progressive verification

`python -m benchmarks.generation_latency.reliability --output DIRECTORY` freezes
both stage manifests together, including requests, fixtures, expectations and
configuration. An existing campaign cannot be resumed or rerun. The directory
must contain `verification.json` matching the source identity.

Stage 1 permits three generation requests and $0.10. Stage 2 permits twenty-four
additional requests and $1.00, only after all three Stage 1 deliveries are
functionally correct, fully accounted for and completed within ten seconds.
The fixed order interleaves scripts, workflows and dependent plans. Models stay
at `gpt-5-mini low`, `gpt-5.4-mini low` and the existing fast test model.
Mermaid, Structured Outputs and pattern parameters remain disabled.

Nested calls share a durable stage ledger. Every call reserves its maximum input
and output token cost before dispatch. The two stage caps together bound the
campaign to 27 requests and $1.10. Incorrect delivery, latency above ten seconds,
unknown usage/pricing, insufficient budget or cancellation stops further cells.
There are no automatic reruns, easier replacement cases or prompt tuning.

The runner requires isolated `ritesmith_reliability` storage and a frozen
candidate image. It substitutes external tools and executes certified plan
scripts against controlled data rather than assuming their output. The normal
production application never imports these staging substitutions.

## Original transport blocker

The production Trama renderer was exercised directly from its compiled classes,
using `TemplateEscaping.JSON_STRING`. It produces an empty string for both:

- `nodes.join.response.body.branches.0.result.output.price`
- `nodes.join.response.body.branches[0].result.output.price`

Rendering the whole branch collection produces Java collection text, for example
`[{result={output={price=111111}}}]`, rather than JSON. Existing Python simulation
looked up numeric list indices and returned typed objects instead. That behavior
is useful for compiler control-flow tests but does not prove native execution.

This affects parallel aggregation and transport of state/remaining work between
continuations. A passing schema check or simulator count cannot qualify those
workflows. Paid verification must remain stopped until generated request bodies
are exercised through Trama's renderer and the receiving RiteSmith contracts,
including missing values, numbers, arrays, objects and continuation payloads.
A repair must also preserve output envelopes, branch scopes and certified script
inputs. It must not depend on regex-based reconstruction of Java collection text.

The prior typed-decisions report and provider responses are preserved unchanged.
Its parallel C result was labeled valid by a weaker oracle; that oracle accepted
an object as Telegram text and did not exercise native Mustache indexing. That
historical row therefore cannot demonstrate functional correctness on Trama.
No production-wide generation accuracy can be inferred from it.

A failed offline qualification creates a report with every paid cell unexecuted
and zero token spending. Such cells are not failed LLM deliveries. A favorable
release recommendation still requires all 3 + 24 deliveries, complete accounting,
passing native functional checks and review. Deployment remains a separate action.

## Historical isolated typed transport correction

The following describes the original opt-in experiment, not the current
integration. Header selection and missing-reference-to-null behavior were
superseded by mandatory typed JSON and explicit initialization. See
[the current integration](trama-typed-json-integration.md). The original frozen
reports and provider responses remain unchanged.

The candidate adds `generation_workflow_typed_json`, disabled by default. When
enabled, JSON task and compensation requests carry
`X-Trama-Template-Mode: typed-json-v1`. This requires the matching Trama candidate;
the deployed legacy engine does not implement this mode. Turning on this setting
alone against that engine is not a valid release configuration.

The Trama change is maintained in a separate worktree/branch and preserved as a
patch with the verification evidence. It parses the original JSON body, resolves
whole-value references as typed JSON, and resolves scalar references inside text.
Dot and bracket indexes read the actual join envelope. Missing whole values become
null, allowing explicit initial counter/state seeds. Collections inside text,
unsupported template expressions and scalar traversal fail with diagnostics.
Resolved values are never recursively interpreted as templates. Unflagged requests
continue to use the original Mustache renderer. Invalid flagged bodies fail before
HTTP dispatch without transport retries.

The offline oracle uses the compiled Trama candidate renderer and JSONLogic
evaluator, not Python replacements. Tests exercise native task/compensation HTTP
rendering, receiving RiteSmith contracts, all nine constructors, both continuation
chunks, final aggregation and actual Luau script execution in dependent plans.
The minimum-state test deliberately raises prices in the second chunk so resetting
state cannot pass unnoticed. HTTP failures are explicit fixtures, distinct from a
tool's successful JSON response containing an `error` key.

The frozen paid verification uses this coordinated candidate configuration. Its
findings cannot establish compatibility with the unchanged production Trama.
Both production services and their rollback images/configurations remain intact.

The offline Java probe must be compiled with `javac --release 21`; the r2d2 JVM
is Java 21 even when the developer machine uses a newer JDK. Package the probe
class with the exact candidate engine JAR and its Kotlin, serialization, Mustache,
Gson and JSONLogic dependencies. Freeze their SHA-256 hashes in `verification.json`.
The runner refuses a different oracle before dispatching any provider request.

## Stopped native-transport verification and follow-up

The coordinated candidate qualified offline and then executed two Stage 1 cells.
Celsius passed in 5.499 seconds; inactive-customer generation failed acceptance
in 7.716 seconds. All three nested provider calls settled with known usage,
totaling US$0.00817635. The campaign stopped on that failure. No weekly workflow
or Stage 2 cell was executed, and production remained unchanged.

The subsequent offline revision uses prompt version `generation-v5`. Local
routing distinguishes “every customer” from timed recurrence and keeps explicit
script/workflow compositions. Luau instructions narrow unions with their actual
discriminant, such as `ok`, before reading member-specific fields. Call limits
count attempts including failures. Independent-test instructions preserve schema
nullability, eligibility, list order and the supplied tool success/failure data.

The retained response fails strict checking and its test expectations remain
invalid; they are not silently rewritten into passing evidence. A corrected
reference passes the original fixtures, and a success-only send counter mutation
fails the ten-attempt expectation. This proves the offline reference behavior,
not future LLM reliability. Both revisions and their separate evidence are
preserved. A new paid campaign requires authorization for the corrected revision;
the stopped campaign cannot be resumed or rerun.

See the [frozen report](../benchmarks/generation_latency/results/reliability-native-20261008/RESULTS.md)
for requests, source hashes, original provider responses, diagnostics and the
offline follow-up patch. Retain the restored strict-enabled production version.
