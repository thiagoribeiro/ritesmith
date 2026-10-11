# r2d2 deployment: semantic workflows + Trama typed JSON (2026-10-10)

RiteSmith and Trama were upgraded together on r2d2. The new RiteSmith emits JSON bodies
that need Trama's mandatory typed JSON renderer, and older Trama versions cannot render
them. **Roll back both services together**, never only one.

| Service | Before | After |
|---|---|---|
| RiteSmith | `93e1977` + uncommitted withdrawn-release files, image `463b35a0b22b` | `a89c6a4` (`feat/semantic-workflow-generation`, PR #7 on PR #6), image `ritesmith:semantic-a89c6a4` → `ritesmith:latest` |
| Trama | 2.2.1, image `1ef6c1ff3465` | 2.3.0 + typed JSON (`fa3aef0`, Trama PR #54), image `trama:2.3.0-typed-json-fa3aef0` → `trama:latest` |

Effective RiteSmith settings: `generation_semantic_workflows=true`,
`generation_deadline_seconds=60`, `require_strict_typecheck=true`,
delegation to `http://trama:9081`.

## What changed on the host

- **Trama image.** Built from the `trama:2.2.1` base, with `bin/` and `lib/` replaced by
  the locally built `installDist` of `fa3aef0` (`deploy/Dockerfile.trama-reliability`;
  `trama-2.3.0.jar` sha256 `d0602d79…8d80`).
- **Trama container.** Recreated alone: `docker compose -f /opt/edge/compose.edge.yml up -d --no-deps trama`.
  No database migration. `REDIS_SHARDING_VIRTUALSHARDCOUNT=1` stays explicit, so the
  2.3.0 shard-default change does not apply.
- **RiteSmith repository.** `~/ritesmith` now tracks `feat/semantic-workflow-generation`,
  delivered by git bundle because the host cannot fetch from GitHub. The previous
  uncommitted files were archived first; see Backups.
- **RiteSmith environment.** `RITESMITH_REQUIRE_STRICT_TYPECHECK=true` moved from a local
  edit of `deploy/docker-compose.yml` into `deploy/.env`, which is the file the container
  reads. The root `~/ritesmith/.env` is not read by the container.
- **Migrations.** `alembic upgrade head` ran, with no new revisions.

## Live workflows

The USD/BRL one-week monitor (`plan_01M4BGABKAR50KA6PPT7KKF2FC`) was cancelled and
recreated as `plan_01M4KXVP81PXFQSBW62Z3RW749`. Its compiled workflow ran under the
new Trama: 15 passes every 6 h, against the original 4.995799 baseline from 07/10.

The old workflow never captured its baseline: `initial_rate` stayed `""` in every pass,
so its 10% alert could not fire. Its Trama execution fails at its next `/trama/execute`
call, which refuses cancelled plans.

Brian plugin and LoomHarbor arrival workflows carry no body templates and are unaffected.
A `brian-workflow-result` execution succeeded on the new engine right after the switch.

## Verification

- `/health` returned ok. Trama `/readyz` and `/healthz` returned 200, with no error logs.
- `POST /generate` of a Celsius→Fahrenheit Luau script, without caller-supplied tests,
  returned 200 in 5.3 s with `certification=strict`. This is the path that previously
  failed with HTTP 500.
- `POST /plans` in execute mode for the monitor returned 201 in 11.0 s. The first pass
  executed in Trama with typed values: a numeric rate, the numeric baseline kept in loop
  state, and the counter moving from 15 to 14 remaining.

Known follow-up: the generated monitor compares the rate with `llm.evaluate`, which makes
an LLM call per check. A deterministic JSONLogic condition can express the 10% threshold.
The semantic rules should steer numeric thresholds to conditions.

## Backups

The backups are in `~/backups/deploy-semantic-20261010/` (mode 700):

- `trama.dump` and `ritesmith.dump`: `pg_dump -Fc` of both databases.
- `deploy.env`, `ritesmith-compose.yml` and `compose.edge.yml`: the previous configuration.
- `ritesmith-head.txt` and `ritesmith-worktree.patch`: the previous repository state.
- `ritesmith-untracked.tgz` and `untracked.txt`: the 581 files that were untracked before.

The previous images are tagged `ritesmith:backup-20261010` and `trama:2.2.1-backup-20261010`.

## Rollback (both services)

```bash
ssh r2d2 '
  docker tag trama:2.2.1-backup-20261010 trama:latest
  cd /opt/edge && docker compose -f compose.edge.yml up -d --no-deps trama
  docker tag ritesmith:backup-20261010 ritesmith:latest
  cd ~/ritesmith && docker compose -f deploy/docker-compose.yml up -d --force-recreate --no-deps ritesmith
'
```

After a rollback, workflows compiled by the new RiteSmith, including the recreated USD/BRL
monitor, will fail under Trama 2.2.1. Cancel their plans first. The strict-typecheck
setting stays in `deploy/.env`. No database restore is needed unless data was corrupted;
the dumps are a last resort.
