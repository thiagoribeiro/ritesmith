"""GenerationService — orquestra o loop: busca → reuso → gera → valida → repara.

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
from ritesmith.llm.base import LLMCallStats, LLMProvider, LuaGenerationResponse
from ritesmith.registry.models import GenerationAttempt, GenerationJob
from ritesmith.registry.search import fts_search
from ritesmith.registry.service import RegistryService
from ritesmith.runtime.host_functions import list_names_for_profile
from ritesmith.schemas.artifact import (
    Artifact,
    ArtifactStatus,
    ArtifactType,
    ValidationResult,
)
from ritesmith.schemas.generation import GeneratedArtifactResponse, GenerateScriptRequest

log = logging.getLogger(__name__)


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

        # 1+2. Reuse check
        if reuse := await check_reuse(self.db, req.intent, ["lua_script"], reuse_policy, self.audit):
            return reuse

        # Similar artifacts for few-shot prompt context
        search_results = await fts_search(self.db, req.intent, artifact_types=["lua_script"], limit=5)

        # 3. Registra GenerationJob
        job = await self._create_job(req, plan_id)
        log.info("generation job created", extra={"job_id": job.generation_id, "goal": req.intent[:80]})
        allowed_fns = list_names_for_profile(profile)
        similar_dicts = [
            {"name": r.artifact.name, "content": r.version.content if r.version else ""}
            for r in search_results[:2]
        ]

        last_script: str | None = None
        last_validation: ValidationResult | None = None
        final_response: LuaGenerationResponse | None = None

        async def attempt_fn(attempt: int) -> bool:
            nonlocal last_script, last_validation, final_response

            if attempt == 1 or last_script is None:
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
                repair_resp, stats = await self.llm.repair_lua(
                    original_goal=req.intent,
                    current_script=last_script,
                    validation_errors=last_validation.errors if last_validation else [],
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
                    "generation.llm_call", "generation_job", job.generation_id,
                    payload={"attempt": attempt, "tokens": stats.total_tokens, "model": stats.model},
                )

            validation = await self.validator.run(
                content=script,
                artifact_type="lua_script",
                constraints=constraints,
                test_cases=(req.context or {}).get("test_cases"),
                profile=profile,
            )

            await self._record_attempt(job.generation_id, attempt, script, validation)
            log.info(
                "generation attempt %d/%d valid=%s",
                attempt, self.settings.generation_max_attempts, validation.valid,
                extra={"job_id": job.generation_id, "attempt": attempt, "valid": validation.valid,
                       "errors": validation.errors[:3]},
            )

            last_script = script
            last_validation = validation
            return validation.valid

        try:
            await run_repair_loop(
                artifact_type="lua_script",
                max_attempts=self.settings.generation_max_attempts,
                attempt_fn=attempt_fn,
            )

            # 5. Atualiza job
            job.status = "completed" if last_validation and last_validation.valid else "failed"
            job.finished_at = datetime.now(UTC)
            job.attempts = self.settings.generation_max_attempts
            log.info(
                "generation job finished status=%s", job.status,
                extra={"job_id": job.generation_id, "status": job.status},
            )

            # 6. Persiste se solicitado e válido
            artifact_orm = None
            artifact_version_orm = None
            if req.save and last_validation and last_validation.valid and final_response:
                artifact_orm, artifact_version_orm = await self.registry.create_artifact(
                    name=final_response.name,
                    artifact_type="lua_script",
                    content=last_script,
                    description=final_response.description,
                    tags=final_response.tags,
                    risk_level=final_response.risk_assessment,
                    generated_by_plan_id=plan_id,
                    metadata={"runtime_profile": profile},
                )
                job.final_artifact_id = artifact_orm.artifact_id
                await self.db.flush()

                if self.audit:
                    await self.audit.log_event(
                        "artifact.created", "artifact", artifact_orm.artifact_id,
                        payload={"goal": req.intent, "generation_id": job.generation_id},
                    )
        except Exception:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            log.error("generation job failed with exception", extra={"job_id": job.generation_id}, exc_info=True)
            raise
        finally:
            await self.db.commit()

        # 7. Monta resposta (mesmo sem salvar, retorna o artifact transitório)
        if artifact_orm and artifact_version_orm:
            artifact_schema = _build_artifact(artifact_orm, artifact_version_orm)
        else:
            artifact_schema = self._make_transient_artifact(
                script=last_script or "",
                gen_response=final_response,
                validation=last_validation,
                plan_id=plan_id,
            )

        return GeneratedArtifactResponse(
            artifact=artifact_schema,
            validation=last_validation,
            reused=False,
        )

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    async def _create_job(self, req: GenerateScriptRequest, plan_id: str | None) -> GenerationJob:
        job = GenerationJob(
            generation_id=generate_id("gen"),
            goal=req.intent,
            target_type="lua_script",
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
        script: str,
        gen_response: LuaGenerationResponse | None,
        validation: ValidationResult | None,
        plan_id: str | None,
    ) -> Artifact:
        now = datetime.now(UTC)
        return Artifact(
            artifact_id=generate_id("art"),
            artifact_type=ArtifactType.lua_script,
            name=gen_response.name if gen_response else "generated_script",
            version=1,
            status=ArtifactStatus.draft,
            content=script,
            description=gen_response.description if gen_response else None,
            tags=gen_response.tags if gen_response else [],
            risk_level=gen_response.risk_assessment if gen_response else "low",
            generated_by_plan_id=plan_id,
            validation=validation,
            metadata={"runtime_profile": gen_response.runtime_profile} if gen_response else None,
            created_at=now,
            updated_at=now,
        )
