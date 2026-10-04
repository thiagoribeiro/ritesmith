# Luau code-generation benchmark

Go/no-go gate for replacing the Lua 5.x sandbox (lupa) with a Luau runtime
(LunarDyson). The benchmark asks one question: **can the LLM generate Luau that
compiles, type-checks and passes tests, at a rate comparable to Lua?**

It is standalone, meaning it is not part of the pytest suite, because it calls
the real OpenAI API.

## Setup

```bash
./benchmarks/luau_codegen/fetch_luau.sh              # pinned Luau release → .bin/ (LUAU_VERSION=0.740)
python -m benchmarks.luau_codegen.selftest           # offline: references + negative cases, no LLM
export OPENAI_API_KEY=...                            # or put it in .env
```

## Run

```bash
python -m benchmarks.luau_codegen.run --repeats 1 --tasks T1           # smoke (~40 samples)
python -m benchmarks.luau_codegen.run --repeats 3                      # full: 2 models × 3 configs × 25 tasks × 3
python -m benchmarks.luau_codegen.run --typecheck-gate strict          # feed strict type errors back into repair
python -m benchmarks.luau_codegen.report benchmarks/luau_codegen/results/<run>   # re-render a report
```

Defaults are `--models` = `llm_model,llm_model_fast` from `ritesmith/config.py`,
`--max-attempts` = `generation_max_attempts`, and the production generation and
repair temperatures. Output goes to `results/<timestamp>/`: `meta.json`,
`results.jsonl` (one line per sample, with every attempt's script and check
result), `summary.csv` and `report.md`.

## What is compared

| config | language | prompt |
|---|---|---|
| `lua/untyped` | Lua, run by lupa (the production engine) | production prompt rules, `tools.*` ABI |
| `luau/untyped` | Luau | same text, plus a short "Luau, not Lua 5.x" language section |
| `luau/typed` | Luau | also predeclared Luau types (`Input`, `Output`, tool signatures) and a required `run(input: Input, context: Context): Output` annotation |

All three use the `tools.ns.fn({...})` ABI proposed for LunarDyson. Tools return
tagged unions: `{ok = true, ...}` or `{ok = false, error, message}`. Because the
ABI is the same in every config, the Lua vs Luau gap measures the language, not
the ABI change.

## Pipeline per attempt ([checks.py](checks.py))

1. **compile**: `luau-compile --null` for Luau, `load()` in lupa for Lua
2. **forbidden**: the production deny-list (`ritesmith/core/validation.py`), plus `getfenv`, `setfenv` and `loadstring` for Luau
3. **dialect slips** (diagnostic only): Lua-isms in Luau (`goto`, `<const>`, `math.tointeger`…) and Luau-isms in Lua (`+=`, `continue`, type annotations…)
4. **typecheck** (Luau only): `luau-analyze` in both `nonstrict` and `strict` mode, on a typed preamble followed by the script. The CLI has no definitions-file flag, so the preamble declares the types and `local tools: {...} = (nil :: any)`. Lint warnings are recorded separately from `TypeError`/`SyntaxError`.
5. **execute**: stubs, script and test harness go into one file, run by `luau` or by `python lua_exec.py` (lupa) in a subprocess with a 5 s timeout. Tool wrappers count calls and enforce `max_calls`. Forbidden globals are shadowed by upvalues, because the Luau CLI's builtin environment is read-only and `os = nil` does nothing.
6. **repair**: the failures from stages 1, 2, 5 (and 4 with `--typecheck-gate`) go back to the model, in the same format as the production `lua_repair_user`, until the attempt budget runs out.

Outputs are compared by the keys in `expected` (extra keys are allowed; `None` means "must be absent"), with a relative tolerance of 1e-6 for numbers. `{}` and `[]` count as equal because an empty Lua table is ambiguous.

## Tasks ([tasks/](tasks/))

31 tasks, each with a hand-written Luau `reference` that `selftest.py` proves to be
strict-clean and passing:

- **T1 (10): pure transforms.** Temperature, percent change, slugify, grouping, dates, word counts, durations.
- **T2 (10): one tool with filtering, formatting and error handling.** Price label, CRM, weather, issues, FX (the tool must not be called when `from == to`), free calendar slots.
- **T3 (5): multi-tool orchestration, the LunarDyson thesis.** Conditional side effects, call budgets, a "notify at most once" rule, order fulfillment.
- **T4 (6): held-out.** Fresh scenarios (roman numerals, median, run-length encoding, bracket balance, Caesar cipher, inventory reorder) **never used to tune the prompts** — run `--tasks T4` to measure generalisation rather than prompt overfitting. Pass rates here are the honest read on whether the model writes valid Luau for problems it (and the prompt) have not seen.

To add a task, append a dict to the tier module with `goal`, schemas, `types`,
`tools` (with `signature`, prose `description` and a `stub` written in the common
Lua/Luau subset), `test_cases` and `reference`. Then run the selftest.

## Metrics

- **compile@1 / pass@1**: first attempt compiles / passes every test case
- **pass final**: passes within `--max-attempts`
- **strict ok (passing)**: share of passing scripts whose final version is clean under `--!strict`
- **avg strict errs**, **nonstrict ok**, **dialect slips**, **> prod limits** (the 60-line / 4 KB production cap, recorded but not enforced), tokens, estimated cost (`PRICES` in `report.py`) and latency

## Go / no-go gate (printed in report.md)

- Best Luau variant's pass final is at most **5 pp below** Lua, measured on the primary model (the first `--models` entry)
- Strict typecheck is clean on at least **80 %** of the passing Luau scripts
- typed vs untyped is reported too: if typed wins, the production prompt should carry Luau signatures
