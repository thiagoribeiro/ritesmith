"""Measure deterministic compilation overhead, separately from HTTP/LLM latency."""

import argparse
import json
import math
import platform
import time
from pathlib import Path

from ritesmith.workflows.compact import compile_workflow
from ritesmith.workflows.examples import EXAMPLE_IDS, example
from ritesmith.workflows.mermaid import compile_mermaid, render_mermaid
from ritesmith.workflows.validator import WorkflowValidator


def measure(repetitions=500):
    rows = []
    for id_ in EXAMPLE_IDS:
        compact = example(id_)
        mermaid = render_mermaid(compact)
        base_url = "http://ritesmith:8081"
        native = compile_workflow(compact, base_url)
        assert compile_mermaid(mermaid, base_url) == native
        for format_, source, compile_ in (
            ("compact", compact, compile_workflow),
            ("mermaid", mermaid, compile_mermaid),
        ):
            times = []
            for _ in range(repetitions):
                start = time.perf_counter()
                result = compile_(source, base_url)
                assert not WorkflowValidator().validate(result)
                times.append((time.perf_counter() - start) * 1000)
            times.sort()
            rows.append(
                {
                    "pattern": id_,
                    "format": format_,
                    "repetitions": repetitions,
                    "compile_validate_p95_ms": times[math.ceil(repetitions * 0.95) - 1],
                    "definition_json_bytes": len(
                        json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()
                    ),
                }
            )
    return {
        "machine": platform.machine(),
        "python": platform.python_version(),
        "rows": rows,
        "qualification": "Only deterministic parsing/compilation/structural validation of examples; "
        "not artifact-generation latency or model quality.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=500)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(measure(args.repetitions), indent=2) + "\n")
