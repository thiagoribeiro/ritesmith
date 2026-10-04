"""Luau vs Lua code-generation benchmark — generate, validate, repair, record.

    python -m benchmarks.luau_codegen.run --repeats 1 --tasks T1          # smoke
    python -m benchmarks.luau_codegen.run --repeats 3                     # full run

Requires OPENAI_API_KEY and the Luau toolchain (fetch_luau.sh). Every sample is
appended to results/<timestamp>/results.jsonl; report.md is written at the end.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError

from benchmarks.luau_codegen import prompts, report
from benchmarks.luau_codegen.checks import luau_version, validate
from benchmarks.luau_codegen.tasks import ALL_TASKS, TASKS_BY_ID
from ritesmith.config import get_settings
from ritesmith.llm.openai_provider import _MAX_TOKENS, _TEMPERATURE

HERE = Path(__file__).parent
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
_LLM_RETRIES = 4
# Mirrors the production OpenAIProvider: gpt-5*/o* reasoning models reject
# `temperature` on Chat Completions and want `max_completion_tokens`.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _strip_fences(code: str) -> str:
    m = _FENCE.match(code)
    return m.group(1) if m else code


class LLM:
    def __init__(self, timeout: float):
        self.client = AsyncOpenAI(timeout=timeout)

    async def complete(
        self, model: str, system: str, user: str, kind: str
    ) -> tuple[dict | None, dict]:
        """Returns (parsed JSON or None, usage stats)."""
        start = time.perf_counter()
        for attempt in range(_LLM_RETRIES):
            try:
                kwargs: dict = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                }
                if model.startswith(_REASONING_PREFIXES):
                    kwargs["max_completion_tokens"] = _MAX_TOKENS[kind]
                else:
                    kwargs["temperature"] = _TEMPERATURE[kind]
                    kwargs["max_tokens"] = _MAX_TOKENS[kind]
                resp = await self.client.chat.completions.create(**kwargs)
                break
            except (RateLimitError, APITimeoutError, APIConnectionError):
                if attempt == _LLM_RETRIES - 1:
                    raise
                await asyncio.sleep(2**attempt)
        usage = resp.usage
        stats = {
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "latency_s": round(time.perf_counter() - start, 3),
        }
        try:
            data = json.loads(resp.choices[0].message.content or "")
        except json.JSONDecodeError:
            data = None
        return data if isinstance(data, dict) else None, stats


async def run_sample(
    llm: LLM, model: str, target: str, variant: str, task: dict, repeat: int, args
) -> dict:
    attempts = []
    script: str | None = None
    errors: list[str] = []
    for n in range(1, args.max_attempts + 1):
        if script is None:
            data, stats = await llm.complete(
                model,
                prompts.generation_system(target),
                prompts.generation_user(task, target, variant),
                "lua_gen",
            )
            code = data.get("script") if data else None
        else:
            data, stats = await llm.complete(
                model,
                prompts.repair_system(target),
                prompts.repair_user(task, script, errors, n - 1, target, variant),
                "lua_repair",
            )
            code = data.get("repaired_content") if data else None

        record = {"attempt": n, "kind": "generate" if script is None else "repair", **stats}
        if not isinstance(code, str) or not code.strip():
            record["response_error"] = "response was not JSON with a non-empty script field"
            attempts.append(record)
            continue  # keeps the previous script (if any) as the repair base

        script = _strip_fences(code).strip()
        check = await asyncio.to_thread(validate, task, script, target, args.typecheck_gate)
        errors = check.repair_errors()
        record.update(script=script, check=check.to_dict())
        attempts.append(record)
        if check.passed:
            break

    final = next((a for a in reversed(attempts) if "check" in a), None)
    return {
        "model": model,
        "target": target,
        "variant": variant,
        "task_id": task["id"],
        "tier": task["tier"],
        "repeat": repeat,
        "typecheck_gate": args.typecheck_gate,
        "passed": bool(final and final["check"]["passed"]),
        "attempts": attempts,
    }


def _select_tasks(spec: str) -> list[dict]:
    if spec == "all":
        return ALL_TASKS
    selected: list[dict] = []
    for token in spec.split(","):
        token = token.strip()
        if token in ("T1", "T2", "T3", "T4"):
            selected += [t for t in ALL_TASKS if t["tier"] == token]
        elif token in TASKS_BY_ID:
            selected.append(TASKS_BY_ID[token])
        else:
            raise SystemExit(f"unknown task or tier: {token}")
    return selected


def _configs(targets: list[str], variants: list[str]) -> list[tuple[str, str]]:
    configs = []
    for target in targets:
        if target == "lua":
            configs.append(("lua", "untyped"))  # no type system to exercise
        else:
            configs += [("luau", v) for v in variants]
    return configs


async def main() -> int:
    load_dotenv()
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--models", default=f"{settings.llm_model},{settings.llm_model_fast}")
    parser.add_argument("--targets", default="luau,lua")
    parser.add_argument("--variants", default="typed,untyped")
    parser.add_argument(
        "--tasks", default="all", help="all | T1,T2,T3,T4 (T4=held-out) | task ids, comma-separated"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=settings.generation_max_attempts)
    parser.add_argument(
        "--typecheck-gate",
        choices=["none", "nonstrict", "strict"],
        default="none",
        help="feed luau-analyze errors back as repair errors (Luau only)",
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set (env or .env)", file=sys.stderr)
        return 1

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    configs = _configs([t.strip() for t in args.targets.split(",")], args.variants.split(","))
    tasks = _select_tasks(args.tasks)
    out_dir = args.out or HERE / "results" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "started_at": datetime.now(UTC).isoformat(),
        "luau_version": luau_version(),
        "models": models,
        "configs": configs,
        "tasks": [t["id"] for t in tasks],
        "repeats": args.repeats,
        "max_attempts": args.max_attempts,
        "typecheck_gate": args.typecheck_gate,
        "temperature": {"generate": _TEMPERATURE["lua_gen"], "repair": _TEMPERATURE["lua_repair"]},
        "max_tokens": {"generate": _MAX_TOKENS["lua_gen"], "repair": _MAX_TOKENS["lua_repair"]},
        "reasoning_models": [m for m in models if m.startswith(_REASONING_PREFIXES)],
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    jobs = [
        (model, target, variant, task, rep)
        for model in models
        for target, variant in configs
        for task in tasks
        for rep in range(1, args.repeats + 1)
    ]
    print(f"{len(jobs)} samples → {out_dir}")

    llm = LLM(timeout=settings.llm_timeout_seconds)
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    done = 0
    results_path = out_dir / "results.jsonl"

    async def worker(job):
        nonlocal done
        model, target, variant, task, rep = job
        async with sem:
            try:
                sample = await run_sample(llm, model, target, variant, task, rep, args)
            except Exception as e:  # keep the run going; the failure is recorded
                sample = {
                    "model": model,
                    "target": target,
                    "variant": variant,
                    "task_id": task["id"],
                    "tier": task["tier"],
                    "repeat": rep,
                    "passed": False,
                    "attempts": [],
                    "fatal": f"{type(e).__name__}: {e}",
                }
        async with lock:
            with results_path.open("a") as f:
                f.write(json.dumps(sample) + "\n")
            done += 1
            status = "PASS" if sample["passed"] else "fail"
            print(
                f"[{done}/{len(jobs)}] {status} {model} {target}/{variant} {task['id']} "
                f"#{rep} ({len(sample['attempts'])} attempt(s))"
            )

    await asyncio.gather(*(worker(j) for j in jobs))
    report_path = report.write_report(out_dir)
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
