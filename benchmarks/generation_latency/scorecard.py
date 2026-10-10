"""Pass-rate scorecard for one or more run.py JSONL files.

python -m benchmarks.generation_latency.scorecard results/a.jsonl [results/b.jsonl ...]

Reports, per artifact kind: valid/total, p50/p95 latency of all requests, estimated
spend (failures included) and spend per valid artifact; then per-task pass counts and
the most common error categories. Unknown usage makes the spend a lower bound.
"""

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from benchmarks.generation_latency.pricing import estimated_cost


def _pct(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * q) - 1)] if values else float("nan")


def _category(error):
    # Collapse instance-specific details (node ids, capability names) into a category.
    error = str(error)
    if error.startswith("missing capability"):
        return "missing capability"
    return re.sub(r" at \S+$|: .*$", "", error)[:70]


def scorecard(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    print(f"\n== {path}  ({len(rows)} requests)")
    by_kind = defaultdict(list)
    for row in rows:
        by_kind[row["kind"]].append(row)
    print(
        f"{'kind':16} {'valid':>9} {'rate':>6} {'p50 s':>7} {'p95 s':>7} {'USD':>8} {'USD/valid':>10}"
    )
    for kind, group in sorted(by_kind.items()):
        valid = sum(r["valid"] for r in group)
        costs = [estimated_cost(r["calls"]) for r in group]
        known = sum(c for c in costs if c is not None)
        unknown = sum(c is None for c in costs)
        times = [r["duration_s"] for r in group]
        per_valid = f"{known / valid:.4f}" if valid else "n/a"
        print(
            f"{kind:16} {valid:>4}/{len(group):<4} {valid / len(group):>6.0%} "
            f"{_pct(times, 0.5):>7.1f} {_pct(times, 0.95):>7.1f} {known:>8.4f} {per_valid:>10}"
            + (f"  ({unknown} unknown-cost)" if unknown else "")
        )
    tasks = defaultdict(lambda: [0, 0])
    errors = Counter()
    for row in rows:
        tasks[(row["kind"], row["task"])][0] += row["valid"]
        tasks[(row["kind"], row["task"])][1] += 1
        if not row["valid"]:
            errors.update({_category(e) for e in row["errors"]} or {"(none recorded)"})
    failing = [(k, v) for k, v in sorted(tasks.items()) if v[0] < v[1]]
    print(f"\ntasks with failures ({len(failing)}/{len(tasks)}):")
    for (kind, task), (ok, total) in failing:
        print(f"  {ok}/{total}  {task}")
    print("\nfailure categories (requests affected):")
    for error, count in errors.most_common(12):
        print(f"  {count:>3}  {error}")


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        scorecard(argument)
