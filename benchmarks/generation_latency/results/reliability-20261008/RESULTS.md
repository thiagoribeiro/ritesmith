# Reliability recovery verification

Source: `e005f511a6da6ec20bcee7a4e0fda6363afef8c5cc7110c3c0f3e1bc0ad32cbb`.

Production was not deployed or modified by this verification.

| Stage | Executed / planned | Correct and ≤10s | Known token cost |
|---|---:|---:|---:|
| 1 | 0/3 | Not measured | $0.000000 |
| 2 | 0/24 | Not measured | $0.000000 |

Stopping reason: offline_qualification_failed: native_template_transport_mismatch: indexed join references render empty; collection/state references do not preserve typed JSON. Native workflow execution is not qualified.

Unexecuted cells are not failed deliveries and provide no accuracy evidence.
These observations do not qualify the 95%/10-second target or prove 50% savings.

Recommendation: `retain_restored_production`.

Full request data, diagnostics, usage and original provider responses are in the adjacent JSON files.

## Offline evidence and remaining work

- Full non-E2E regression: **888 passed**, 3 skipped, 5 expected failures and 14 E2E tests excluded. Lint and formatting passed. These counts measure implementation checks, not LLM delivery accuracy.
- Trama implementation tests: 9 JSONLogic and 9 split/join tests passed. The native renderer probe still fails the expected typed/indexed transport behavior. Passing the engine's existing tests does not qualify RiteSmith's generated request bodies.
- All 16 retained provider responses were replayed without paid calls, linked to their original SHA-256 hashes. Thirteen candidates fail current local parsing/assembly/graph checks. The other decoded responses remain functionally unqualified. The original unknown-usage call has no response to replay.
- Notification fixtures cover at least twelve eligible customers, ineligible customers, listing failure and sending failure. The strict reference script passes the ten-attempt rule, and a mutated script counting successes instead is rejected.

The implementation remains **incomplete for native Trama transport**. Before paid Stage 1, correct typed values in generated HTTP bodies, missing values, join indexing and serialized continuation state; exercise those bodies through the actual renderer and receiving contracts. Avoid reconstructing Java collection strings with regex. Preserve business references, real join envelopes, finite remaining work, callbacks, compensation and certified script dependencies.

The prior parallel C row cannot establish correctness: its old oracle allowed a Telegram message object and assumed indexed Mustache lookup. The original report and responses are unchanged; this is a separate corrected assessment. No production success or failure percentage follows from these observations.

Recommendation: **retain the restored strict-enabled production version and continue offline transport correction**. The new image is a review candidate, not a release. Stage 1 and Stage 2 remain entirely unexecuted; no new OpenAI token cost was incurred.

ARM verification: **23 directed tests passed**, 5 database-dependent cases excluded from that container run (covered by the complete local regression). The native template mismatch was reproduced against the exact JAR copied read-only from the deployed Trama image, without changing that service.

Production verification: the running image still matches the restored rollback image (`sha256:463b35a0b22bc851aa2008451bfbf97c6d9747ac00dac805af904d3c04a3f09c`), strict is explicitly enabled and health is OK. A private strict-enabled Compose rollback snapshot is preserved at `/home/thiago/ritesmith/deploy/docker-compose.strict-rollback-20261008.yml`. No candidate was promoted.

Frozen ARM candidate: `ritesmith:reliability-candidate-20261008`, image `sha256:97e9d76947e541993ee6872f6034a288bae85186067994a907b17a6c36874e49`. Its source content hash matches the two-stage manifest. The image is preserved on r2d2. Temporary evaluation databases, role, credential file and local tunnel were removed after offline checks.
