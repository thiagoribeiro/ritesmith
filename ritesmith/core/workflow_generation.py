"""WorkflowGenerationService — gera Trama workflow definitions via LLM + repair loop.

Fluxo:
1. Lista capabilities disponíveis no registry
2. Busca FTS por workflows similares (artifacts tipo trama_workflow)
3. Gera workflow via LLM.generate_workflow()
4. Valida com WorkflowValidator (Layer 1 + Layer 2)
5. Se inválido → LLM.repair_workflow() e tenta de novo (até max_attempts)
6. Persiste se save=True e válido
7. Retorna GeneratedArtifactResponse
"""
import json
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.ids import generate_id
from ritesmith.core.repair import run_repair_loop
from ritesmith.core.reuse import check_reuse
from ritesmith.llm.base import LLMProvider, WorkflowGenerationResponse
from ritesmith.registry.search import fts_search
from ritesmith.registry.service import RegistryService
from ritesmith.schemas.artifact import Artifact, ArtifactStatus, ArtifactType, ValidationResult
from ritesmith.schemas.generation import GeneratedArtifactResponse, GenerateWorkflowRequest
from ritesmith.workflows.validator import WorkflowValidator

log = logging.getLogger(__name__)


class WorkflowGenerationService:
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
        self.registry = RegistryService(db)
        self.audit = audit

    async def generate_workflow(
        self,
        req: GenerateWorkflowRequest,
        plan_id: str | None = None,
    ) -> GeneratedArtifactResponse:
        constraints = req.constraints or {}
        reuse_policy = constraints.get("reuse_policy", "prefer_reuse")

        # 1. Reuse check
        if reuse := await check_reuse(self.db, req.intent, ["trama_workflow"], reuse_policy, self.audit):
            return reuse

        # 2. Collect available capabilities — provider caps (runtime) + registered Lua artifacts (DB)
        from ritesmith.runtime.host_functions import get_available_provider_capabilities
        provider_caps = get_available_provider_capabilities()
        db_caps = await self._get_capabilities()

        # Combined: provider caps first (they're the primary ones), then DB artifacts
        all_caps_for_prompt = provider_caps + db_caps

        # Registry keyed by capability_name for the validator
        cap_registry: dict[str, dict] = {c["capability_name"]: c for c in provider_caps}
        for c in db_caps:
            key = c.get("capability_name") or c.get("capability_id", "")
            if key:
                cap_registry[key] = c

        # 3. Similar workflows for few-shot examples — include full content like Lua generation does
        similar = await fts_search(self.db, req.intent, artifact_types=["trama_workflow"], limit=2)
        similar_dicts = [
            {
                "name": r.artifact.name,
                "description": r.artifact.description or "",
                "content": r.version.content if r.version else None,
            }
            for r in similar
        ]

        validator = WorkflowValidator(cap_registry)

        last_definition: dict | None = None
        last_errors: list[str] = []
        final_response: WorkflowGenerationResponse | None = None

        async def attempt_fn(attempt: int) -> bool:
            nonlocal last_definition, last_errors, final_response

            if attempt == 1 or last_definition is None:
                base_url = self.settings.public_url or "http://ritesmith:8081"
                llm_resp, stats = await self.llm.generate_workflow(
                    goal=req.intent,
                    available_capabilities=all_caps_for_prompt,
                    constraints=constraints,
                    similar_workflows=similar_dicts,
                    ritesmith_base_url=base_url,
                    context=req.context,
                )
                definition = llm_resp.definition
            else:
                repair_resp, stats = await self.llm.repair_workflow(
                    original_goal=req.intent,
                    current_definition=last_definition,
                    validation_errors=last_errors,
                    attempt_number=attempt,
                    available_capability_names=sorted(cap_registry.keys()),
                )
                definition = repair_resp.definition
                llm_resp = WorkflowGenerationResponse(
                    definition=definition,
                    name=final_response.name if final_response else req.intent[:40],
                    description=final_response.description if final_response else req.intent,
                    required_capabilities=final_response.required_capabilities if final_response else [],
                )

            final_response = llm_resp

            if self.audit:
                await self.audit.log_event(
                    "workflow_generation.llm_call", "workflow_generation", plan_id or "none",
                    payload={"attempt": attempt, "tokens": stats.total_tokens},
                )

            errors = validator.validate(definition)
            last_definition = definition
            last_errors = errors
            log.info(
                "workflow generation attempt %d valid=%s",
                attempt, not errors,
                extra={"attempt": attempt, "valid": not errors, "errors": errors[:3]},
            )
            return not errors

        artifact_orm = None
        artifact_version_orm = None
        validation = ValidationResult(valid=False, errors=[], warnings=[], checks=[])
        try:
            await run_repair_loop(
                artifact_type="trama_workflow",
                max_attempts=self.settings.generation_max_attempts,
                attempt_fn=attempt_fn,
            )

            # 5. Build ValidationResult
            valid = not last_errors
            validation = ValidationResult(
                valid=valid,
                errors=last_errors,
                warnings=[],
                checks=[],
            )

            # 6. Persist if requested and valid
            if req.save and valid and final_response:
                artifact_orm, artifact_version_orm = await self.registry.create_artifact(
                    name=final_response.name,
                    artifact_type="trama_workflow",
                    content=json.dumps(last_definition),
                    description=final_response.description,
                    tags=["workflow", "trama"],
                    risk_level="low",
                    generated_by_plan_id=plan_id,
                )
                await self.db.commit()

                if self.audit:
                    await self.audit.log_event(
                        "artifact.created", "artifact", artifact_orm.artifact_id,
                        payload={"goal": req.intent},
                    )
        except Exception:
            await self.db.rollback()
            log.error("workflow generation failed with exception", exc_info=True)
            raise
        else:
            if not req.save:
                await self.db.rollback()

        # 7. Build response
        if artifact_orm and artifact_version_orm:
            artifact_schema = _build_artifact(artifact_orm, artifact_version_orm)
        else:
            now = datetime.now(UTC)
            artifact_schema = Artifact(
                artifact_id=generate_id("art"),
                artifact_type=ArtifactType.trama_workflow,
                name=final_response.name if final_response else "generated_workflow",
                version=1,
                status=ArtifactStatus.draft,
                content=json.dumps(last_definition) if last_definition else "{}",
                description=final_response.description if final_response else None,
                tags=["workflow", "trama"],
                generated_by_plan_id=plan_id,
                validation=validation,
                created_at=now,
                updated_at=now,
            )

        return GeneratedArtifactResponse(
            artifact=artifact_schema,
            validation=validation,
            reused=False,
        )

    async def _get_capabilities(self) -> list[dict]:
        caps = await self.registry.list_capabilities(limit=50, status="available")
        return [
            {
                "capability_id": c.capability_id,
                "name": c.name,
                "description": c.description,
                "input_schema": c.input_schema,
                "output_schema": c.output_schema,
            }
            for c in caps
        ]
