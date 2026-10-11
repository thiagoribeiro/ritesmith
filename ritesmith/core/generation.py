"""Generate, validate and certify scripts with bounded recovery and truthful acceptance."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.diagnostics import validation_diagnostics
from ritesmith.core.exceptions import GenerationFailedError, LLMError
from ritesmith.core.generation_budget import bounded_generation
from ritesmith.core.ids import generate_id
from ritesmith.core.pending_tests import finish_pending_tests
from ritesmith.core.repair import run_repair_loop
from ritesmith.core.reuse import check_reuse
from ritesmith.core.validation import ValidationPipeline
from ritesmith.llm.base import LLMCallStats, LLMProvider, LuaGenerationResponse
from ritesmith.observability.generation import current_trace, outcome, stage
from ritesmith.registry.models import GenerationAttempt, GenerationJob
from ritesmith.registry.search import SearchResult, fts_search
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
    ValidationCheck,
    ValidationResult,
)
from ritesmith.schemas.generation import GeneratedArtifactResponse, GenerateScriptRequest
from ritesmith.schemas.test_spec import bind_known_fixtures, functional_tests

log = logging.getLogger(__name__)


def _is_strict_clean(validation) -> bool:
    """True only when the Luau strict checker explicitly passed."""
    if validation is None:
        return False
    return any(c.name == "strict_type_check" and c.status == "passed" for c in validation.checks)


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

    @bounded_generation
    async def generate_lua(
        self,
        req: GenerateScriptRequest,
        plan_id: str | None = None,
        *,
        initial: tuple[LuaGenerationResponse, LLMCallStats] | None = None,
        recall: list[SearchResult] | None = None,
        pre_reused: GeneratedArtifactResponse | None = None,
        initial_tests: tuple[list[dict] | None, bool] | None = None,
        reuse_checked: bool = False,
    ) -> GeneratedArtifactResponse:
        if pre_reused is not None:
            return pre_reused
        constraints = req.constraints.model_dump() if req.constraints else {}
        if req.required_capabilities is not None:
            constraints["required_capabilities"] = req.required_capabilities
        profile = constraints.get("runtime_profile", "transform_only")
        reuse_policy = constraints.get("reuse_policy", "prefer_reuse")
        language = self._resolve_language(req)
        artifact_type = script_artifact_type(language)
        trace = current_trace.get()
        if trace is not None:
            trace.artifact_types.add(artifact_type)
        max_attempts = self.settings.generation_max_attempts

        if req.context:
            constraints = {**constraints, "context": req.context}
        if recall is None:
            with stage("recall", artifact_type):
                recall = await fts_search(
                    self.db,
                    req.intent,
                    artifact_types=["luau_script", "lua_script"],
                    limit=max(5, self.settings.reuse_recall_limit),
                )

        # 1+2+3. Reuse check — a working script is reusable whatever its language,
        # but only if it is contract-compatible with this request and relevant.
        if not reuse_checked and (
            reuse := await check_reuse(
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
                results=recall,
            )
        ):
            return reuse

        # Similar artifacts for few-shot prompt context (same language only)
        search_results = [r for r in recall if r.artifact.artifact_type == artifact_type][:5]

        # 3. Record the generation job
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
        trace = current_trace.get()
        if trace is not None:
            trace.client_tests |= bool(effective_tests)
        tests_attempted = False
        if initial_tests is not None and not effective_tests:
            effective_tests, tests_attempted = initial_tests
        active_llm = self.llm
        tests_task = None
        test_failure = None
        candidate_failure = None
        if (
            self.settings.generation_parallel_tests
            and not effective_tests
            and not tests_attempted
            and self.settings.require_tests_min_risk not in ("", "off")
        ):
            tests_task = asyncio.create_task(
                self.llm.generate_validation_tests(
                    req.intent,
                    req.input_schema,
                    req.output_schema,
                    profile=profile,
                    fixtures=(req.context or {}).get("test_fixtures", []),
                )
            )

        async def attempt_fn(attempt: int) -> bool:
            nonlocal last_script, last_validation, final_response
            nonlocal effective_tests, tests_attempted, active_llm, test_failure
            nonlocal candidate_failure
            job.attempts = attempt
            if attempt > 1 and last_script is None and candidate_failure is None:
                active_llm = self.llm.fallback()

            if attempt == 1 and initial is not None:
                llm_response, stats = initial
                script = llm_response.script
            elif language == "luau" and (attempt == 1 or last_script is None):
                try:
                    llm_response, stats = await active_llm.generate_luau(
                        goal=req.intent,
                        input_schema=req.input_schema,
                        output_schema=req.output_schema,
                        tool_descriptions=tool_descriptions,
                        type_declarations=type_declarations,
                        similar_artifacts=similar_dicts,
                        constraints={
                            **constraints,
                            **(
                                {"generation_repair": candidate_failure}
                                if candidate_failure
                                else {}
                            ),
                        },
                    )
                except LLMError as exc:
                    if exc.details.get("candidate") is not None:
                        candidate_failure = {
                            "candidate": exc.details["candidate"],
                            "diagnostic": str(exc),
                            "location": exc.details.get("location"),
                        }
                        await self._record_attempt(
                            job.generation_id,
                            attempt,
                            str(exc.details["candidate"]),
                            ValidationResult(valid=False, errors=[str(exc)], checks=[]),
                        )
                    raise
                script = llm_response.script
            elif attempt == 1 or last_script is None:
                llm_response, stats = await active_llm.generate_lua(
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
                    repair_resp, stats = await active_llm.repair_luau(
                        original_goal=req.intent,
                        current_script=last_script,
                        validation_errors=errors,
                        attempt_number=attempt,
                        tool_descriptions=tool_descriptions,
                        type_declarations=type_declarations,
                    )
                else:
                    repair_resp, stats = await active_llm.repair_lua(
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

            llm_response.runtime_profile = profile
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
                        "stats": stats.model_dump(),
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
            if (
                (require_tests or tests_task is not None)
                and not effective_tests
                and not tests_attempted
            ):
                tests_attempted = True
                try:
                    with stage("tests", artifact_type):
                        generated, _tstats = (
                            await tests_task
                            if tests_task is not None
                            else await self.llm.generate_validation_tests(
                                req.intent,
                                req.input_schema,
                                req.output_schema,
                                profile=profile,
                                fixtures=(req.context or {}).get("test_fixtures", []),
                            )
                        )
                    effective_tests = generated or None
                except NotImplementedError:
                    pass
                except LLMError as exc:
                    test_failure = str(exc)

            if effective_tests and (req.context or {}).get("test_fixtures"):
                effective_tests = bind_known_fixtures(effective_tests, req.context["test_fixtures"])
            with stage("validation", artifact_type):
                validation = await self.validator.run(
                    content=script,
                    artifact_type=artifact_type,
                    constraints=constraints,
                    test_cases=effective_tests,
                    profile=profile,
                    input_schema=req.input_schema,
                    output_schema=req.output_schema,
                )

            if test_failure:
                validation.valid = False
                validation.errors.append(test_failure)
                validation.checks.append(
                    ValidationCheck(name="test_spec_fixture", status="failed", message=test_failure)
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
            performed = await run_repair_loop(
                artifact_type=artifact_type,
                max_attempts=max_attempts,
                attempt_fn=attempt_fn,
                settings=self.settings,
                initial_recoveries=max(0, initial[1].api_attempts - 1) if initial else 0,
                initial_duration=initial[1].duration_s if initial else 0,
                state_fn=lambda: (
                    last_script,
                    validation_diagnostics(last_validation) if last_validation else [],
                ),
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
            tests_ok = (not require_tests) or functional_tests(effective_tests)
            accepted = bool(
                last_validation
                and last_validation.valid
                and not (require_strict and not strict_clean)
                and tests_ok
            )
            outcome(artifact_type, accepted)
            job.status = "completed" if accepted else "failed"
            job.finished_at = datetime.now(UTC)
            job.attempts = performed
            if not accepted:
                diagnostics = list(last_validation.errors) if last_validation else []
                if candidate_failure:
                    diagnostics.append(candidate_failure["diagnostic"])
                if require_strict and not strict_clean:
                    diagnostics.append("Strict certification required")
                if not tests_ok:
                    diagnostics.append("Functional tests required")
                raise GenerationFailedError(
                    "No acceptable script after bounded recovery",
                    details={
                        "diagnostics": diagnostics,
                        "category": "response" if last_validation is None else "functional",
                    },
                )
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
        except asyncio.CancelledError:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            raise
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
            await finish_pending_tests(tests_task)
            with stage("persist", artifact_type):
                from ritesmith.core.generation_budget import current_budget

                budget = current_budget.get()
                if budget is not None and budget.remaining <= 0:
                    await self.db.rollback()
                else:
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

        outcome(artifact_type, accepted)
        artifact_schema._input_schema = req.input_schema
        artifact_schema._output_schema = req.output_schema
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
            created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
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
