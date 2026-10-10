"""Re-grade run.py JSONL with native-format normalization only; raw rows are untouched.

python -m benchmarks.generation_latency.regrade results/a.jsonl [...] [--exclude TASK ...]

Newer engine contracts emit native Trama fields the v2 grader predates: sleep as
durationMillis and callback mode "async". They are mapped to the grader's names before
the same checks run. HTTP failures and failed acceptance gates are kept as recorded.
Writes <name>.regraded.jsonl next to each input and prints its scorecard.
"""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from benchmarks.generation_latency.run import assess
from benchmarks.generation_latency.scorecard import scorecard
from benchmarks.generation_latency.tasks import BY_ID


def _normalized(content):
    definition = json.loads(content)
    for node in definition.get("nodes", []):
        if node.get("kind") == "sleep" and "durationMillis" in node:
            node["durationSeconds"] = node["durationMillis"] / 1000
        action = node.get("action")
        if isinstance(action, dict) and action.get("mode") == "async":
            action["mode"] = "async-http-callback"
    return json.dumps(definition)


def regrade(row):
    if row["http_status"] >= 400 or not row["http_status"]:
        return row
    response = deepcopy(row["response"])
    for artifact in response.get("artifacts", []) + [response.get("artifact")]:
        if artifact and artifact.get("artifact_type") == "trama_workflow":
            artifact["content"] = _normalized(artifact["content"])
    errors = assess(BY_ID[row["task"]], response, row["entrypoint"])
    if "generation acceptance gate failed" in row["errors"]:
        errors.append("generation acceptance gate failed")
    return {**row, "errors": errors, "valid": not errors, "regraded": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    for path in map(Path, args.paths):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        out = path.with_suffix(".regraded.jsonl")
        out.write_text(
            "".join(
                json.dumps(regrade(row), ensure_ascii=False) + "\n"
                for row in rows
                if row["task"] not in args.exclude
            )
        )
        scorecard(out)


if __name__ == "__main__":
    main()
