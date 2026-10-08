"""ValidationPipeline — executa checks em sequência sobre um artefato.

Checks para lua_script (em ordem):
1. SyntaxCheck          — carrega em Lupa, captura LuaError
2. ForbiddenTokenCheck  — regex de tokens proibidos
3. SizeLimitCheck       — máx 60 linhas, máx 4KB
4. SchemaPresenceCheck  — deve ter 'function run(input'
5. AllowedPrimitivesCheck — host functions usadas estão no profile?
6. TestExecutionCheck   — executa test_cases fornecidos

Política NÃO é verificada aqui: `valid` significa apenas "validado" (sintaxe,
contrato, tipos, testes). A aprovação para execução é do PolicyEngine, no
PlanBuilder e no ExecutionService (os três estados: gerado → validado → aprovado).

Checks para luau_script (LunarDyson):
1. TypeCheck            — luau-analyze in-process contra as tools do profile e os tipos
                          Input/Output derivados dos schemas: sintaxe + nonstrict são
                          bloqueantes (afetam `valid`); strict é sempre reportado como
                          WARNING (não bloqueia `valid`). Quem decide a certificação
                          strict vs nonstrict é o chamador (GenerationService).
2. ForbiddenTokenCheck  — mesma deny-list + getfenv/setfenv/loadstring
3. SizeLimitCheck       — limites 1.5× maiores (anotações de tipo ocupam espaço)
4. SchemaPresenceCheck  — deve ter 'function run(input'
5. TestExecutionCheck   — como no Lua, executando via LunarDyson
AllowedPrimitivesCheck (luau_tools_in_profile) é bloqueante: usar uma tool fora do
profile só aparece no type check *strict* (que virou warning em P0.2), mas é uma
fronteira de segurança, não refinamento de tipo — então checamos as referências
`tools.ns.fn` deterministicamente contra o profile, independente da certificação.
"""

import asyncio
import re

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
_LUAU_TOOL_REF = re.compile(r"\btools\.([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)")

_MAX_LINES = 60
_MAX_BYTES = 4 * 1024  # 4KB

_LUAU_EXTRA_FORBIDDEN = [
    (re.compile(r"\bgetfenv\s*\("), "função 'getfenv' é proibida"),
    (re.compile(r"\bsetfenv\s*\("), "função 'setfenv' é proibida"),
    (re.compile(r"\bloadstring\s*\("), "função 'loadstring' é proibida"),
]
_LUAU_SIZE_FACTOR = 1.5


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
        input_schema: dict | None = None,
        output_schema: dict | None = None,
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

            if test_cases:
                exec_checks = await self._check_test_execution(content, test_cases, profile)
                checks.extend(exec_checks)

        elif artifact_type == "luau_script":
            checks.extend(self._check_luau_tools_in_profile(content, profile))
            checks.extend(self._check_forbidden_tokens(content, extra=_LUAU_EXTRA_FORBIDDEN))
            checks.extend(self._check_size(content, constraints, factor=_LUAU_SIZE_FACTOR))
            checks.extend(self._check_schema_presence(content))
            checks.extend(
                await self._check_luau_session(
                    content, profile, input_schema, output_schema, test_cases or []
                )
            )

        elif artifact_type in ("trama_workflow", "workflow_template"):
            checks.extend(self._check_json_syntax(content))

        else:
            # Tipos genéricos: só verifica que tem conteúdo
            checks.append(
                ValidationCheck(
                    name="content_present",
                    status="passed",
                    message=None,
                )
            )

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
            return [
                ValidationCheck(
                    name="syntax",
                    status="failed",
                    message=f"Lua syntax error: {errors[0]}",
                )
            ]
        return [ValidationCheck(name="syntax", status="passed")]

    async def _check_luau_session(self, content, profile, input_schema, output_schema, cases):
        from ritesmith.runtime.validation_session import LuauValidationSession, preflight

        def evaluate():
            session = LuauValidationSession(self.settings, profile, input_schema, output_schema)
            test_checks = []
            try:
                diagnostics = session.check(content)
                try:
                    specs = preflight(cases, session.tools, input_schema, output_schema)
                except Exception as exc:
                    return diagnostics, [
                        ValidationCheck(name="test_spec_fixture", status="failed", message=str(exc))
                    ]
                if diagnostics["nonstrict"]:
                    return diagnostics, []
                for index, spec in enumerate(specs):
                    result, fixture_error, errors = session.execute(content, spec)
                    error = fixture_error or (result.message if not result.ok else None)
                    error = error or ("; ".join(errors) if errors else None)
                    test_checks.append(
                        ValidationCheck(
                            name=f"test_fixture_{index}" if fixture_error else f"test_case_{index}",
                            status="failed" if error else "passed",
                            message=f"Test case {index}: {error}" if error else None,
                        )
                    )
                return diagnostics, test_checks
            finally:
                session.close()

        diagnostics, test_checks = await asyncio.to_thread(evaluate)
        return (
            await self._check_luau_types(
                content, profile, input_schema, output_schema, diagnostics=diagnostics
            )
            + test_checks
        )

    async def _check_luau_types(
        self,
        content: str,
        profile: str,
        input_schema: dict | None,
        output_schema: dict | None,
        diagnostics: dict | None = None,
    ) -> list[ValidationCheck]:
        from ritesmith.runtime.luau import LuauScriptRuntime

        rt = LuauScriptRuntime(self.settings)
        diags = diagnostics or await asyncio.to_thread(
            rt.check,
            content,
            profile=profile,
            input_schema=input_schema,
            output_schema=output_schema,
        )

        def fmt(d: dict) -> str:
            return f"line {d['line']}: {d['message']}"

        syntax = [fmt(d) for d in diags["nonstrict"] if d["kind"] == "SyntaxError"]
        if syntax:
            return [
                ValidationCheck(
                    name="syntax",
                    status="failed",
                    message="Luau syntax error: " + "; ".join(syntax),
                )
            ]
        checks = [ValidationCheck(name="syntax", status="passed")]

        nonstrict = [fmt(d) for d in diags["nonstrict"]]
        if nonstrict:
            checks.append(
                ValidationCheck(
                    name="type_check",
                    status="failed",
                    message="Type errors: " + "; ".join(nonstrict),
                )
            )
            return checks
        checks.append(ValidationCheck(name="type_check", status="passed"))

        strict = [fmt(d) for d in diags["strict"] if fmt(d) not in nonstrict]
        if strict:
            checks.append(
                ValidationCheck(
                    name="strict_type_check",
                    status="warning",
                    message="Strict-mode type errors: " + "; ".join(strict),
                )
            )
        else:
            checks.append(ValidationCheck(name="strict_type_check", status="passed"))
        return checks

    def _check_luau_tools_in_profile(self, content: str, profile: str) -> list[ValidationCheck]:
        from ritesmith.runtime.luau import luau_tools_for_profile

        allowed = set(luau_tools_for_profile(profile))
        used = {f"{m.group(1)}.{m.group(2)}" for m in _LUAU_TOOL_REF.finditer(content)}
        unknown = used - allowed
        if unknown:
            return [
                ValidationCheck(
                    name="tools_in_profile",
                    status="failed",
                    message=f"Tools não disponíveis no profile '{profile}': {', '.join(sorted(unknown))}",
                )
            ]
        return [ValidationCheck(name="tools_in_profile", status="passed")]

    def _check_forbidden_tokens(
        self, content: str, extra: list | None = None
    ) -> list[ValidationCheck]:
        hits = []
        for pattern, msg in _FORBIDDEN_PATTERNS + (extra or []):
            if pattern.search(content):
                hits.append(msg)
        if hits:
            return [
                ValidationCheck(
                    name="forbidden_tokens",
                    status="failed",
                    message="; ".join(hits),
                )
            ]
        return [ValidationCheck(name="forbidden_tokens", status="passed")]

    def _check_size(
        self, content: str, constraints: dict | None, factor: float = 1.0
    ) -> list[ValidationCheck]:
        max_lines = int((constraints or {}).get("max_lines", _MAX_LINES) * factor)
        max_bytes = int(_MAX_BYTES * factor)
        lines = content.splitlines()
        size_bytes = len(content.encode())
        issues = []
        if len(lines) > max_lines:
            issues.append(f"Script tem {len(lines)} linhas (máx {max_lines})")
        if size_bytes > max_bytes:
            issues.append(f"Script tem {size_bytes} bytes (máx {max_bytes})")
        if issues:
            return [
                ValidationCheck(
                    name="size_limit",
                    status="failed",
                    message="; ".join(issues),
                )
            ]
        return [ValidationCheck(name="size_limit", status="passed")]

    def _check_schema_presence(self, content: str) -> list[ValidationCheck]:
        if not _RUN_SIGNATURE.search(content):
            return [
                ValidationCheck(
                    name="run_function",
                    status="failed",
                    message="Script deve definir exatamente uma função 'run(input, ...)'",
                )
            ]
        return [ValidationCheck(name="run_function", status="passed")]

    def _check_allowed_primitives(self, content: str, profile: str) -> list[ValidationCheck]:
        allowed = set(list_names_for_profile(profile))
        used = {m.group(1) for m in _HOST_FN_CALL.finditer(content)}
        # filtra chamadas que parecem host functions (têm namespace)
        host_calls = {name for name in used if "." in name}
        unknown = host_calls - allowed
        if unknown:
            return [
                ValidationCheck(
                    name="allowed_primitives",
                    status="failed",
                    message=f"Host functions não permitidas no profile '{profile}': {', '.join(sorted(unknown))}",
                )
            ]
        return [ValidationCheck(name="allowed_primitives", status="passed")]

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
        luau: bool = False,
    ) -> list[ValidationCheck]:
        if luau:
            from ritesmith.runtime.luau import LuauScriptRuntime

            rt = LuauScriptRuntime(self.settings)
        else:
            from ritesmith.runtime.lua import LuaScriptRuntime

            rt = LuaScriptRuntime(self.settings)
        checks = []
        for i, tc in enumerate(test_cases):
            input_data = tc.get("input", {})
            expected = tc.get("expected_output")
            result = await rt.execute(content, input_data, {}, profile=profile)
            if result.error:
                checks.append(
                    ValidationCheck(
                        name=f"test_case_{i}",
                        status="failed",
                        message=f"Test case {i} error: {result.error}",
                    )
                )
            elif expected is not None and result.output != expected:
                checks.append(
                    ValidationCheck(
                        name=f"test_case_{i}",
                        status="failed",
                        message=f"Test case {i}: expected {expected}, got {result.output}",
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        name=f"test_case_{i}",
                        status="passed",
                        message=f"Test case {i} passed",
                    )
                )
        return checks
