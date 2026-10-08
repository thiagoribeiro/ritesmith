"""Controlled comparison and acceptance report; staging only, no promotion.

Stages: original prompt/default effort; model+low; selected examples; full.
Triage uses 3 repetitions. The best two models per kind are evaluated on all
31+20 scenarios with 5 repetitions and concurrency 1/2. HTTP entrypoints rotate
across those repetitions and are always assessed as separate SLO groups.
"""

import argparse
import asyncio
import json
import math
from argparse import Namespace
from collections import defaultdict
from pathlib import Path

from benchmarks.generation_latency.pricing import estimated_cost
from benchmarks.generation_latency.run import MODELS, BenchmarkBlocked, run, summarize


def read_rows(root, pattern="*.jsonl"):
    return [
        json.loads(line)
        for p in sorted(root.glob(pattern))
        for line in p.read_text().splitlines()
        if line
    ]


def rank(rows, kind, baseline):
    groups = defaultdict(list)
    for row in rows:
        if row["kind"] == kind:
            groups[row["model"]].append(row)
    ranking = []
    for model, samples in groups.items():
        valid_rate = sum(r["valid"] for r in samples) / len(samples)
        quality_ok = valid_rate >= baseline
        if not quality_ok:
            continue
        times = sorted(r["duration_s"] for r in samples)
        p95 = times[math.ceil(len(times) * 0.95) - 1]
        cost = estimated_cost([c for r in samples for c in r["calls"]])
        ranking.append(
            (not quality_ok, p95, -valid_rate, cost if cost is not None else float("inf"), model)
        )
    return [r[4] for r in sorted(ranking)[:2]]


def write_report(root, finalists=None, *, blocked=None, triage_only=False):
    rows = read_rows(root)
    report = {
        "status": "blocked" if blocked else "triage_only" if triage_only else "evaluated",
        "blocker": blocked,
        "finalists": finalists or {},
        "groups": summarize(rows),
        "cache_groups": {
            label: summarize(
                [
                    r
                    for r in rows
                    if bool(sum(c.get("cached_tokens", 0) for c in r["calls"])) == cached
                ]
            )
            for label, cached in (("uncached", False), ("cached", True))
        },
        "failures": [
            {
                k: r[k]
                for k in (
                    "task",
                    "phase",
                    "model",
                    "entrypoint",
                    "concurrency",
                    "duration_s",
                    "errors",
                )
            }
            for r in rows
            if not r["valid"] or r["duration_s"] >= 5
        ],
        "promotion": False,
        "qualification": (
            "Separate staging; no artifact reuse/few-shot reuse. Production HTTP services "
            "and LunarDyson with substituted tools. Workflow graph/semantic checks; external "
            "effects are not executed. Caller test inputs and independent model-generated tests "
            "are reported in separate groups. Rotation distributes five repetitions across "
            "interfaces, not five per interface. Blocked/triage-only reports cannot qualify "
            "a model or establish quality against the full baseline. No seven-day production "
            "monitoring or execution of external workflow effects is claimed."
        ),
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


async def campaign(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)

    async def measure(
        phase,
        model,
        *,
        kind=None,
        tasks="triage",
        repetitions=3,
        concurrency=1,
        missing_tests=False,
    ):
        suffix = "-missing-tests" if missing_tests else ""
        filename = f"{tasks}-{phase}-{model}-{kind or 'mixed'}-c{concurrency}{suffix}.jsonl"
        path = root / filename
        if path.with_suffix(".summary.json").exists():
            print("Resume: retaining completed file " + filename, flush=True)
            return
        await run(
            Namespace(
                url=args.url,
                phase=phase,
                model=model,
                tasks=tasks,
                task_id=None,
                kind=kind,
                repetitions=repetitions,
                concurrency=concurrency,
                entrypoints=["plans"]
                if tasks == "compounds"
                else ["specialized"]
                if tasks == "triage"
                else ["specialized", "generate", "plans"],
                rotate_entrypoints=tasks == "all",
                missing_client_tests=missing_tests,
                output=str(path),
            )
        )

    await measure("baseline", "gpt-5-mini")
    for model in MODELS:
        await measure("model", model)
    for phase in ("filtered", "full"):
        for model in MODELS:
            await measure(phase, model)
    rows = read_rows(root, "triage-*.jsonl")
    baseline_rows = read_rows(root, "*baseline*.jsonl")
    baseline = {
        kind: sum(r["valid"] for r in baseline_rows if r["kind"] == kind)
        / max(1, sum(r["kind"] == kind for r in baseline_rows))
        for kind in ("luau_script", "trama_workflow")
    }
    finalists = {
        kind: rank([r for r in rows if r["phase"] == "full"], kind, baseline[kind])
        for kind in baseline
    }
    (root / "finalists.json").write_text(json.dumps(finalists, indent=2) + "\n")
    if not args.triage_only:
        for concurrency in (1, 2):
            await measure(
                "baseline", "gpt-5-mini", tasks="all", repetitions=5, concurrency=concurrency
            )
            for kind, models in finalists.items():
                for model in models:
                    await measure(
                        "full",
                        model,
                        kind=kind,
                        tasks="all",
                        repetitions=5,
                        concurrency=concurrency,
                    )
                    if kind == "trama_workflow":
                        await measure(
                            "full",
                            model,
                            kind=kind,
                            tasks="compounds",
                            repetitions=5,
                            concurrency=concurrency,
                        )
            for model in finalists["luau_script"]:
                await measure(
                    "full",
                    model,
                    kind="luau_script",
                    repetitions=5,
                    concurrency=concurrency,
                    missing_tests=True,
                )
    write_report(root, finalists, triage_only=args.triage_only)


async def controlled_campaign(args):
    try:
        await campaign(args)
    except BenchmarkBlocked as exc:
        write_report(Path(args.output), blocked=str(exc), triage_only=args.triage_only)
        print(str(exc), flush=True)
        raise SystemExit(2) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--output", required=True)
    parser.add_argument("--triage-only", action="store_true")
    asyncio.run(controlled_campaign(parser.parse_args()))


if __name__ == "__main__":
    main()
