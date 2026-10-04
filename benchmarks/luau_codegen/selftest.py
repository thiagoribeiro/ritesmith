"""Offline sanity check of the benchmark pipeline — no LLM calls.

    python -m benchmarks.luau_codegen.selftest

1. Every task's reference solution must compile, typecheck in strict mode and
   pass its test cases (proves tasks, stubs and expectations are consistent).
2. Known-bad scripts must be classified by the right stage.
3. The Lua baseline path (lupa) must accept valid Lua and reject Luau syntax.
4. report.py must render a report from synthetic samples.
"""

import json
import sys
import tempfile
from pathlib import Path

from benchmarks.luau_codegen import report
from benchmarks.luau_codegen.checks import validate
from benchmarks.luau_codegen.tasks import ALL_TASKS, TASKS_BY_ID

_failures: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        _failures.append(name)
        if detail:
            print(f"      {detail}")


def check_references() -> list[dict]:
    samples = []
    for task in ALL_TASKS:
        check = validate(task, task["reference"].strip(), "luau")
        strict = check.typecheck.get("strict", {})
        ok = check.passed and strict.get("passed", False)
        expect(
            f"reference {task['id']}",
            ok,
            "; ".join(check.repair_errors() + strict.get("errors", [])),
        )
        samples.append(
            {
                "model": "reference",
                "target": "luau",
                "variant": "typed",
                "task_id": task["id"],
                "tier": task["tier"],
                "repeat": 1,
                "passed": check.passed,
                "attempts": [{"attempt": 1, "script": task["reference"], "check": check.to_dict()}],
            }
        )
    return samples


def check_negative_cases() -> None:
    celsius = TASKS_BY_ID["t1_celsius_to_fahrenheit"]
    price = TASKS_BY_ID["t2_price_label"]
    budget = TASKS_BY_ID["t3_budgeted_sync"]

    c = validate(celsius, "function run(input, context)\n  return {\nend", "luau")
    expect("luau syntax error → compile stage", not c.compiled and not c.cases)

    c = validate(
        celsius,
        "function run(input, context)\n  for i = 1, 3 do\n    goto done\n  end\n  ::done::\n"
        "  return { fahrenheit = 0 }\nend",
        "luau",
    )
    expect("goto → compile error + dialect slip", not c.compiled and "goto" in c.dialect_slips)

    c = validate(
        celsius,
        "function run(input, context)\n  return { fahrenheit = input.celsius }\nend",
        "luau",
    )
    expect("wrong output → test failure", c.compiled and not c.tests_passed, str(c.repair_errors()))

    c = validate(celsius, "function run(input, context)\n  while true do end\nend", "luau")
    expect("infinite loop → timeout", bool(c.exec_error) and "timed out" in c.exec_error)

    c = validate(
        celsius,
        "function run(input, context)\n  local env = getfenv()\n  return { fahrenheit = 32 }\nend",
        "luau",
    )
    expect("getfenv → forbidden", any("getfenv" in f for f in c.forbidden))

    c = validate(
        celsius,
        "function run(input, context)\n  return { fahrenheit = os.time() }\nend",
        "luau",
    )
    expect(
        "os hidden at runtime",
        not c.tests_passed and any("os" in e or "nil" in e for e in c.repair_errors()),
        str(c.repair_errors()),
    )

    c = validate(
        celsius, "local function run(input, context)\n  return { fahrenheit = 32 }\nend", "luau"
    )
    expect("local run → contract error", bool(c.exec_error) and "must define" in c.exec_error)

    typo = price["reference"].replace("r.price * 100", "r.prcie * 100")
    c = validate(price, typo.strip(), "luau", typecheck_gate="strict")
    strict = c.typecheck.get("strict", {})
    expect(
        "field typo → strict type error (gated)",
        not strict.get("passed", True) and bool(c.gated_type_errors) and not c.passed,
        str(strict),
    )

    unchecked = """
function run(input: Input, context: Context): Output
    local r = tools.market.get_price({ symbol = input.symbol, currency = input.currency })
    return { symbol = r.symbol, currency = r.currency, price = r.price, label = "" }
end"""
    c = validate(price, unchecked.strip(), "luau")
    expect(
        "missing ok-check → strict error on tagged union",
        not c.typecheck["strict"]["passed"],
        str(c.typecheck["strict"]),
    )

    greedy = """
function run(input, context)
    local processed, total = {}, 0
    for _, id in ipairs(input.ids) do
        local r = tools.crm.get({ id = id })
        if r.ok then
            table.insert(processed, id)
            total = total + r.customer.balance
        end
    end
    return { processed = processed, not_found = {}, skipped = {}, total_balance = total }
end"""
    c = validate(budget, greedy.strip(), "luau")
    expect(
        "tool budget exceeded → case error",
        any("budget exceeded" in e for e in c.repair_errors()),
        str(c.repair_errors()),
    )


def check_lua_baseline() -> list[dict]:
    lua_solutions = {
        "t1_celsius_to_fahrenheit": """
function run(input, context)
  return { fahrenheit = input.celsius * 9 / 5 + 32 }
end""",
        "t2_price_label": """
function run(input, context)
  local r = tools.market.get_price({ symbol = input.symbol, currency = input.currency })
  if not r.ok then
    return { error = r.error, message = r.message }
  end
  return {
    symbol = r.symbol, currency = r.currency,
    price = math.floor(r.price * 100 + 0.5) / 100,
    label = string.format("%s: %.2f %s", r.symbol, r.price, r.currency),
  }
end""",
    }
    samples = []
    for task_id, code in lua_solutions.items():
        task = TASKS_BY_ID[task_id]
        c = validate(task, code.strip(), "lua")
        expect(f"lua baseline {task_id}", c.passed, str(c.repair_errors()))
        samples.append(
            {
                "model": "reference",
                "target": "lua",
                "variant": "untyped",
                "task_id": task_id,
                "tier": task["tier"],
                "repeat": 1,
                "passed": c.passed,
                "attempts": [{"attempt": 1, "script": code, "check": c.to_dict()}],
            }
        )

    celsius = TASKS_BY_ID["t1_celsius_to_fahrenheit"]
    luau_syntax = "function run(input, context)\n  local f = input.celsius\n  f *= 1.8\n  f += 32\n  return { fahrenheit = f }\nend"
    c = validate(celsius, luau_syntax, "lua")
    expect(
        "luau syntax in lua target → compile error + slip",
        not c.compiled and "compound assignment" in c.dialect_slips,
        str(c.compile_errors),
    )

    c = validate(
        celsius, "function run(input, context)\n  return { fahrenheit = os.time() }\nend", "lua"
    )
    expect("lua: os hidden at runtime", not c.tests_passed)
    return samples


def check_report(samples: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / "results.jsonl").write_text("\n".join(json.dumps(s) for s in samples))
        path = report.write_report(run_dir)
        text = path.read_text()
        expect("report renders", "## Summary" in text and "reference" in text)


def main() -> int:
    samples = check_references()
    check_negative_cases()
    samples += check_lua_baseline()
    check_report(samples)
    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed: {', '.join(_failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
