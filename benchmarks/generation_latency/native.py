"""Use the production LunarDyson binding with pure benchmark tool substitutes.

Each tool substitute is the existing task's Lua stub, evaluated in a separate
LunarDyson runtime. Generated code can only call these substitutes. No provider
functions are registered and no workflow is executed.
"""

from collections import Counter
from dataclasses import asdict

from lunardyson import Runtime

from benchmarks.luau_codegen.checks import CaseResult, SampleCheck, compare, forbidden_tokens
from benchmarks.luau_codegen.preamble import DEFAULT_CONTEXT, task_types


def validate(task, script, *_args, test_cases=None):
    check = SampleCheck(
        target="luau",
        lines=script.count("\n") + 1,
        bytes=len(script.encode()),
        exceeds_prod_limits=False,
    )
    check.forbidden = forbidden_tokens(script, "luau")
    counts = Counter()
    stubs = []
    rt = Runtime(memory_mb=32, cpu_time_ms=1000)
    try:
        rt.declare_types(task_types(task))
        for tool in task.get("tools", []):
            stub_rt = Runtime(memory_mb=16, cpu_time_ms=1000)
            stubs.append(stub_rt)
            source = (
                "local stub = "
                + tool["stub"]
                + "\nfunction run(input, context) return stub(input) end"
            )

            def handler(args, stub_rt=stub_rt, source=source, name=tool["name"]):
                counts[name] += 1
                result = stub_rt.execute(source, args, {})
                if not result.ok:
                    raise RuntimeError(result.message)
                return result.output

            rt.tool(
                tool["name"],
                handler,
                signature=tool["signature"],
                effect="pure",
                max_calls=tool.get("max_calls"),
            )
        for mode in ("nonstrict", "strict"):
            diagnostics = rt.check(script, strict=mode == "strict")
            errors = [str(d) for d in diagnostics if d["severity"] == "error"]
            check.typecheck[mode] = {"passed": not errors, "errors": errors, "lints": []}
            if mode == "strict":
                check.gated_type_errors = errors
        cases = task["test_cases"] + [
            {"input": c.get("input", {}), "expected": c.get("expected_output"), "exact": True}
            for c in test_cases or []
        ]
        for i, case in enumerate(cases):
            counts.clear()
            result = rt.execute(script, case["input"], case.get("context", DEFAULT_CONTEXT))
            error = None
            if not result.ok:
                error = result.error_kind + ": " + result.message
                if result.error_kind == "syntax":
                    check.compile_errors.append(error)
            else:
                if case.get("exact"):
                    if case["expected"] is not None and result.output != case["expected"]:
                        error = f"expected {case['expected']}, got {result.output}"
                else:
                    error = compare(case["expected"], result.output)
                if not error:
                    for name, expected in case.get("expected_calls", {}).items():
                        if counts[name] != expected:
                            error = f"tools.{name}: expected {expected} calls, got {counts[name]}"
                            break
            check.cases.append(
                asdict(CaseResult(i, error is None, f"case {i}: {error}" if error else None))
            )
    finally:
        rt.close()
        for stub in stubs:
            stub.close()
    return check


if __name__ == "__main__":
    from benchmarks.luau_codegen.tasks import ALL_TASKS

    failures = []
    for task in ALL_TASKS:
        check = validate(task, task["reference"])
        print(task["id"], "PASS" if check.passed else check.repair_errors())
        if not check.passed:
            failures.append(task["id"])
    raise SystemExit(bool(failures))
