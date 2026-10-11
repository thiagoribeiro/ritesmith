# Generation rollback on r2d2 — October 8, 2026

The generation-improvements release was withdrawn after the reference variant
failed all three observed cases in the bounded typed-decisions comparison.
Those three isolated observations do not establish a 100% production failure
rate, but the assembly and workflow errors warranted withdrawing the promotion.
Offline execution of supplied/model-mocked candidates did not establish actual
generation reliability for the deployed revision. Promotion relied on incomplete
evidence.

## Restored service

- Restored image:
  `sha256:463b35a0b22bc851aa2008451bfbf97c6d9747ac00dac805af904d3c04a3f09c`.
- Withdrawn image retained as `ritesmith:generation-20261008-144227`:
  `sha256:3674b56b2d208411697311ab41a528a25372cfceba44773128644598ca97b0d7`.
- The rollback script restored the previous image, environment and Compose file.
  Only RiteSmith was recreated. Current application data was retained; no database
  backup was restored and no migration was run.
- The original configuration did not require strict certification. The restored
  Compose service explicitly sets `RITESMITH_REQUIRE_STRICT_TYPECHECK=true` to
  preserve this requirement. Running the original rollback script again restores
  its saved configuration; retain this explicit override when repeating recovery.
- The withdrawn environment and Compose file were retained privately under
  `/home/thiago/ritesmith/deploy/rollbacks/withdrawn-generation-*/` before rollback.
- New schema constraints, semantic generation and body-assembly features are not
  present in the restored image. The experimental implementation remains in the
  working tree for correction and review.

## Verification and limits

`/health` returned `ok`. A known full Celsius script passed live `/validate`,
including `strict_type_check` and two controlled execution cases: 0°C → 32°F
and 100°C → 212°F. Strict enforcement was confirmed in the container environment.
These checks made **no paid generation calls and no external effects**.

This verifies the restored validator and known code, not the accuracy of new LLM
generations. No production success percentage or latency/cost target is claimed
for the restored service. The previous runtime also has fewer generation and
recovery controls than the withdrawn release; a rollback is recovery, not a new
performance qualification.

The server record is `/home/thiago/ritesmith/deploy/generation-rollback.json`,
preserved as [rollback.json](../benchmarks/generation_latency/results/typed-decisions-20261008/rollback.json).
The frozen [comparison report](../benchmarks/generation_latency/results/typed-decisions-20261008/RESULTS.md)
and all failed responses remain unchanged. Production was untouched during that
comparison; this rollback occurred afterward.

Before another promotion, correct the conflicting script/body instructions and
workflow references, add offline reproduction of the retained failures, and
verify complete real generation on the release candidate. Unit/regression counts,
schema conformance and health checks must not be reported as generation accuracy.
Any further paid evaluation requires new authorization; the bounded campaign is
closed after unknown usage and must not be resumed or rerun.

## Progressive recovery verification

A subsequent owner authorization covers the progressive 3 + 24 request protocol
in [generation-reliability-validation.md](generation-reliability-validation.md).
Its offline native transport check currently blocks dispatch; no new paid cell
was executed. The candidate image is preserved separately and production has not
been promoted. The private server snapshot
`/home/thiago/ritesmith/deploy/docker-compose.strict-rollback-20261008.yml`
retains the strict-enabled rollback configuration.
