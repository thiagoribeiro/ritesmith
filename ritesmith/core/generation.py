"""GenerationService — orquestra o loop: busca → reuso → gera → valida → repara.

O idioma do script vem de req.language ou settings.script_language ("luau" por
padrão, gerando luau_script executado pelo LunarDyson; cai para "lua" se a lib ou
o provider de LLM não suportarem Luau). Em Luau, o type check strict é bloqueante
em todas as tentativas menos a última, onde erros só-strict viram warnings.

Fluxo principal (generate_lua):
1. Busca FTS por artifacts similares
2. Se match suficientemente bom e reuse_policy != force_new → retorna existente
3. Cria GenerationJob
4. Loop de até max_attempts:
   a. Chama LLM.generate_lua()
   b. Valida com ValidationPipeline
   c. Se válido → break
   d. Senão → chama LLM.repair_lua() e tenta de novo
5. Se save=True e válido → persiste artifact
6. Retorna GeneratedArtifactResponse
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.ids import generate_id
from ritesmith.core.repair import run_repair_loop
from ritesmith.core.reuse import check_reuse
from ritesmith.core.validation import ValidationPipeline
from ritesmith.llm.base import LLMProvider, LuaGenerationResponse
from ritesmith.registry.models import GenerationAttempt, GenerationJob
from ritesmith.registry.search import fts_search
from ritesmith.registry.service import RegistryService
from ritesmith.runtime.host_functions import list_names_for_profile
from ritesmith.runtime.luau import (
    describe_tools_for_prompt,
    effective_script_language,
    luau_available,
    script_artifact_type,
    script_type_declarations,
)
from ritesmith.schemas.artifact import (
    Artifact,
    ArtifactStatus,
    ArtifactType,
    ValidationResult,
)
from ritesmith.schemas.generation import GeneratedArtifactResponse, GenerateScriptRequest

log = logging.getLogger(__name__)


def _is_strict_clean(validation) -> bool:
    """True when no strict-mode type errors remain (luau); always true for lua/trama."""
    if validation is None:
        return False
    return not any(
        c.name == "strict_type_check" and c.status == "warning" for c in validation.checks
    )


def _certification(language: str, strict_clean: bool) -> str:
    """strict | nonstrict (luau) | lua (legacy)."""
    if language != "luau":
        return "lua"
    return "strict" if strict_clean else "nonstrict"


_RISK_ORDER = ["low", "medium", "high", "critical"]


def _risk_at_least(risk: str | None, threshold: str | None) -> bool:
    """True when `risk` is at or above `threshold`. Unknown/empty threshold disables."""
    if not threshold or threshold not in _RISK_ORDER:
        return False
    r = risk or "low"
    if r not in _RISK_ORDER:
        return False
    return _RISK_ORDER.index(r) >= _RISK_ORDER.index(threshold)


class GenerationService:
    def __init__(
        self,
        db: AsyncSession,
        llm: LLMProvider,
        settings: Settings,
        audit: AuditLogger | None = None,
    ):
        self.db = db
        self.llm = llm
        self.settings = settings
        self.validator = ValidationPipeline(settings)
        self.registry = RegistryService(db)
        self.audit = audit

    async def generate_lua(
        self,
        req: GenerateScriptRequest,
        plan_id: str | None = None,
    ) -> GeneratedArtifactResponse:
        constraints = req.constraints.model_dump() if req.constraints else {}
        profile = constraints.get("runtime_profile", "transform_only")
        reuse_policy = constraints.get("reuse_policy", "prefer_reuse")
        language = self._resolve_language(req)
        artifact_type = script_artifact_type(language)
        max_attempts = self.settings.generation_max_attempts

        # 1+2+3. Reuse check — a working script is reusable whatever its language,
        # but only if it is contract-compatible with this request and relevant.
        if reuse := await check_reuse(
            self.db,
            req.intent,
            ["luau_script", "lua_script"],
            reuse_policy,
            self.audit,
            input_schema=req.input_schema,
            output_schema=req.output_schema,
            runtime_profile=profile,
            llm=self.llm,
            settings=self.settings,
        ):
            return reuse

        # Similar artifacts for few-shot prompt context (same language only)
        search_results = await fts_search(
            self.db, req.intent, artifact_types=[artifact_type], limit=5
        )

        # 3. Registra GenerationJob
        job = await self._create_job(req, plan_id, artifact_type)
        log.info(
            "generation job created",
            extra={"job_id": job.generation_id, "goal": req.intent[:80], "language": language},
        )
        allowed_fns = list_names_for_profile(profile)
        if language == "luau":
            tool_descriptions = describe_tools_for_prompt(profile)
            type_declarations = script_type_declarations(req.input_schema, req.output_schema)
        similar_dicts = [
            {"name": r.artifact.name, "content": r.version.content if r.version else ""}
            for r in search_results[:2]
        ]

        last_script: str | None = None
        last_validation: ValidationResult | None = None
        final_response: LuaGenerationResponse | None = None
        effective_tests: list[dict] | None = (req.context or {}).get("test_cases")
        tests_attempted = False

        async def attempt_fn(attempt: int) -> bool:
            nonlocal last_script, last_validation, final_response
            nonlocal effective_tests, tests_attempted

            if language == "luau" and (attempt == 1 or last_script is None):
                llm_response, stats = await self.llm.generate_luau(
                    goal=req.intent,
                    input_schema=req.input_schema,
                    output_schema=req.output_schema,
                    tool_descriptions=tool_descriptions,
                    type_declarations=type_declarations,
                    similar_artifacts=similar_dicts,
                    constraints=constraints,
                )
                script = llm_response.script
            elif attempt == 1 or last_script is None:
                llm_response, stats = await self.llm.generate_lua(
                    goal=req.intent,
                    input_schema=req.input_schema,
                    output_schema=req.output_schema,
                    allowed_host_functions=allowed_fns,
                    similar_artifacts=similar_dicts,
                    constraints=constraints,
                )
                script = llm_response.script
            else:
                # Feed strict warnings to the repair too, so the loop tries to reach a
                # strict-clean (certified) program before settling for nonstrict.
                errors = list(last_validation.errors) if last_validation else []
                if language == "luau" and last_validation:
                    errors += last_validation.warnings
                if language == "luau":
                    repair_resp, stats = await self.llm.repair_luau(
                        original_goal=req.intent,
                        current_script=last_script,
                        validation_errors=errors,
                        attempt_number=attempt,
                        tool_descriptions=tool_descriptions,
                        type_declarations=type_declarations,
                    )
                else:
                    repair_resp, stats = await self.llm.repair_lua(
                        original_goal=req.intent,
                        current_script=last_script,
                        validation_errors=errors,
                        attempt_number=attempt,
                    )
                script = repair_resp.repaired_content
                llm_response = LuaGenerationResponse(
                    script=script,
                    name=final_response.name if final_response else req.intent[:40],
                    description=final_response.description if final_response else req.intent,
                    tags=final_response.tags if final_response else [],
                    risk_assessment=final_response.risk_assessment if final_response else "low",
                    runtime_profile=profile,
                )

            final_response = llm_response

            if self.audit:
                await self.audit.log_event(
                    "generation.llm_call",
                    "generation_job",
                    job.generation_id,
                    payload={
                        "attempt": attempt,
                        "tokens": stats.total_tokens,
                        "model": stats.model,
                    },
                )

            # P0.3 risk-class test gate: a medium+ risk script must pass executed
            # test cases. Use the caller's cases, else generate sanity cases once
            # from the intent+schemas (not the script). Failing cases block `valid`
            # and drive repair; *missing* cases are an acceptance gate handled after
            # the loop (repairing the script cannot conjure tests).
            require_tests = _risk_at_least(
                llm_response.risk_assessment, self.settings.require_tests_min_risk
            )
            if require_tests and not effective_tests and not tests_attempted:
                tests_attempted = True
                try:
                    generated, _tstats = await self.llm.generate_tests(
                        req.intent, req.input_schema, req.output_schema
                    )
                    effective_tests = generated or None
                except NotImplementedError:
                    pass

            validation = await self.validator.run(
                content=script,
                artifact_type=artifact_type,
                constraints=constraints,
                test_cases=effective_tests,
                profile=profile,
                input_schema=req.input_schema,
                output_schema=req.output_schema,
            )

            await self._record_attempt(job.generation_id, attempt, script, validation)
            log.info(
                "generation attempt %d/%d valid=%s",
                attempt,
                self.settings.generation_max_attempts,
                validation.valid,
                extra={
                    "job_id": job.generation_id,
                    "attempt": attempt,
                    "valid": validation.valid,
                    "errors": validation.errors[:3],
                },
            )

            last_script = script
            last_validation = validation
            # For luau, keep repairing while strict errors remain so the loop drives
            # toward a strict-clean (certified) program; it only stops early once the
            # script is both valid and strict-clean. At exhaustion the caller still
            # accepts a nonstrict-valid script (certification="nonstrict"), unless
            # require_strict_typecheck forces a generation failure.
            if language == "luau":
                return validation.valid and _is_strict_clean(validation)
            return validation.valid

        try:
            await run_repair_loop(
                artifact_type=artifact_type,
                max_attempts=max_attempts,
                attempt_fn=attempt_fn,
            )

            # 5. Certification + acceptance. `valid` = validated (syntax/contract/nonstrict
            # types/tests). Strict type errors are reported as warnings and decide the
            # certification tier, not validity.
            strict_clean = _is_strict_clean(last_validation)
            certification = _certification(language, strict_clean)
            # require_strict_typecheck: a luau program that only clears nonstrict is a
            # generation failure; otherwise it is persisted as certification="nonstrict"
            # and the PolicyEngine will require approval to run it.
            require_strict = self.settings.require_strict_typecheck and language == "luau"
            # P0.3: a medium+ risk script with no executed test cases (caller or
            # generated) is not accepted — this is an acceptance gate, not a
            # validity failure, so it does not drive the repair loop.
            require_tests = _risk_at_least(
                final_response.risk_assessment if final_response else None,
                self.settings.require_tests_min_risk,
            )
            tests_ok = (not require_tests) or bool(effective_tests)
            accepted = bool(
                last_validation
                and last_validation.valid
                and not (require_strict and not strict_clean)
                and tests_ok
            )
            job.status = "completed" if accepted else "failed"
            job.finished_at = datetime.now(UTC)
            job.attempts = self.settings.generation_max_attempts
            log.info(
                "generation job finished status=%s",
                job.status,
                extra={"job_id": job.generation_id, "status": job.status},
            )

            # 6. Persiste se solicitado e válido
            artifact_orm = None
            artifact_version_orm = None
            if req.save and accepted and final_response:
                artifact_orm, artifact_version_orm = await self.registry.create_artifact(
                    name=final_response.name,
                    artifact_type=artifact_type,
                    content=last_script,
                    description=final_response.description,
                    usage_description=final_response.usage_description or None,
                    tags=final_response.tags,
                    input_schema=req.input_schema,
                    output_schema=req.output_schema,
                    risk_level=final_response.risk_assessment,
                    generated_by_plan_id=plan_id,
                    metadata={"runtime_profile": profile, "certification": certification},
                )
                job.final_artifact_id = artifact_orm.artifact_id
                await self.db.flush()

                if self.audit:
                    await self.audit.log_event(
                        "artifact.created",
                        "artifact",
                        artifact_orm.artifact_id,
                        payload={"goal": req.intent, "generation_id": job.generation_id},
                    )
        except Exception:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            log.error(
                "generation job failed with exception",
                extra={"job_id": job.generation_id},
                exc_info=True,
            )
            raise
        finally:
            await self.db.commit()

        # 7. Monta resposta (mesmo sem salvar, retorna o artifact transitório)
        if artifact_orm and artifact_version_orm:
            artifact_schema = _build_artifact(artifact_orm, artifact_version_orm)
        else:
            artifact_schema = self._make_transient_artifact(
                artifact_type=artifact_type,
                script=last_script or "",
                gen_response=final_response,
                validation=last_validation,
                plan_id=plan_id,
                certification=certification,
            )

        return GeneratedArtifactResponse(
            artifact=artifact_schema,
            validation=last_validation,
            reused=False,
        )

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _resolve_language(self, req: GenerateScriptRequest) -> str:
        language = req.language or effective_script_language(self.settings)
        if language == "luau" and not (luau_available() and self.llm.supports_luau):
            log.warning("luau requested but unavailable (lib or LLM provider); generating lua")
            return "lua"
        return "luau" if language == "luau" else "lua"

    async def _create_job(
        self, req: GenerateScriptRequest, plan_id: str | None, artifact_type: str
    ) -> GenerationJob:
        job = GenerationJob(
            generation_id=generate_id("gen"),
            goal=req.intent,
            target_type=artifact_type,
            status="running",
            input_schema=req.input_schema,
            output_schema=req.output_schema,
            constraints=req.constraints.model_dump() if req.constraints else None,
            plan_id=plan_id,
        )
        self.db.add(job)
        await self.db.flush()
        return job

    async def _record_attempt(
        self,
        generation_id: str,
        attempt_number: int,
        content: str,
        validation: ValidationResult,
    ) -> None:
        attempt = GenerationAttempt(
            generation_id=generation_id,
            attempt_number=attempt_number,
            content=content,
            validation_result=validation.model_dump(),
        )
        self.db.add(attempt)
        await self.db.flush()

    def _make_transient_artifact(
        self,
        artifact_type: str,
        script: str,
        gen_response: LuaGenerationResponse | None,
        validation: ValidationResult | None,
        plan_id: str | None,
        certification: str = "lua",
    ) -> Artifact:
        now = datetime.now(UTC)
        meta = {"certification": certification}
        if gen_response:
            meta["runtime_profile"] = gen_response.runtime_profile
        return Artifact(
            artifact_id=generate_id("art"),
            artifact_type=ArtifactType(artifact_type),
            name=gen_response.name if gen_response else "generated_script",
            version=1,
            status=ArtifactStatus.draft,
            content=script,
            description=gen_response.description if gen_response else None,
            usage_description=(gen_response.usage_description or None) if gen_response else None,
            tags=gen_response.tags if gen_response else [],
            risk_level=gen_response.risk_assessment if gen_response else "low",
            generated_by_plan_id=plan_id,
            validation=validation,
            metadata=meta,
            created_at=now,
            updated_at=now,
        )
