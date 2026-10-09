# RiteSmith / Trama integration review

## Result

The review found and corrected four failure cases in RiteSmith. Final offline
regression: **992 passed, zero unexpected failures/errors, 3 existing skips,
5 existing expected failures, 14 live E2E tests excluded**. Lint, formatting and
whitespace checks passed. No paid requests, benchmarks or deployments occurred.

Trama source was not changed. All 138 source hashes match the revision previously
verified by its complete **338-test suite**, with no failures/errors/skips. That
suite was not rerun in this review. The matching compiled renderer and JSONLogic
engine were used in RiteSmith's native integration checks.

## Findings corrected

1. **Literal data in repeat exits.** Early-deadline expansion could traverse a
   protected literal envelope and rewrite business references inside its data.
   In some cases compilation failed; in others literal contents could change.
   Expansion and final-action renaming now leave protected values untouched.
   Regressions execute early expiration, expiration before the first sample and
   zero remaining work through the actual native renderer.
2. **Malformed native graphs.** Invalid node containers, IDs, links, requests,
   headers, callbacks or cases could raise Python exceptions during preparation,
   validation or plan completion. Located shape checks now run before traversal
   and completion insertion. Rejected generation gets the one allowed repair,
   then HTTP 422 `generation_failed`; no unusable artifact is saved. Checks cover
   specialized workflow generation, `/generate` and `/plans`. Supported URL/header
   string wrappers are normalized rather than rejected as malformed values.
3. **Unusable provider responses.** Workflow generation/repair error handling
   assumed a parsed JSON object. A response such as `null`, an array or a string
   could fail again while constructing its diagnostic. Error handling now preserves
   the original response, usage statistics and attempt count. Mocked responses
   verify this without provider calls.
4. **Invalid cached continuation.** Missing retained state or remaining work
   could escape as an internal error. An incompatible contract/version could
   silently discard the cache and trigger fresh generation. These cases now fail
   with a located 422 diagnostic and no LLM recovery, retaining failed-job evidence.
   They cannot silently restart the work. A changed intent still follows normal
   generation. Malformed public context containers retain FastAPI's existing
   request-validation 422 response.

The review also checked script assembly/strict acceptance, independent fixture
tests, bounded recovery/accounting, continuation deadlines, branch visibility,
parallel result envelopes, callbacks/compensation and the matching Trama rendering
paths. Existing public contracts and approval/persistence behavior remain covered
by the regression suite. Product repositories other than RiteSmith were read only.

## Evidence

- [regression.log](regression.log), [regression.xml](regression.xml): final full
  non-E2E RiteSmith run, 992 passes in 13.05 seconds. This is test-suite duration,
  not generation latency.
- New review module: **41 passing tests**. Existing initialization/native transport
  modules remain included with their native checks enabled, not skipped.
- [verification.json](verification.json): working-tree source hashes, native
  artifact hashes, exact source identity, Trama comparison and check results.
- [attempts](attempts/): earlier verification output, including the two initial
  continuation-test failures corrected before the final run.
- [Trama full-suite evidence](../trama-default-json-20261009/RESULTS.md): prior
  complete run for `fa3aef0346cf2b1a7e8e075dfc470e9239a34eb8`.

RiteSmith base commit remains `4b6d3ffae7852fae599320120872f55e6c958e23`;
corrections are uncommitted working-tree changes. The base commit alone does not
reproduce them. Previous reports and original provider responses remain unchanged.

The unchanged skips concern the absent shell-generation endpoint and two Lua
timeout cases. Expected failures concern three Obsidian/calendar schema mismatches
and two callback blocklist gaps. They are not fixed by this review. The existing
`slowapi` deprecation warning remains. A dedicated disposable PostgreSQL database
was used and stopped after verification; production storage was not used.

## What remains

The reviewed integration passes the exercised offline contracts and behaviors.
It is ready for code review alongside the compatible Trama revision. Deployment
remains a separate decision, with the existing production and rollback images
unchanged.

**LLM generation accuracy, the 95% / 10-second target and 50% cost savings remain
unconfirmed for this corrected revision.** Controlled references and mocks cannot
establish those results. Any further paid verification needs authorization and
must use a new frozen revision; the stopped campaign cannot be resumed as passing
evidence. The native graph expansion's size/performance tradeoff also remains
unmeasured.
