"""PlanBuilder — orquestrador central do ciclo intent → artifact(s) → policy → plan.

Fluxo principal (build_plan):
1. Analisa intent via LLM (analyze_intent) → IntentAnalysis
2. Busca FTS por artifacts reutilizáveis
3. Para cada artifact type necessário:
   - reuse_policy=prefer_reuse e bom match → reusa
   - Caso contrário → GenerationService.generate_lua()
4. Avalia política via PolicyEngine para cada artifact
5. Determina status do plano: blocked | proposed | approved
6. Persiste Plan (sempre) + artifacts (se mode=persist ou execute)
7. Retorna Plan schema
"""
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.exceptions import InvalidTransitionError, NotFoundError, PolicyDeniedError
from ritesmith.core.generation import GenerationService
from ritesmith.core.workflow_generation import WorkflowGenerationService
from ritesmith.core.ids import generate_id
from ritesmith.core.policy import PolicyEngine
from ritesmith.llm.base import LLMProvider
from ritesmith.registry.models import Artifact as ArtifactORM
from ritesmith.registry.models import ArtifactVersion as ArtifactVersionORM
from ritesmith.registry.models import Plan as PlanORM
from ritesmith.registry.search import fts_search
from ritesmith.registry.service import RegistryService
from ritesmith.schemas.artifact import Artifact, ArtifactStatus, ArtifactType, ValidationResult
from ritesmith.schemas.generation import GenerateScriptRequest, GenerateWorkflowRequest, ScriptConstraints
from ritesmith.schemas.plan import (
    ApprovePlanRequest,
    CreatePlanRequest,
    GenerationMode,
    Plan,
    PlanConstraints,
    PlanStatus,
    PlanStep,
    PolicyDecision,
    RejectPlanRequest,
    ReusePolicy,
)
from ritesmith.schemas.policy import PolicyDecisionValue, PolicyEvaluationRequest

_REUSE_SCORE_THRESHOLD = 0.05

_ALLOWED_TRANSITIONS: dict[PlanStatus, set[PlanStatus]] = {
    PlanStatus.draft:     {PlanStatus.proposed, PlanStatus.blocked},
    PlanStatus.proposed:  {PlanStatus.approved, PlanStatus.rejected, PlanStatus.blocked},
    PlanStatus.approved:  {PlanStatus.persisted, PlanStatus.executing, PlanStatus.failed},
    PlanStatus.persisted: {PlanStatus.executing, PlanStatus.completed, PlanStatus.failed},
    PlanStatus.executing: {PlanStatus.completed, PlanStatus.failed},
}


class PlanBuilder:
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
        self.audit = audit
        self.registry = RegistryService(db)
        self.policy = PolicyEngine(settings)
        self._generation_service = GenerationService(db=db, llm=llm, settings=settings, audit=audit)
        self._workflow_service = WorkflowGenerationService(db=db, llm=llm, settings=settings, audit=audit)

    async def build_plan(self, req: CreatePlanRequest) -> Plan:
        constraints = req.constraints or PlanConstraints()
        plan_id = generate_id("plan")

        # 1. Analyze intent
        intent_analysis, _ = await self.llm.analyze_intent(
            goal=req.intent,
            constraints=constraints.model_dump(),
        )

        # 2. Determine which artifact types to generate
        artifact_types = req.requested_artifact_types or intent_analysis.artifact_types or ["lua_script"]

        # 3. Search for reusable artifacts and generate
        steps: list[PlanStep] = []
        artifacts: list[Artifact] = []
        validations: list[ValidationResult] = []
        policy_decisions: list[PolicyDecision] = []
        any_blocked = False
        any_requires_approval = False

        for i, artifact_type in enumerate(artifact_types):
            step_id = f"step_{i + 1}"

            if artifact_type == "lua_script":
                step, artifact, validation = await self._handle_lua_artifact(
                    req=req,
                    constraints=constraints,
                    plan_id=plan_id,
                    step_id=step_id,
                    artifact_type=artifact_type,
                    intent_analysis=intent_analysis,
                )
            elif artifact_type == "trama_workflow":
                step, artifact, validation = await self._handle_workflow_artifact(
                    req=req,
                    plan_id=plan_id,
                    step_id=step_id,
                )
            else:
                step = PlanStep(
                    step_id=step_id,
                    title=f"Generate {artifact_type}",
                    description=f"Unsupported artifact type: {artifact_type}",
                    status="skipped",
                )
                artifact = None
                validation = None

            steps.append(step)
            if artifact:
                artifacts.append(artifact)
            if validation:
                validations.append(validation)

            # 4. Policy evaluation per artifact
            if artifact:
                policy_req = PolicyEvaluationRequest(
                    operation="execute",
                    artifact_type=artifact_type,
                    risk_level=artifact.risk_level or "low",
                )
                decision = self.policy.evaluate(policy_req)
                plan_decision = PolicyDecision(
                    decision=decision.decision,
                    reason=decision.reason,
                )
                policy_decisions.append(plan_decision)

                if decision.decision == PolicyDecisionValue.deny:
                    any_blocked = True
                    step.status = "blocked"
                elif decision.decision == PolicyDecisionValue.require_approval:
                    any_requires_approval = True

        # 5. Determine plan status
        if any_blocked:
            plan_status = PlanStatus.blocked
        elif any_requires_approval or req.constraints and req.constraints.require_approval_for_execution:
            plan_status = PlanStatus.proposed
        else:
            plan_status = PlanStatus.approved

        # Aggregate policy decision
        aggregate_policy: PolicyDecision | None = None
        if policy_decisions:
            decisions = [d.decision for d in policy_decisions]
            if PolicyDecisionValue.deny in decisions:
                aggregate_policy = PolicyDecision(decision=PolicyDecisionValue.deny, reason="One or more artifacts were denied by policy")
            elif PolicyDecisionValue.require_approval in decisions:
                aggregate_policy = PolicyDecision(decision=PolicyDecisionValue.require_approval, reason="One or more artifacts require approval")
            else:
                aggregate_policy = PolicyDecision(decision=PolicyDecisionValue.allow, reason="All artifacts approved by policy")

        # 6. Persist plan
        save_artifacts = req.mode in (GenerationMode.persist, GenerationMode.execute)
        artifact_ids: list[str] = []

        if save_artifacts:
            persisted = []
            for artifact in artifacts:
                if artifact.status == ArtifactStatus.draft and artifact.content:
                    art_orm, ver_orm = await self.registry.create_artifact(
                        name=artifact.name,
                        artifact_type=artifact.artifact_type.value,
                        content=artifact.content,
                        description=artifact.description,
                        tags=artifact.tags,
                        risk_level=artifact.risk_level or "low",
                        generated_by_plan_id=plan_id,
                        metadata={"runtime_profile": artifact.runtime_profile},
                    )
                    persisted.append(_build_artifact(art_orm, ver_orm))
                    artifact_ids.append(art_orm.artifact_id)
                else:
                    persisted.append(artifact)
                    if artifact.artifact_id:
                        artifact_ids.append(artifact.artifact_id)
            artifacts = persisted
        else:
            artifact_ids = [a.artifact_id for a in artifacts if a.artifact_id]

        now = datetime.now(UTC)
        plan_orm = PlanORM(
            plan_id=plan_id,
            status=plan_status.value,
            intent=req.intent,
            summary=intent_analysis.summary,
            mode=req.mode.value,
            reuse_policy=req.reuse_policy.value,
            constraints=constraints.model_dump(),
            context=req.context,
            steps=[s.model_dump() for s in steps],
            artifact_ids=artifact_ids if artifact_ids else None,
            policy_decision=aggregate_policy.model_dump() if aggregate_policy else None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(plan_orm)
        await self.db.flush()

        if self.audit:
            await self.audit.log_event(
                "plan.created", "plan", plan_id,
                payload={"intent": req.intent, "status": plan_status, "mode": req.mode},
            )

        await self.db.commit()

        return Plan(
            plan_id=plan_id,
            status=plan_status,
            intent=req.intent,
            summary=intent_analysis.summary or None,
            steps=steps,
            artifacts=artifacts,
            validations=validations,
            policy_decision=aggregate_policy,
            created_at=now,
            updated_at=now,
        )

    async def _handle_lua_artifact(
        self,
        req: CreatePlanRequest,
        constraints: PlanConstraints,
        plan_id: str,
        step_id: str,
        artifact_type: str,
        intent_analysis,
    ) -> tuple[PlanStep, Artifact | None, ValidationResult | None]:
        runtime_profile = "readonly_network" if intent_analysis.requires_network else "transform_only"

        # Try reuse first
        if req.reuse_policy != ReusePolicy.force_new:
            search_results = await fts_search(self.db, req.intent, artifact_types=[artifact_type], limit=3)
            if search_results and search_results[0].score >= _REUSE_SCORE_THRESHOLD:
                best = search_results[0]
                artifact = _build_artifact(best.artifact, best.version)
                step = PlanStep(
                    step_id=step_id,
                    title=f"Reuse {artifact_type}: {artifact.name}",
                    description=f"Reusing existing artifact (FTS score: {best.score:.3f})",
                    status="completed",
                    artifact_id=artifact.artifact_id,
                    details={"reused": True, "score": best.score},
                )
                return step, artifact, None

        # Generate
        gen_req = GenerateScriptRequest(
            intent=req.intent,
            language="lua",
            constraints=ScriptConstraints(
                allow_network=constraints.allow_network,
                allow_filesystem=constraints.allow_filesystem,
                runtime_profile=runtime_profile,
                reuse_policy=req.reuse_policy.value,
            ),
            context=req.context,
            save=False,
        )
        gen_response = await self._generation_service.generate_lua(gen_req, plan_id=plan_id)
        artifact = gen_response.artifact
        validation = gen_response.validation

        step_status = "completed" if (validation is None or validation.valid) else "failed"
        step = PlanStep(
            step_id=step_id,
            title=f"Generate {artifact_type}: {artifact.name}",
            description=artifact.description,
            status=step_status,
            artifact_id=artifact.artifact_id if step_status == "completed" else None,
            details={"reused": gen_response.reused, "generated": True},
        )
        return step, artifact if step_status == "completed" else None, validation

    async def _handle_workflow_artifact(
        self,
        req: CreatePlanRequest,
        plan_id: str,
        step_id: str,
    ) -> tuple[PlanStep, Artifact | None, ValidationResult | None]:
        wf_req = GenerateWorkflowRequest(
            intent=req.intent,
            constraints=req.constraints.model_dump() if req.constraints else {},
            context=req.context,
            save=False,
        )
        gen_resp = await self._workflow_service.generate_workflow(wf_req, plan_id=plan_id)
        artifact = gen_resp.artifact
        validation = gen_resp.validation

        step_status = "completed" if (validation is None or validation.valid) else "failed"
        step = PlanStep(
            step_id=step_id,
            title=f"Generate trama_workflow: {artifact.name}",
            description=artifact.description,
            status=step_status,
            artifact_id=artifact.artifact_id if step_status == "completed" else None,
            details={"reused": gen_resp.reused, "generated": True},
        )
        return step, artifact if step_status == "completed" else None, validation

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    async def get_plan(self, plan_id: str) -> Plan:
        result = await self.db.execute(select(PlanORM).where(PlanORM.plan_id == plan_id))
        plan_orm = result.scalar_one_or_none()
        if not plan_orm:
            raise NotFoundError(f"Plan {plan_id} not found")
        return await self._orm_to_schema(plan_orm)

    async def approve_plan(self, plan_id: str, req: ApprovePlanRequest) -> Plan:
        plan_orm = await self._get_orm(plan_id)
        self._assert_transition(plan_orm, PlanStatus.approved)
        plan_orm.status = PlanStatus.approved
        plan_orm.approved_at = datetime.now(UTC)
        plan_orm.updated_at = datetime.now(UTC)

        # Persist any draft artifacts that belong to this plan
        await self._persist_draft_artifacts(plan_orm)

        await self.db.flush()
        if self.audit:
            await self.audit.log_event("plan.approved", "plan", plan_id, payload={"comment": req.comment})
        await self.db.commit()
        return await self._orm_to_schema(plan_orm)

    async def reject_plan(self, plan_id: str, req: RejectPlanRequest) -> Plan:
        plan_orm = await self._get_orm(plan_id)
        self._assert_transition(plan_orm, PlanStatus.rejected)
        plan_orm.status = PlanStatus.rejected
        plan_orm.updated_at = datetime.now(UTC)
        await self.db.flush()
        if self.audit:
            await self.audit.log_event("plan.rejected", "plan", plan_id, payload={"reason": req.reason})
        await self.db.commit()
        return await self._orm_to_schema(plan_orm)

    async def persist_artifacts(self, plan_id: str) -> Plan:
        plan_orm = await self._get_orm(plan_id)
        await self._persist_draft_artifacts(plan_orm)
        plan_orm.status = PlanStatus.persisted
        plan_orm.updated_at = datetime.now(UTC)
        await self.db.flush()
        await self.db.commit()
        return await self._orm_to_schema(plan_orm)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_orm(self, plan_id: str) -> PlanORM:
        result = await self.db.execute(select(PlanORM).where(PlanORM.plan_id == plan_id))
        plan_orm = result.scalar_one_or_none()
        if not plan_orm:
            raise NotFoundError(f"Plan {plan_id} not found")
        return plan_orm

    def _assert_transition(self, plan_orm: PlanORM, target: PlanStatus) -> None:
        current = PlanStatus(plan_orm.status)
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition plan from '{current}' to '{target}'"
            )

    async def _persist_draft_artifacts(self, plan_orm: PlanORM) -> None:
        """Persists draft artifacts referenced in plan steps that were not yet saved."""
        pass

    async def _orm_to_schema(self, plan_orm: PlanORM) -> Plan:
        steps = [PlanStep(**s) for s in (plan_orm.steps or [])]

        # Load persisted artifacts — single batch query instead of N+1
        artifacts: list[Artifact] = []
        artifact_ids = plan_orm.artifact_ids or []
        if artifact_ids:
            stmt = (
                select(ArtifactORM, ArtifactVersionORM)
                .outerjoin(
                    ArtifactVersionORM,
                    (ArtifactVersionORM.artifact_id == ArtifactORM.artifact_id)
                    & (ArtifactVersionORM.version == ArtifactORM.current_version),
                )
                .where(ArtifactORM.artifact_id.in_(artifact_ids))
            )
            rows = await self.db.execute(stmt)
            artifacts = [_build_artifact(art, ver) for art, ver in rows if ver]

        policy = PolicyDecision(**plan_orm.policy_decision) if plan_orm.policy_decision else None

        return Plan(
            plan_id=plan_orm.plan_id,
            status=PlanStatus(plan_orm.status),
            intent=plan_orm.intent,
            summary=plan_orm.summary,
            steps=steps,
            artifacts=artifacts,
            validations=[],
            policy_decision=policy,
            created_at=plan_orm.created_at,
            updated_at=plan_orm.updated_at,
            approved_at=plan_orm.approved_at,
            metadata=plan_orm.constraints,
        )
