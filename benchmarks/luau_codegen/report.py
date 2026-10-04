"""Aggregates results.jsonl into report.md + summary.csv.

python -m benchmarks.luau_codegen.report benchmarks/luau_codegen/results/<run>
"""

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Approximate list prices, USD per 1M tokens (input, output). Edit when they change.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1-nano": (0.10, 0.40),
}

GO_MAX_GAP_PP = 5.0
GO_MIN_STRICT = 0.80


def _load(run_dir: Path) -> tuple[dict, list[dict]]:
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    samples = [json.loads(ln) for ln in (run_dir / "results.jsonl").read_text().splitlines() if ln]
    return meta, samples


def _checked(sample: dict) -> list[dict]:
    return [a for a in sample["attempts"] if "check" in a]


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.0f}%" if den else "—"


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _normalize(msg: str) -> str:
    msg = re.sub(r"^Test case \d+ \(input=.*?\) ", "Test case ", msg)
    msg = re.sub(r"\(full output: .*\)$", "", msg)
    msg = re.sub(r"line \d+", "line N", msg)
    msg = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", msg)
    msg = re.sub(r"\d+(\.\d+)?", "N", msg)
    return msg.strip()[:140]


def summarize(samples: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in samples:
        groups[(s["model"], s["target"], s["variant"])].append(s)

    rows = []
    for (model, target, variant), group in sorted(groups.items()):
        n = len(group)
        first = [(_checked(s) or [None])[0] for s in group]
        finals = [(_checked(s) or [None])[-1] for s in group]
        passed = [s for s in group if s["passed"]]
        passed_finals = [_checked(s)[-1] for s in passed]

        strict_known = [a for a in passed_finals if "strict" in a["check"]["typecheck"]]
        strict_ok = [a for a in strict_known if a["check"]["typecheck"]["strict"]["passed"]]
        luau_finals = [a for a in finals if a and "strict" in a["check"]["typecheck"]]
        strict_errs = [len(a["check"]["typecheck"]["strict"]["errors"]) for a in luau_finals]
        nonstrict_ok = [a for a in luau_finals if a["check"]["typecheck"]["nonstrict"]["passed"]]

        slips = sum(1 for s in group if any(a["check"]["dialect_slips"] for a in _checked(s)))
        over = sum(1 for a in finals if a and a["check"]["exceeds_prod_limits"])
        prompt_toks = sum(a.get("prompt_tokens", 0) for s in group for a in s["attempts"])
        compl_toks = sum(a.get("completion_tokens", 0) for s in group for a in s["attempts"])
        latency = sum(a.get("latency_s", 0) for s in group for a in s["attempts"])
        price_in, price_out = PRICES.get(model, (0.0, 0.0))

        rows.append(
            {
                "model": model,
                "target": target,
                "variant": variant,
                "n": n,
                "compile_at_1": _rate(sum(1 for a in first if a and a["check"]["compiled"]), n),
                "pass_at_1": _rate(sum(1 for a in first if a and a["check"]["passed"]), n),
                "pass_final": _rate(len(passed), n),
                "avg_attempts": sum(len(s["attempts"]) for s in group) / n,
                "strict_ok_of_passing": _rate(len(strict_ok), len(strict_known)),
                "nonstrict_ok": _rate(len(nonstrict_ok), len(luau_finals)),
                "avg_strict_errors": (sum(strict_errs) / len(strict_errs)) if strict_errs else None,
                "dialect_slip_rate": _rate(slips, n),
                "over_prod_limits": _rate(over, n),
                "fatal": sum(1 for s in group if s.get("fatal")),
                "prompt_tokens": prompt_toks,
                "completion_tokens": compl_toks,
                "cost_usd": (prompt_toks * price_in + compl_toks * price_out) / 1e6,
                "avg_latency_s": latency / n,
            }
        )
    return rows


def _fmt(value, kind: str = "pct") -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{100 * value:.0f}%"
    if kind == "float":
        return f"{value:.2f}"
    return str(value)


def _tier_table(samples: list[dict]) -> list[str]:
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for s in samples:
        cells[(s["model"], s["target"], s["variant"], s["tier"])].append(s)
    tiers = sorted({s["tier"] for s in samples})
    configs = sorted({k[:3] for k in cells})
    lines = [
        "| model | target | variant | " + " | ".join(f"{t} pass@1 / final" for t in tiers) + " |",
        "|---|---|---|" + "---|" * len(tiers),
    ]
    for cfg in configs:
        row = []
        for tier in tiers:
            group = cells.get((*cfg, tier), [])
            p1 = sum(1 for s in group if _checked(s) and _checked(s)[0]["check"]["passed"])
            pf = sum(1 for s in group if s["passed"])
            row.append(f"{_pct(p1, len(group))} / {_pct(pf, len(group))}")
        lines.append(f"| {cfg[0]} | {cfg[1]} | {cfg[2]} | " + " | ".join(row) + " |")
    return lines


def _task_failures(samples: list[dict]) -> list[str]:
    per_task: dict[str, Counter] = defaultdict(Counter)
    for s in samples:
        per_task[s["task_id"]]["n"] += 1
        per_task[s["task_id"]]["pass"] += int(s["passed"])
    lines = ["| task | pass final |", "|---|---|"]
    for task_id, c in sorted(per_task.items(), key=lambda kv: kv[1]["pass"] / kv[1]["n"]):
        lines.append(f"| {task_id} | {c['pass']}/{c['n']} |")
    return lines


def _top_errors(samples: list[dict], target: str, limit: int = 10) -> tuple[Counter, Counter]:
    runtime, types = Counter(), Counter()
    for s in samples:
        if s["target"] != target:
            continue
        for a in _checked(s):
            c = a["check"]
            msgs = [f"compile: {e}" for e in c["compile_errors"]]
            msgs += [f"forbidden: {e}" for e in c["forbidden"]]
            if c["exec_error"]:
                msgs.append(f"exec: {c['exec_error']}")
            msgs += [case["message"] for case in c["cases"] if not case["passed"]]
            runtime.update(_normalize(m) for m in msgs)
            strict = c["typecheck"].get("strict")
            if strict:
                types.update(_normalize(e) for e in strict["errors"])
    return Counter(dict(runtime.most_common(limit))), Counter(dict(types.most_common(limit)))


def _verdict(rows: list[dict], primary_model: str | None) -> list[str]:
    by_cfg = {(r["model"], r["target"], r["variant"]): r for r in rows}
    models = sorted({r["model"] for r in rows})
    model = primary_model if primary_model in models else (models[0] if models else None)
    if model is None:
        return ["No samples."]
    lua = by_cfg.get((model, "lua", "untyped"))
    luau = [r for r in rows if r["model"] == model and r["target"] == "luau"]
    if not lua or not luau:
        return [f"Need both lua and luau results for `{model}` to evaluate the gate."]

    best = max(luau, key=lambda r: r["pass_final"] or 0)
    gap_pp = 100 * ((lua["pass_final"] or 0) - (best["pass_final"] or 0))
    strict = best["strict_ok_of_passing"]
    ok_gap = gap_pp <= GO_MAX_GAP_PP
    ok_strict = strict is not None and strict >= GO_MIN_STRICT
    lines = [
        f"Primary model: `{model}`",
        (
            f"- Luau best variant `{best['variant']}` pass final {_fmt(best['pass_final'])} vs "
            f"Lua {_fmt(lua['pass_final'])} → gap {gap_pp:+.1f}pp "
            f"(limit {GO_MAX_GAP_PP:.0f}pp): **{'OK' if ok_gap else 'FAIL'}**"
        ),
        (
            f"- Strict typecheck among passing scripts: {_fmt(strict)} "
            f"(min {GO_MIN_STRICT:.0%}): **{'OK' if ok_strict else 'FAIL'}**"
        ),
    ]
    typed = by_cfg.get((model, "luau", "typed"))
    untyped = by_cfg.get((model, "luau", "untyped"))
    if typed and untyped:
        diff = 100 * ((typed["pass_final"] or 0) - (untyped["pass_final"] or 0))
        lines.append(
            f"- typed vs untyped pass final: {diff:+.1f}pp; strict ok "
            f"{_fmt(typed['strict_ok_of_passing'])} vs {_fmt(untyped['strict_ok_of_passing'])}"
        )
    lines.append("")
    lines.append(
        f"**Verdict: {'GO' if ok_gap and ok_strict else 'NO-GO'}** (gate as proposed in the plan)"
    )
    return lines


def write_report(run_dir: Path) -> Path:
    meta, samples = _load(run_dir)
    rows = summarize(samples)

    with (run_dir / "summary.csv").open("w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    md = [
        "# Luau code-generation benchmark",
        "",
        f"- Started: {meta.get('started_at', '?')}",
        f"- Luau toolchain: {meta.get('luau_version', '?')}; Lua baseline: lupa (production engine)",
        (
            f"- Tasks: {len(meta.get('tasks', []))} × repeats {meta.get('repeats', '?')}; "
            f"max attempts {meta.get('max_attempts', '?')}; "
            f"typecheck gate `{meta.get('typecheck_gate', '?')}`"
        ),
        f"- Samples: {len(samples)}",
        "",
        "## Go / no-go",
        "",
        *_verdict(rows, (meta.get("models") or [None])[0]),
        "",
        "## Summary",
        "",
        (
            "| model | target | variant | n | compile@1 | pass@1 | pass final | avg attempts "
            "| strict ok (passing) | nonstrict ok | avg strict errs | dialect slips "
            "| > prod limits | cost USD | avg latency s |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['model']} | {r['target']} | {r['variant']} | {r['n']} "
            f"| {_fmt(r['compile_at_1'])} | {_fmt(r['pass_at_1'])} | {_fmt(r['pass_final'])} "
            f"| {r['avg_attempts']:.2f} | {_fmt(r['strict_ok_of_passing'])} "
            f"| {_fmt(r['nonstrict_ok'])} | {_fmt(r['avg_strict_errors'], 'float')} "
            f"| {_fmt(r['dialect_slip_rate'])} | {_fmt(r['over_prod_limits'])} "
            f"| {r['cost_usd']:.3f} | {r['avg_latency_s']:.1f} |"
        )
    md += ["", "## By tier", "", *_tier_table(samples)]
    md += ["", "## Hardest tasks (all configs)", "", *_task_failures(samples)]
    for target in ("luau", "lua"):
        runtime, types = _top_errors(samples, target)
        if runtime:
            md += ["", f"## Top validation errors — {target}", ""]
            md += [f"- {count}× `{msg}`" for msg, count in runtime.most_common()]
        if types:
            md += ["", "## Top strict type errors — luau", ""]
            md += [f"- {count}× `{msg}`" for msg, count in types.most_common()]
    fatal = [s for s in samples if s.get("fatal")]
    if fatal:
        md += ["", "## Fatal errors", ""]
        md += [
            f"- {s['model']} {s['target']}/{s['variant']} {s['task_id']}: {s['fatal']}"
            for s in fatal
        ]

    path = run_dir / "report.md"
    path.write_text("\n".join(md) + "\n")
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(write_report(Path(sys.argv[1])))
