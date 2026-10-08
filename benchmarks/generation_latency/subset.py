"""Bounded evaluation: <=43 new HTTP requests; reuse the preserved comparison.

Target: 95% valid within 10s and >=50% lower estimated cost per valid artifact.
The subset does not establish a full-corpus or seven-day production SLO.
"""

import argparse
import asyncio
import json
import math
from argparse import Namespace
from collections import defaultdict
from pathlib import Path

from benchmarks.generation_latency.campaign import read_rows
from benchmarks.generation_latency.pricing import estimated_cost
from benchmarks.generation_latency.review import review
from benchmarks.generation_latency.run import BenchmarkBlocked, run
from benchmarks.generation_latency.tasks import TRIAGE
from benchmarks.generation_latency.version import software_manifest


def measurements(rows, seconds):
    calls = [c for r in rows for c in r["calls"]]
    cost = estimated_cost(calls)
    valid = sum(r["valid"] for r in rows)
    times = sorted(r["duration_s"] for r in rows)
    return {
        "requests": len(rows),
        "valid_fraction": valid / len(rows),
        "p95_s": times[math.ceil(len(times) * 0.95) - 1],
        "valid_within_target_fraction": sum(r["valid"] and r["duration_s"] <= seconds for r in rows)
        / len(rows),
        "estimated_usd": cost,
        "cost_per_valid_artifact_usd": cost / valid if valid and cost is not None else None,
        "cached_tokens": sum(c.get("cached_tokens", 0) for c in calls),
        "fallback_requests": sum(any(c.get("fallback") for c in r["calls"]) for r in rows),
    }


def comparison(rows, baseline, seconds=10, reduction=0.5):
    def family(row):
        if row["task"].startswith("plan_"):
            return "dependency"
        return "script" if row["kind"] == "luau_script" else "workflow"

    groups = defaultdict(list)
    for r in rows:
        groups[
            (
                r["phase"],
                r["model"],
                r["kind"],
                r["entrypoint"],
                r["concurrency"],
                r.get("client_tests", True),
                r.get("software_sha256") or "unrecorded",
                family(r),
            )
        ].append(r)
    results = []
    for key, samples in sorted(groups.items()):
        # Compare the same scenarios, artifact kind, HTTP path, supplied-tests
        # group and concurrency. Never substitute a cheaper/easier baseline.
        tasks = {r["task"] for r in samples}
        reference = [
            r
            for r in baseline
            if r["task"] in tasks
            and r["kind"] == key[2]
            and r["entrypoint"] == key[3]
            and r["concurrency"] == key[4]
            and r.get("client_tests", True) == key[5]
            and family(r) == key[7]
        ]
        observed = measurements(samples, seconds)
        base = (
            measurements(reference, seconds)
            if reference and {r["task"] for r in reference} == tasks
            else None
        )
        current_cost = observed["cost_per_valid_artifact_usd"]
        base_cost = base["cost_per_valid_artifact_usd"] if base else None
        saving = 1 - current_cost / base_cost if current_cost is not None and base_cost else None
        results.append(
            {
                "phase": key[0],
                "model": key[1],
                "kind": key[2],
                "entrypoint": key[3],
                "concurrency": key[4],
                "client_tests": key[5],
                "software_sha256": key[6],
                "scenario_family": key[7],
                "tasks": sorted(tasks),
                "observed": observed,
                "baseline": base,
                "cost_reduction_fraction": saving,
                "latency_gate": observed["valid_within_target_fraction"] >= 0.95,
                "quality_gate": base is not None
                and observed["valid_fraction"] >= base["valid_fraction"],
                "cost_gate": saving is not None and saving >= reduction,
            }
        )
    for result in results:
        result["qualified_on_subset"] = all(
            result[g] for g in ("latency_gate", "quality_gate", "cost_gate")
        )
    return results


def write_report(root, baseline, previous, args, status="evaluated", blocker=None):
    rows = read_rows(root)
    baseline = baseline + [r for r in rows if r["phase"] == "baseline"]
    groups = comparison(
        [r for r in rows if r["phase"] != "baseline"],
        baseline,
        args.target_seconds,
        args.cost_reduction,
    )
    previous_triage = [
        r for r in previous if r["task"] in TRIAGE and r["source_file"].startswith("triage-")
    ]
    report = {
        "status": status,
        "blocker": blocker,
        "target_seconds": args.target_seconds,
        "required_valid_fraction": 0.95,
        "required_cost_reduction": args.cost_reduction,
        "new_request_limit": 43,
        "new_requests": len(rows),
        "groups": groups,
        "preserved_triage": comparison(
            previous_triage, baseline, args.target_seconds, args.cost_reduction
        ),
        "promotion": False,
        "analysis_version": "latency-cost-subset-v2",
        "analysis_software": software_manifest(),
        "estimated_total_usd": estimated_cost([c for r in rows for c in r["calls"]]),
        "llm_calls": sum(len(r["calls"]) for r in rows),
        "failures": [
            {k: r[k] for k in ("task", "phase", "model", "entrypoint", "duration_s", "errors")}
            for r in rows
            if not r["valid"] or r["duration_s"] > args.target_seconds
        ],
        "cache_groups": {
            label: comparison(
                [
                    r
                    for r in rows
                    if r["phase"] != "baseline"
                    and bool(sum(c.get("cached_tokens", 0) for c in r["calls"])) == cached
                ],
                [
                    r
                    for r in baseline
                    if bool(sum(c.get("cached_tokens", 0) for c in r["calls"])) == cached
                ],
                args.target_seconds,
                args.cost_reduction,
            )
            for label, cached in (("uncached", False), ("cached", True))
        },
        "qualification": "Representative subset; source versions reported separately. New compact/Mermaid arms use the same code and evaluator. "
        "Baseline comparison requires the same tasks/path/concurrency/test-supply group; missing baseline or unknown billed usage cannot pass cost. "
        "Only staging and substituted tools/limited workflow semantic checks. No full-corpus or production SLO claim.",
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


async def campaign(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    previous, _ = review(Path(args.baseline_directory), root / "preserved-review-v2.json")
    baseline = [r for r in previous if r["phase"] == "baseline"]

    async def measure(phase, model, ids, repetitions, entrypoints, rotate=True, missing=False):
        path = (
            root
            / f"{phase}-{model}-{'dependency' if ids[0].startswith('plan_') else 'missing-tests' if missing else 'workflows'}.jsonl"
        )
        await run(
            Namespace(
                url=args.url,
                phase=phase,
                model=model,
                tasks="all",
                task_id=ids,
                kind=None,
                repetitions=repetitions,
                concurrency=1,
                entrypoints=entrypoints,
                rotate_entrypoints=rotate,
                missing_client_tests=missing,
                output=str(path),
                target_seconds=args.target_seconds,
            )
        )

    try:
        await measure("baseline", "gpt-5-mini", ["plan_t1_celsius_to_fahrenheit"], 1, ["plans"])
        await measure(
            "baseline", "gpt-5-mini", ["t3_notify_inactive"], 1, ["generate"], missing=True
        )
        for model in ("gpt-5-mini", "gpt-5.4-mini"):
            for phase in ("full", "mermaid"):
                await measure(
                    phase,
                    model,
                    ["wf_reminder", "wf_week", "wf_parallel_prices"],
                    3,
                    ["specialized", "generate", "plans"],
                )
                await measure(phase, model, ["plan_t1_celsius_to_fahrenheit"], 1, ["plans"])
        await measure("full", "gpt-5-mini", ["t3_notify_inactive"], 1, ["generate"], missing=True)
    except BenchmarkBlocked as exc:
        write_report(root, baseline, previous, args, "blocked", str(exc))
        raise SystemExit(2) from exc
    write_report(root, baseline, previous, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--baseline-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-seconds", type=float, default=10)
    parser.add_argument("--cost-reduction", type=float, default=0.5)
    asyncio.run(campaign(parser.parse_args()))
