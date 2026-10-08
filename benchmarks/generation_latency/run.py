"""Measure complete HTTP responses; write every attempt, including failures.

Run in the separate staging container, never against production: python -m
benchmarks.generation_latency.run --phase full --model gpt-5.4-mini ...
"""

import argparse
import asyncio
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path

import httpx

from benchmarks.generation_latency.pricing import estimated_cost
from benchmarks.generation_latency.tasks import (
    ASSESSMENT_VERSION,
    COMPOUND_TASKS,
    TASKS,
    TRIAGE,
    workflow_checks,
)

MODELS = ("gpt-5-mini", "gpt-5", "gpt-5.2", "gpt-5.4-mini")


class BenchmarkBlocked(RuntimeError):
    """Account quota prevents the remaining requests; recorded failures stay in the run."""


def append_record(path, row):
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def payload(task, entrypoint, missing_tests=False):
    script = task["kind"] == "luau_script"
    intent = ("Generate a reusable Luau function: " if script else "") + task["goal"]
    context = dict(task.get("context", {}))
    script_task = task.get("script_task", task)
    if "test_cases" in script_task and not missing_tests:
        # Caller supplies test inputs; expected outputs/call budgets remain hidden.
        context["test_cases"] = [{"input": c["input"]} for c in script_task["test_cases"]]
    body = {"intent": intent, "context": context, "constraints": {"reuse_policy": "force_new"}}
    if script:
        body.update(input_schema=task["input_schema"], output_schema=task["output_schema"])
    if entrypoint == "specialized":
        return ("/generate/lua" if script else "/generate/trama-workflow"), {**body, "save": True}
    if entrypoint == "generate":
        return "/generate", {**body, "save": True}
    body.pop("input_schema", None)
    body.pop("output_schema", None)
    body["constraints"].pop("reuse_policy")
    body.update(mode="persist", reuse_policy="force_new")
    return "/plans", body


def assess(task, body, entrypoint):
    artifacts = body.get("artifacts", []) if entrypoint == "plans" else [body.get("artifact")]
    validations = body.get("validations", []) if entrypoint == "plans" else [body.get("validation")]
    errors = []
    if not artifacts or any(not a for a in artifacts):
        return ["no completed artifact"]
    if not validations or any(not v or not v.get("valid") for v in validations):
        errors.append("validation failed or absent")
    if entrypoint == "plans" and any(s.get("status") == "failed" for s in body.get("steps", [])):
        errors.append("plan step failed")
    if task["kind"] not in {a.get("artifact_type") for a in artifacts}:
        errors.append("wrong artifact kind")
    for artifact in artifacts:
        if artifact.get("artifact_type") == "luau_script":
            if artifact.get("metadata", {}).get("certification") != "strict":
                errors.append("missing strict certification")
        elif artifact.get("artifact_type") == "trama_workflow":
            try:
                errors.extend(workflow_checks(task, json.loads(artifact["content"])))
            except (ValueError, KeyError, TypeError) as exc:
                errors.append("invalid workflow JSON: " + str(exc))
    if task.get("script_task"):
        script_ids = {
            a["artifact_id"] for a in artifacts if a.get("artifact_type") == "luau_script"
        }
        if not script_ids:
            errors.append("missing completed script dependency")
        bound = False
        for artifact in artifacts:
            if artifact.get("artifact_type") == "trama_workflow":
                try:
                    bound |= any(
                        n.get("action", {}).get("request", {}).get("body", {}).get("artifact_id")
                        in script_ids
                        for n in json.loads(artifact["content"]).get("nodes", [])
                    )
                except (ValueError, KeyError, TypeError):
                    pass  # malformed workflow is already reported above
        if not bound:
            errors.append("workflow not bound to its persisted script dependency")
    return errors


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        key = (
            row["phase"],
            row["model"],
            row["kind"],
            row["entrypoint"],
            row["concurrency"],
            row.get("client_tests", True),
            row.get("software_sha256") or "unrecorded",
        )
        groups[key].append(row)
    result = []
    for key, samples in sorted(groups.items()):
        times = sorted(s["duration_s"] for s in samples)
        result.append(
            dict(
                zip(
                    (
                        "phase",
                        "model",
                        "kind",
                        "entrypoint",
                        "concurrency",
                        "client_tests",
                        "software_sha256",
                    ),
                    key,
                    strict=True,
                ),
                requests=len(samples),
                p95_s=times[math.ceil(len(times) * 0.95) - 1],
                valid_fraction=sum(s["valid"] for s in samples) / len(samples),
                valid_under_5s_fraction=sum(s["valid"] and s["duration_s"] < 5 for s in samples)
                / len(samples),
                target_seconds=samples[0].get("target_seconds", 5),
                valid_within_target_fraction=sum(
                    s["valid"] and s["duration_s"] <= s.get("target_seconds", 5) for s in samples
                )
                / len(samples),
                fallback_fraction=sum(any(c.get("fallback") for c in s["calls"]) for s in samples)
                / len(samples),
                prompt_tokens=sum(c.get("prompt_tokens", 0) for s in samples for c in s["calls"]),
                cached_tokens=sum(c.get("cached_tokens", 0) for s in samples for c in s["calls"]),
                estimated_usd=estimated_cost([c for s in samples for c in s["calls"]]),
                completion_tokens=sum(
                    c.get("completion_tokens", 0) for s in samples for c in s["calls"]
                ),
            )
        )
    return result


async def run(args):
    tasks = (
        COMPOUND_TASKS
        if args.tasks == "compounds"
        else [t for t in TASKS if args.tasks == "all" or t["id"] in TRIAGE]
    )
    if args.kind:
        tasks = [t for t in tasks if t["kind"] == args.kind]
    if args.task_id:
        tasks = [t for t in TASKS + COMPOUND_TASKS if t["id"] in args.task_id]
    if not tasks:
        raise ValueError("No benchmark tasks selected")
    if any(t.get("script_task") for t in tasks) and args.entrypoints != ["plans"]:
        raise ValueError("Compound dependency scenarios require --entrypoints plans")
    headers = {}
    if os.environ.get("RITESMITH_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["RITESMITH_API_TOKEN"]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.concurrency)
    blocked = asyncio.Event()
    rows = [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
    done = {(r["task"], r["entrypoint"], r["repetition"]) for r in rows}
    async with httpx.AsyncClient(base_url=args.url, timeout=600, headers=headers) as client:

        async def measure(task, entrypoint, repetition):
            if (task["id"], entrypoint, repetition) in done:
                return
            async with semaphore:
                if blocked.is_set():
                    return
                endpoint, body = payload(task, entrypoint, args.missing_client_tests)
                started = time.perf_counter()
                errors, calls, stages, response_body, status = [], [], [], {}, 0
                software_sha = None
                try:
                    response = await client.post(
                        endpoint,
                        json=body,
                        headers={
                            "x-benchmark-task": task["id"],
                            "x-benchmark-phase": args.phase,
                            "x-benchmark-model": args.model,
                            "x-benchmark-provider-cache": "cold" if repetition == 0 else "normal",
                        },
                    )
                    elapsed = time.perf_counter() - started
                    status = response.status_code
                    response_body = response.json()
                    software_sha = response.headers.get("x-benchmark-software")
                    calls = json.loads(response.headers.get("x-benchmark-calls", "[]"))
                    stages = json.loads(response.headers.get("x-benchmark-stages", "[]"))
                    errors = (
                        assess(task, response_body, entrypoint)
                        if status < 400
                        else [f"HTTP {status}"]
                    )
                    if response.headers.get("x-benchmark-accepted") == "false":
                        errors.append("generation acceptance gate failed")
                except Exception as exc:
                    elapsed = time.perf_counter() - started
                    errors = [type(exc).__name__ + ": " + str(exc)]
                row = {
                    "task": task["id"],
                    "kind": task["kind"],
                    "held_out": task.get("held_out", False),
                    "phase": args.phase,
                    "software_sha256": software_sha,
                    "assessment_version": ASSESSMENT_VERSION,
                    "target_seconds": getattr(args, "target_seconds", 5),
                    "model": args.model,
                    "repetition": repetition,
                    "provider_cache_mode": "cold" if repetition == 0 else "normal",
                    "client_tests": not args.missing_client_tests,
                    "entrypoint": entrypoint,
                    "concurrency": args.concurrency,
                    "duration_s": elapsed,
                    "http_status": status,
                    "valid": not errors,
                    "errors": errors,
                    "calls": calls,
                    "stages": stages,
                    "response": response_body,
                }
                rows.append(row)
                append_record(output, row)
                if any(
                    c.get("provider_code") in ("insufficient_quota", "credit_balance_exhausted")
                    for c in calls
                ):
                    blocked.set()
                print(
                    json.dumps(
                        {
                            k: row[k]
                            for k in (
                                "task",
                                "entrypoint",
                                "model",
                                "duration_s",
                                "valid",
                                "errors",
                            )
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(
            *[
                measure(task, entrypoint, repetition)
                for repetition in range(args.repetitions)
                for index, task in enumerate(tasks)
                for entrypoint in (
                    [args.entrypoints[(repetition + index) % len(args.entrypoints)]]
                    if args.rotate_entrypoints
                    else args.entrypoints
                )
            ]
        )
    summary = summarize(rows)
    suffix = ".partial.summary.json" if blocked.is_set() else ".summary.json"
    output.with_suffix(suffix).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    if blocked.is_set():
        raise BenchmarkBlocked("OpenAI account credits exhausted; incomplete evaluation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument(
        "--phase", choices=("baseline", "model", "filtered", "full", "mermaid"), required=True
    )
    parser.add_argument("--target-seconds", type=float, default=10)
    parser.add_argument("--model", choices=MODELS, default="gpt-5-mini")
    parser.add_argument("--tasks", choices=("triage", "all", "compounds"), default="triage")
    parser.add_argument("--task-id", action="append")
    parser.add_argument("--kind", choices=("luau_script", "trama_workflow"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--entrypoints",
        nargs="+",
        choices=("specialized", "generate", "plans"),
        default=["specialized"],
    )
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--rotate-entrypoints",
        action="store_true",
        help="Distribute repetitions across HTTP entrypoints; report each group separately.",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--missing-client-tests", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
