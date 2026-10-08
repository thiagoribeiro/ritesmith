"""Compare compact JSON and Mermaid on the same staging code and evaluator."""

import argparse
import asyncio
import json
from argparse import Namespace
from pathlib import Path

from benchmarks.generation_latency.campaign import rank, write_report
from benchmarks.generation_latency.review import review
from benchmarks.generation_latency.run import MODELS, BenchmarkBlocked, run


async def campaign(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    # Preserve the complete original campaign. Correct assessment in a separate
    # file with raw-source hashes instead of overwriting measured failures.
    baseline_rows, _ = review(Path(args.baseline_directory), root / "baseline-review-v2.json")
    triage_baseline = [
        r
        for r in baseline_rows
        if r["phase"] == "baseline"
        and r["source_file"].startswith("triage-baseline-")
        and r["kind"] == "trama_workflow"
        and r["task"] in ("wf_reminder", "wf_week", "wf_parallel_prices")
        and r["entrypoint"] == "specialized"
        and r["concurrency"] == 1
        and r["repetition"] < 3
    ]
    if len(triage_baseline) != 9:
        raise ValueError("need the completed original nine workflow triage responses")
    baseline_quality = sum(r["valid"] for r in triage_baseline) / 9

    async def measure(phase, model, tasks="triage", repetitions=3, concurrency=1):
        path = root / f"{tasks}-{phase}-{model}-c{concurrency}.jsonl"
        if path.with_suffix(".summary.json").exists():
            return
        await run(
            Namespace(
                url=args.url,
                phase=phase,
                model=model,
                tasks=tasks,
                task_id=None,
                kind="trama_workflow",
                repetitions=repetitions,
                concurrency=concurrency,
                entrypoints=["plans"]
                if tasks == "compounds"
                else ["specialized"]
                if tasks == "triage"
                else ["specialized", "generate", "plans"],
                rotate_entrypoints=tasks == "all",
                missing_client_tests=False,
                output=str(path),
            )
        )

    for phase in ("full", "mermaid"):
        for model in MODELS:
            await measure(phase, model)
    rows = [
        json.loads(line) for p in root.glob("triage-*.jsonl") for line in p.read_text().splitlines()
    ]
    finalists = {
        phase: rank([r for r in rows if r["phase"] == phase], "trama_workflow", baseline_quality)
        for phase in ("full", "mermaid")
    }
    (root / "finalists.json").write_text(json.dumps(finalists, indent=2) + "\n")
    if not args.triage_only:
        for concurrency in (1, 2):
            for phase, models in finalists.items():
                for model in models:
                    await measure(phase, model, "all", 5, concurrency)
                    await measure(phase, model, "compounds", 5, concurrency)
    write_report(root, finalists, triage_only=args.triage_only)


async def controlled(args):
    try:
        await campaign(args)
    except BenchmarkBlocked as exc:
        write_report(Path(args.output), blocked=str(exc), triage_only=args.triage_only)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--baseline-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--triage-only", action="store_true")
    asyncio.run(controlled(parser.parse_args()))
