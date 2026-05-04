"""ValidationPipeline — executa checks em sequência sobre um artefato.

Checks para lua_script (em ordem):
1. SyntaxCheck          — carrega em Lupa, captura LuaError
2. ForbiddenTokenCheck  — regex de tokens proibidos
3. SizeLimitCheck       — máx 60 linhas, máx 4KB
4. SchemaPresenceCheck  — deve ter 'function run(input'
5. AllowedPrimitivesCheck — host functions usadas estão no profile?
6. PolicyCheck          — stub (always allow) — wired na Fase 6
7. TestExecutionCheck   — executa test_cases fornecidos
"""
import re
from dataclasses import dataclass, field

from ritesmith.config import Settings, get_settings
from ritesmith.runtime.host_functions import list_names_for_profile
from ritesmith.schemas.artifact import ValidationCheck, ValidationResult

# Tokens Lua absolutamente proibidos (deny-list conservador)
_FORBIDDEN_PATTERNS = [
    (re.compile(r"\brequire\b"), "uso de 'require' é proibido"),
    (re.compile(r"\bos\s*[\.\[]"), "módulo 'os' é proibido"),
    (re.compile(r"\bio\s*[\.\[]"), "módulo 'io' é proibido"),
    (re.compile(r"\bdebug\s*[\.\[]"), "módulo 'debug' é proibido"),
    (re.compile(r"\bload\s*\("), "função 'load' é proibida"),
    (re.compile(r"\bloadfile\s*\("), "função 'loadfile' é proibida"),
    (re.compile(r"\bdofile\s*\("), "função 'dofile' é proibida"),
    (re.compile(r"\brawget\s*\("), "função 'rawget' é proibida"),
    (re.compile(r"\brawset\s*\("), "função 'rawset' é proibida"),
    (re.compile(r"\bpackage\s*[\.\[]"), "módulo 'package' é proibido"),
    (re.compile(r"\bcollectgarbage\s*\("), "função 'collectgarbage' é proibida"),
    (re.compile(r"\bnewproxy\s*\("), "função 'newproxy' é proibida"),
]

_RUN_SIGNATURE = re.compile(r"\bfunction\s+run\s*\(")
_HOST_FN_CALL = re.compile(r"\b([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\(")

_MAX_LINES = 60
_MAX_BYTES = 4 * 1024  # 4KB


class ValidationPipeline:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def run(
        self,
        content: str,
        artifact_type: str,
        *,
        constraints: dict | None = None,
        test_cases: list[dict] | None = None,
        profile: str = "transform_only",
    ) -> ValidationResult:
        checks: list[ValidationCheck] = []
        errors: list[str] = []
        warnings: list[str] = []

        if artifact_type == "lua_script":
            checks.extend(self._check_syntax(content))
            checks.extend(self._check_forbidden_tokens(content))
            checks.extend(self._check_size(content, constraints))
            checks.extend(self._check_schema_presence(content))
            checks.extend(self._check_allowed_primitives(content, profile))
            checks.extend(self._check_policy(artifact_type, constraints))

            if test_cases:
                exec_checks = await self._check_test_execution(content, test_cases, profile)
                checks.extend(exec_checks)

        elif artifact_type in ("trama_workflow", "workflow_template"):
            checks.extend(self._check_json_syntax(content))

        else:
            # Tipos genéricos: só verifica que tem conteúdo
            checks.append(ValidationCheck(
                name="content_present",
                status="passed",
                message=None,
            ))

        failed = [c for c in checks if c.status == "failed"]
        warned = [c for c in checks if c.status == "warning"]

        for c in failed:
            errors.append(c.message or c.name)
        for c in warned:
            warnings.append(c.message or c.name)

        valid = len(failed) == 0

        return ValidationResult(
            valid=valid,
            checks=checks,
            warnings=warnings,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Checks individuais
    # ------------------------------------------------------------------

    def _check_syntax(self, content: str) -> list[ValidationCheck]:
        from ritesmith.runtime.lua import LuaScriptRuntime
        rt = LuaScriptRuntime(self.settings)
        errors = rt.validate_syntax(content)
        if errors:
            return [ValidationCheck(
                name="syntax",
                status="failed",
                message=f"Lua syntax error: {errors[0]}",
            )]
        return [ValidationCheck(name="syntax", status="passed")]

    def _check_forbidden_tokens(self, content: str) -> list[ValidationCheck]:
        hits = []
        for pattern, msg in _FORBIDDEN_PATTERNS:
            if pattern.search(content):
                hits.append(msg)
        if hits:
            return [ValidationCheck(
                name="forbidden_tokens",
                status="failed",
                message="; ".join(hits),
            )]
        return [ValidationCheck(name="forbidden_tokens", status="passed")]

    def _check_size(self, content: str, constraints: dict | None) -> list[ValidationCheck]:
        max_lines = (constraints or {}).get("max_lines", _MAX_LINES)
        lines = content.splitlines()
        size_bytes = len(content.encode())
        issues = []
        if len(lines) > max_lines:
            issues.append(f"Script tem {len(lines)} linhas (máx {max_lines})")
        if size_bytes > _MAX_BYTES:
            issues.append(f"Script tem {size_bytes} bytes (máx {_MAX_BYTES})")
        if issues:
            return [ValidationCheck(
                name="size_limit",
                status="failed",
                message="; ".join(issues),
            )]
        return [ValidationCheck(name="size_limit", status="passed")]

    def _check_schema_presence(self, content: str) -> list[ValidationCheck]:
        if not _RUN_SIGNATURE.search(content):
            return [ValidationCheck(
                name="run_function",
                status="failed",
                message="Script deve definir exatamente uma função 'run(input, ...)'",
            )]
        return [ValidationCheck(name="run_function", status="passed")]

    def _check_allowed_primitives(self, content: str, profile: str) -> list[ValidationCheck]:
        allowed = set(list_names_for_profile(profile))
        used = set(m.group(1) for m in _HOST_FN_CALL.finditer(content))
        # filtra chamadas que parecem host functions (têm namespace)
        host_calls = {name for name in used if "." in name}
        unknown = host_calls - allowed
        if unknown:
            return [ValidationCheck(
                name="allowed_primitives",
                status="failed",
                message=f"Host functions não permitidas no profile '{profile}': {', '.join(sorted(unknown))}",
            )]
        return [ValidationCheck(name="allowed_primitives", status="passed")]

    def _check_policy(self, artifact_type: str, constraints: dict | None) -> list[ValidationCheck]:
        # Stub — PolicyEngine real é wired na Fase 6
        return [ValidationCheck(name="policy", status="passed", message="policy check stub (allow)")]

    def _check_json_syntax(self, content: str) -> list[ValidationCheck]:
        import json
        try:
            json.loads(content)
            return [ValidationCheck(name="json_syntax", status="passed")]
        except Exception as e:
            return [ValidationCheck(name="json_syntax", status="failed", message=str(e))]

    async def _check_test_execution(
        self,
        content: str,
        test_cases: list[dict],
        profile: str,
    ) -> list[ValidationCheck]:
        from ritesmith.runtime.lua import LuaScriptRuntime
        rt = LuaScriptRuntime(self.settings)
        checks = []
        for i, tc in enumerate(test_cases):
            input_data = tc.get("input", {})
            expected = tc.get("expected_output")
            result = await rt.execute(content, input_data, {}, profile=profile)
            if result.error:
                checks.append(ValidationCheck(
                    name=f"test_case_{i}",
                    status="failed",
                    message=f"Test case {i} error: {result.error}",
                ))
            elif expected is not None and result.output != expected:
                checks.append(ValidationCheck(
                    name=f"test_case_{i}",
                    status="failed",
                    message=f"Test case {i}: expected {expected}, got {result.output}",
                ))
            else:
                checks.append(ValidationCheck(
                    name=f"test_case_{i}",
                    status="passed",
                    message=f"Test case {i} passed",
                ))
        return checks
