"""Validation stages for one generated script: compile → forbidden → typecheck → execute."""

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from benchmarks.luau_codegen.preamble import RESULT_SENTINEL, analyze_source, exec_source
from ritesmith.core.validation import _FORBIDDEN_PATTERNS

HERE = Path(__file__).parent
BIN = HERE / ".bin"
EXEC_TIMEOUT_S = 5.0
TOOL_TIMEOUT_S = 20.0
PROD_MAX_LINES = 60
PROD_MAX_BYTES = 4 * 1024

_LUAU_FORBIDDEN = [
    (re.compile(r"\bgetfenv\s*\("), "função 'getfenv' é proibida"),
    (re.compile(r"\bsetfenv\s*\("), "função 'setfenv' é proibida"),
    (re.compile(r"\bloadstring\s*\("), "função 'loadstring' é proibida"),
]

# Constructs valid in one dialect but not the other — diagnostic only, never a gate.
_LUA_ISMS = [  # found in a Luau script
    (re.compile(r"\bgoto\b"), "goto"),
    (re.compile(r"::\w+::"), "label"),
    (re.compile(r"<\s*(const|close)\s*>"), "variable attribute"),
    (re.compile(r"\bmath\.(tointeger|type|ult)\b"), "math 5.3+ api"),
    (re.compile(r"\butf8\.charpattern\b"), "utf8.charpattern"),
]
_LUAU_ISMS = [  # found in a Lua script
    (re.compile(r"[\w\]\)]\s*(\+|-|\*|/|\.\.)=\s*"), "compound assignment"),
    (re.compile(r"^\s*continue\s*$", re.MULTILINE), "continue"),
    (re.compile(r"\blocal\s+\w+\s*:\s*[A-Za-z{]"), "type annotation"),
    (re.compile(r"^\s*(export\s+)?type\s+\w+\s*=", re.MULTILINE), "type alias"),
    (re.compile(r"\b(string\.split|table\.find|table\.clone)\b"), "luau stdlib"),
]

_ANALYZE_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)(?:-\d+)?: \((?P<code>\w+)\) (?P<kind>\w+): (?P<msg>.*)$"
)
_ERROR_KINDS = {"TypeError", "SyntaxError"}
_IGNORED_LINTS = {"LocalUnused", "FunctionUnused", "ImportUnused", "LocalShadow"}
_SCRIPT_LOC = re.compile(r"(?:\./)?\bscript(?:\.luau)?:(\d+):")


@dataclass
class TypecheckResult:
    mode: str
    errors: list[str] = field(default_factory=list)
    lints: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


@dataclass
class CaseResult:
    index: int
    passed: bool
    message: str | None = None


@dataclass
class SampleCheck:
    target: str
    lines: int
    bytes: int
    exceeds_prod_limits: bool
    compile_errors: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    dialect_slips: list[str] = field(default_factory=list)
    typecheck: dict[str, dict] = field(default_factory=dict)
    exec_error: str | None = None
    cases: list[dict] = field(default_factory=list)
    gated_type_errors: list[str] = field(default_factory=list)

    @property
    def compiled(self) -> bool:
        return not self.compile_errors

    @property
    def tests_passed(self) -> bool:
        return bool(self.cases) and all(c["passed"] for c in self.cases)

    @property
    def passed(self) -> bool:
        return (
            self.compiled
            and not self.forbidden
            and not self.gated_type_errors
            and self.tests_passed
        )

    def repair_errors(self) -> list[str]:
        errors = [f"Syntax error: {e}" for e in self.compile_errors]
        errors += [f"Forbidden: {e}" for e in self.forbidden]
        errors += [f"Type error: {e}" for e in self.gated_type_errors]
        if self.exec_error:
            errors.append(f"Execution error: {self.exec_error}")
        errors += [c["message"] for c in self.cases if not c["passed"] and c["message"]]
        return errors

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(compiled=self.compiled, tests_passed=self.tests_passed, passed=self.passed)
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def luau_version() -> str:
    version_file = BIN / "VERSION"
    return version_file.read_text().strip() if version_file.exists() else "unknown"


def _require_bin(name: str) -> str:
    path = BIN / name
    if not path.exists():
        raise RuntimeError(f"{path} not found — run benchmarks/luau_codegen/fetch_luau.sh")
    return str(path)


def _remap_lines(text: str, offset: int) -> str:
    def repl(m: re.Match) -> str:
        line = int(m.group(1)) - offset
        return f"line {line}:" if line > 0 else "harness:"

    return _SCRIPT_LOC.sub(repl, text)


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None


def _short(value, limit: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def compile_errors(script: str, target: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        if target == "luau":
            (cwd / "script.luau").write_text(script, encoding="utf-8")
            proc = _run(
                [_require_bin("luau-compile"), "--null", "script.luau"], cwd, TOOL_TIMEOUT_S
            )
            if proc is None:
                return ["compiler timed out"]
            out = (proc.stdout + proc.stderr).splitlines()
            errs = [ln for ln in out if "Error" in ln and not ln.startswith("Compiled")]
            if proc.returncode != 0 and not errs:
                errs = [ln for ln in out if ln.strip()] or ["compile failed"]
            return [re.sub(r"^script\.luau", "", e).strip() for e in errs]

        (cwd / "script.lua").write_text(script, encoding="utf-8")
        proc = _run(
            [sys.executable, str(HERE / "lua_exec.py"), "check", "script.lua"], cwd, TOOL_TIMEOUT_S
        )
        if proc is None:
            return ["compiler timed out"]
        return [proc.stderr.strip() or "load failed"] if proc.returncode != 0 else []


def forbidden_tokens(script: str, target: str) -> list[str]:
    patterns = _FORBIDDEN_PATTERNS + (_LUAU_FORBIDDEN if target == "luau" else [])
    return [msg for pattern, msg in patterns if pattern.search(script)]


def dialect_slips(script: str, target: str) -> list[str]:
    patterns = _LUA_ISMS if target == "luau" else _LUAU_ISMS
    return [label for pattern, label in patterns if pattern.search(script)]


def typecheck(task: dict, script: str, mode: str) -> TypecheckResult:
    source, offset = analyze_source(task, script, mode)
    result = TypecheckResult(mode=mode)
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "script.luau").write_text(source, encoding="utf-8")
        proc = _run(
            [_require_bin("luau-analyze"), "--formatter=plain", "script.luau"], cwd, TOOL_TIMEOUT_S
        )
    if proc is None:
        result.errors.append("analyzer timed out")
        return result

    entries: list[tuple[str, str]] = []  # (kind, message)
    for raw in (proc.stdout + proc.stderr).splitlines():
        m = _ANALYZE_LINE.match(raw)
        if m:
            line = int(m.group("line")) - offset
            where = f"line {line}" if line > 0 else "preamble"
            entries.append((m.group("kind"), f"{where}: {m.group('msg')}"))
        elif entries and raw.strip():
            kind, msg = entries[-1]
            entries[-1] = (kind, f"{msg} {raw.strip()}")

    for kind, msg in entries:
        if kind in _ERROR_KINDS:
            result.errors.append(msg)
        elif kind not in _IGNORED_LINTS:
            result.lints.append(f"{kind}: {msg}")
    return result


def compare(expected, actual, path: str = "$") -> str | None:
    """Returns a mismatch description, or None when `actual` satisfies `expected`.

    Objects are checked by the keys in `expected` (extra keys allowed); an
    expected value of None means the key must be absent. Empty Lua tables
    encode ambiguously, so {} and [] are treated as equal.
    """
    if isinstance(expected, dict):
        if actual == []:
            actual = {}
        if not isinstance(actual, dict):
            return f"{path}: expected object, got {_short(actual)}"
        for key, value in expected.items():
            if value is None:
                if actual.get(key) is not None:
                    return f"{path}.{key}: expected absent/nil, got {_short(actual[key])}"
                continue
            if key not in actual:
                return f"{path}.{key}: missing"
            mismatch = compare(value, actual[key], f"{path}.{key}")
            if mismatch:
                return mismatch
        return None
    if isinstance(expected, list):
        if actual == {}:
            actual = []
        if not isinstance(actual, list):
            return f"{path}: expected array, got {_short(actual)}"
        if len(actual) != len(expected):
            return f"{path}: expected {len(expected)} items, got {len(actual)}: {_short(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            mismatch = compare(e, a, f"{path}[{i}]")
            if mismatch:
                return mismatch
        return None
    if isinstance(expected, bool):
        return None if actual is expected else f"{path}: expected {expected}, got {_short(actual)}"
    if isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return f"{path}: expected number {expected}, got {_short(actual)}"
        if abs(actual - expected) > 1e-6 * max(1.0, abs(expected)):
            return f"{path}: expected {expected}, got {actual}"
        return None
    return (
        None if actual == expected else f"{path}: expected {_short(expected)}, got {_short(actual)}"
    )


def execute(task: dict, script: str, target: str) -> tuple[list[CaseResult], str | None]:
    cases = task["test_cases"]
    source, offset = exec_source(task, script, target, cases)
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        if target == "luau":
            (cwd / "script.luau").write_text(source, encoding="utf-8")
            cmd = [_require_bin("luau"), "script.luau"]
        else:
            (cwd / "script.lua").write_text(source, encoding="utf-8")
            cmd = [sys.executable, str(HERE / "lua_exec.py"), "run", "script.lua"]
        proc = _run(cmd, cwd, EXEC_TIMEOUT_S)

    if proc is None:
        return [], f"timed out after {EXEC_TIMEOUT_S:.0f}s (infinite loop?)"

    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_SENTINEL):
            payload = json.loads(line[len(RESULT_SENTINEL) :])
    if payload is None:
        err = _remap_lines(proc.stderr.strip() or proc.stdout.strip(), offset)
        return [], err.splitlines()[0] if err else "no result produced"
    if "contract_error" in payload:
        # Same wording as the production sandbox (ritesmith/runtime/sandbox.py).
        return [], "Script must define a 'run(input, context)' function (global, not local)"
    if "load_error" in payload:
        return [], "top-level error: " + _remap_lines(payload["load_error"], offset)

    results = []
    for i, (case, outcome) in enumerate(zip(cases, payload["cases"], strict=True), start=1):
        label = f"Test case {i} (input={_short(case['input'], 200)})"
        if not outcome["ok"]:
            msg = _remap_lines(outcome.get("error", ""), offset)
            results.append(CaseResult(i, False, f"{label} raised: {msg}"))
            continue
        mismatch = compare(case["expected"], outcome.get("output"))
        if mismatch is None:
            calls = outcome.get("calls") or {}
            for name, count in case.get("expected_calls", {}).items():
                actual = calls.get(name, 0) if isinstance(calls, dict) else 0
                if actual != count:
                    mismatch = f"expected {count} call(s) to tools.{name}, got {actual}"
                    break
        if mismatch:
            got = _short(outcome.get("output"))
            results.append(CaseResult(i, False, f"{label} failed: {mismatch} (full output: {got})"))
        else:
            results.append(CaseResult(i, True))
    return results, None


def validate(task: dict, script: str, target: str, typecheck_gate: str = "none") -> SampleCheck:
    """Runs every stage; later stages still run for metrics even if earlier ones fail."""
    check = SampleCheck(
        target=target,
        lines=script.count("\n") + 1,
        bytes=len(script.encode()),
        exceeds_prod_limits=script.count("\n") + 1 > PROD_MAX_LINES
        or len(script.encode()) > PROD_MAX_BYTES,
    )
    check.compile_errors = compile_errors(script, target)
    check.forbidden = forbidden_tokens(script, target)
    check.dialect_slips = dialect_slips(script, target)

    if target == "luau" and check.compiled:
        for mode in ("nonstrict", "strict"):
            tc = typecheck(task, script, mode)
            check.typecheck[mode] = {"passed": tc.passed, "errors": tc.errors, "lints": tc.lints}
            if mode == typecheck_gate:
                check.gated_type_errors = tc.errors

    if check.compiled:
        cases, check.exec_error = execute(task, script, target)
        check.cases = [asdict(c) for c in cases]
    return check
