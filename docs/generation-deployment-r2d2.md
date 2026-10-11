# r2d2 generation deployment — 2026-10-08

The owner authorized a direct update of the single-user r2d2 deployment with an
executable rollback. The update was deployed, then **withdrawn on October 8, 2026**
after generation failures in the bounded typed-decisions comparison. The previous
image is running again, with strict certification explicitly kept enabled.
See [the rollback and verification record](generation-rollback-r2d2.md).
Historical benchmark conclusions remain unchanged.

## Withdrawn configuration

- Scripts: `gpt-5-mini`, reasoning effort `low`.
- Workflows: `gpt-5.4-mini`, reasoning effort `low`.
- Typed prompts, Luau body assembly, semantic workflows and compact graphs enabled.
- Mermaid disabled; JSON compact graphs remain the advanced-topology escape.
- One generation plus one recovery, 30-second request deadline, 10-second SLO.
- Strict Luau certification required. Classification/test generation retain the
  existing fast model. The existing generation model remains the fallback.

No database schema migration is required. Existing credentials, mounts, policies,
external integrations and the Compose project are retained.

## Rollback

From the workstation:

```bash
ssh r2d2 /home/thiago/ritesmith/deploy/rollback-generation.sh
```

To check resources without switching:

```bash
ssh r2d2 '/home/thiago/ritesmith/deploy/rollback-generation.sh --check'
```

The command restores the previous `.env`, Compose file and exact cached Docker
image, recreates only RiteSmith, and checks `/health`. Current application data is
retained. It does not run migrations or restore the database automatically.

Snapshot: `/home/thiago/ritesmith/deploy/rollbacks/generation-20261008-144227/`.
The snapshot retains the original image tag, configuration, source archive and
custom-format PostgreSQL backup. Keep the rollback image tag until it is no longer
needed. Source and database archives are recovery resources, not automatic rollback
steps. New artifacts that use the new stat helpers depend on the updated runtime;
regenerate those artifacts with the older generator if reverting the runtime.

## Verification

Deployment verification uses `/health`, database connectivity, runtime settings,
HTTP validation, strict LunarDyson execution on ARM and deterministic compilation
of the nine workflow patterns. Missing external fixtures must stop validation
without accessing live services. These checks make no paid LLM requests and do
not qualify latency or token/cost savings for the new deployment. Ordinary user
requests provide that evidence through the existing telemetry.

## Completed deployment checks

- Release image: `ritesmith:generation-20261008-144227` (`arm64`).
- Strict Luau body assembly/execution, all nine Trama examples and rejection of
  missing external fixtures passed inside the built ARM image.
- Live `/health` and `/validate` passed after the container switch.
- PostgreSQL connectivity and registration of `stat.chain_step`/`stat.collect` passed.
- Only the RiteSmith service was recreated, without running migrations.
- The rollback resource check passed before and after deployment.
- No benchmarks or paid LLM generation calls were made.

The live release manifest is stored at
`/home/thiago/ritesmith/deploy/generation-release.json`; it records the image
digest, UTC deployment timestamp, enabled configuration and completed checks.
