#!/usr/bin/env bash
# Measure generation quality of the checked-out revision with its shipped defaults.
#
# Needs OPENAI_API_KEY in the environment and podman. Uses a disposable Postgres and a
# loopback-only staging server; artifact reuse is withheld, no real tools are called.
# Two paid stages, each with a hard estimated-spend cap:
#   1. smoke: 6 triage tasks x 1 (cap SMOKE_USD, default 0.30)
#   2. full: all 51 tasks x REPS (default 3), only after you confirm (cap FULL_USD, default 2.00)
set -euo pipefail

: "${OPENAI_API_KEY:?export OPENAI_API_KEY first}"
SMOKE_USD=${SMOKE_USD:-0.30}
FULL_USD=${FULL_USD:-2.00}
REPS=${REPS:-3}
PORT=${PORT:-18081}
PG_PORT=${PG_PORT:-45432}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PY=${PY:-/home/thiago/Documents/projects/ritesmith/.venv/bin/python}
OUT=${OUT:-$ROOT/benchmarks/generation_latency/results/scorecard-$(date +%Y%m%d)-$(git -C "$ROOT" rev-parse --short HEAD)}
DB=postgresql+asyncpg://ritesmith:ritesmith@127.0.0.1:$PG_PORT/ritesmith_eval
mkdir -p "$OUT"
cd "$ROOT"

if ! podman container exists ritesmith-eval-pg; then
  podman run -d --rm --name ritesmith-eval-pg -e POSTGRES_USER=ritesmith \
    -e POSTGRES_PASSWORD=ritesmith -e POSTGRES_DB=ritesmith_eval \
    -p 127.0.0.1:$PG_PORT:5432 docker.io/library/postgres:16 >/dev/null
  until podman exec ritesmith-eval-pg pg_isready -U ritesmith >/dev/null 2>&1; do sleep 1; done
fi
RITESMITH_DATABASE_URL=$DB "$PY" -m alembic upgrade head >/dev/null

RITESMITH_DATABASE_URL=$DB "$PY" -m uvicorn benchmarks.generation_latency.app:app \
  --host 127.0.0.1 --port "$PORT" >"$OUT/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
until curl -sf "127.0.0.1:$PORT/health" >/dev/null; do sleep 1; done

run() {  # name, cap, extra args...
  local name=$1 cap=$2; shift 2
  "$PY" -m benchmarks.generation_latency.run --url "http://127.0.0.1:$PORT" --phase current \
    --entrypoints generate --max-usd "$cap" --output "$OUT/$name.jsonl" "$@" \
    | grep -E '^\{"task"|^estimated spend|^stopped at' || true
  "$PY" -m benchmarks.generation_latency.scorecard "$OUT/$name.jsonl" | tee "$OUT/$name.scorecard.txt"
}

echo "== smoke (cap US\$$SMOKE_USD)"
run smoke "$SMOKE_USD" --tasks triage --repetitions 1
"$PY" - "$OUT/smoke.jsonl" "$REPS" <<'EOF'
import json, sys
from benchmarks.generation_latency.pricing import estimated_cost
rows = [json.loads(l) for l in open(sys.argv[1])]
costs = [c for c in (estimated_cost(r["calls"]) for r in rows) if c is not None]
avg = sum(costs) / len(costs) if costs else float("nan")
print(f"\nsmoke mean US${avg:.4f}/request -> full run (51 tasks x {sys.argv[2]}) "
      f"projected ~US${avg * 51 * int(sys.argv[2]):.2f}")
EOF

read -r -p "Run the full suite with a US\$$FULL_USD cap? [y/N] " answer
[[ $answer == y ]] || exit 0
echo "== full (cap US\$$FULL_USD)"
run full "$FULL_USD" --tasks all --repetitions "$REPS"
echo "results: $OUT"
