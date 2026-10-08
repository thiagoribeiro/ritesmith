"""Versioned assessment of preserved responses; never rewrite benchmark rows."""

import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.generation_latency.run import assess, summarize
from benchmarks.generation_latency.tasks import ASSESSMENT_VERSION, BY_ID


def review(root: Path, output: Path):
    rows, changes, sources = [], [], {}
    for source in sorted(root.glob("*.jsonl")):
        sources[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
        for line in source.read_text().splitlines():
            row = json.loads(line)
            original = row["errors"]
            errors = original
            if row["http_status"] < 400 and row["http_status"] > 0:
                errors = assess(BY_ID[row["task"]], row["response"], row["entrypoint"])
                if "generation acceptance gate failed" in original:
                    errors.append("generation acceptance gate failed")
            if errors != original:
                changes.append(
                    {
                        "source": source.name,
                        "task": row["task"],
                        "repetition": row["repetition"],
                        "entrypoint": row["entrypoint"],
                        "original_errors": original,
                        "reviewed_errors": errors,
                    }
                )
            rows.append({**row, "source_file": source.name, "errors": errors, "valid": not errors})
    report = {
        "assessment_version": ASSESSMENT_VERSION,
        "raw_sha256": sources,
        "changes": changes,
        "groups": summarize(rows),
        "qualification": "Independent versioned review; timings and raw responses unchanged. "
        "Corrects BTC capability equivalence and detects counter-reset cycles. "
        "Structural/limited semantic checks do not prove live workflow execution.",
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return rows, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    review(args.root, args.output)
