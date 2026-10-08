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

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.exceptions import InvalidTransitionError, NotFoundError
from ritesmith.core.generation import GenerationService
from ritesmith.core.generation_budget import bounded_generation
from ritesmith.core.ids import generate_id
from ritesmith.core.policy import PolicyEngine
from ritesmith.core.proposal import prepare_proposal
from ritesmith.core.workflow_generation import WorkflowGenerationService
from ritesmith.llm.base import IntentAnalysis, LLMProvider
from ritesmith.observability.generation import current_trace, stage
from ritesmith.registry.models import Artifact as ArtifactORM
from ritesmith.registry.models import ArtifactVersion as ArtifactVersionORM
from ritesmith.registry.models import Execution as ExecutionORM
from ritesmith.registry.models import Plan as PlanORM
from ritesmith.registry.service import RegistryService
from ritesmith.runtime.luau import effective_script_language, script_artifact_type
from ritesmith.schemas.artifact import Artifact, ArtifactStatus, ValidationResult
from ritesmith.schemas.generation import (
    GenerateScriptRequest,
    GenerateWorkflowRequest,
    ScriptConstraints,
)
from ritesmith.schemas.plan import (
    ApprovePlanRequest,
    CompletePlanRequest,
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

_SCRIPT_TYPES = ("lua_script", "luau_script")

_PRIVATE_URL_PREFIXES = (
    "http://localhost",
    "https://localhost",
    "http://127.",
    "https://127.",
    "http://10.",
    "https://10.",
    "http://172.16.",
    "https://172.16.",
    "http://192.168.",
    "https://192.168.",
    "http://0.0.0.0",
    "https://0.0.0.0",
    "http://[::1]",
    "https://[::1]",
)


def _is_safe_callback_url(url: str, allowed_hosts: Iterable[str] = ()) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    if allowed_hosts and urlparse(url).hostname in set(allowed_hosts):
        return True
    return not any(url.startswith(p) for p in _PRIVATE_URL_PREFIXES)


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    return tuple(h.strip() for h in raw.split(",") if h.strip())


_CALLBACK_ATTEMPTS = 3


async def _fire_callback(
    callback_url: str,
    plan_id: str,
    status: str,
    summary: str | None,
    allowed_hosts: Iterable[str] = (),
    extra: dict | None = None,
) -> None:
    import asyncio
    import logging

    import httpx

    log = logging.getLogger(__name__)
    if not _is_safe_callback_url(callback_url, allowed_hosts):
        log.warning("plan.callback blocked unsafe url plan=%s url=%r", plan_id, callback_url)
        return
    body = {"plan_id": plan_id, "status": status, "summary": summary or ""}
    if extra:
        body.update({k: v for k, v in extra.items() if k not in body})
    for attempt in range(1, _CALLBACK_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(callback_url, json=body)
            if resp.status_code < 500:
                log.info(
                    "plan.callback fired plan=%s status=%s http=%s",
                    plan_id,
                    status,
                    resp.status_code,
                )
                return
            log.warning(
                "plan.callback http=%s plan=%s attempt=%d", resp.status_code, plan_id, attempt
            )
        except Exception as exc:
            log.warning("plan.callback failed plan=%s attempt=%d: %s", plan_id, attempt, exc)
        if attempt < _CALLBACK_ATTEMPTS:
            await asyncio.sleep(2**attempt)


_ALLOWED_TRANSITIONS: dict[PlanStatus, set[PlanStatus]] = {
    PlanStatus.draft: {PlanStatus.proposed, PlanStatus.blocked},
    PlanStatus.proposed: {
        PlanStatus.approved,
        PlanStatus.rejected,
        PlanStatus.blocked,
        PlanStatus.cancelled,
    },
    PlanStatus.approved: {
        PlanStatus.persisted,
        PlanStatus.executing,
        PlanStatus.completed,
        PlanStatus.failed,
        PlanStatus.cancelled,
    },
    PlanStatus.persisted: {
        PlanStatus.executing,
        PlanStatus.completed,
        PlanStatus.failed,
        PlanStatus.cancelled,
    },
    PlanStatus.executing: {PlanStatus.completed, PlanStatus.failed, PlanStatus.cancelled},
    PlanStatus.failed: {PlanStatus.superseded},
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
        self._workflow_service = WorkflowGenerationService(
            db=db, llm=llm, settings=settings, audit=audit
        )

    @bounded_generation
    async def build_plan(self, req: CreatePlanRequest) -> Plan:
        constraints = req.constraints or PlanConstraints()
        plan_id = generate_id("plan")

        proposal = None
        recall = None
        reusable = None
        if (
            req.requested_artifact_types
            or "presence_reminder" in (req.context or {})
            or (
                self.settings.generation_semantic_workflows
                and (req.context or {}).get("workflow_continuation", {}).get("intent") == req.intent
            )
        ):
            explicit_types = req.requested_artifact_types or ["trama_workflow"]
            intent_analysis = IntentAnalysis(
                artifact_types=explicit_types,
                requires_workflow="trama_workflow" in explicit_types,
                requires_lua=any(t in _SCRIPT_TYPES for t in explicit_types),
                requires_network=constraints.allow_network,
                summary=req.intent,
            )
        else:
            profile = (
                "trusted_internal"
                if self.settings.casp_providers
                else "readonly_network"
                if constraints.allow_network
                else "transform_only"
            )
            proposal, recall, reusable = await prepare_proposal(
                self.db,
                self.llm,
                self.settings,
                goal=req.intent,
                constraints={**constraints.model_dump(), "reuse_policy": req.reuse_policy.value},
                context=req.context,
                profile=profile,
                audit=self.audit,
            )
            if reusable is not None:
                kind = reusable.artifact.artifact_type.value
                intent_analysis = IntentAnalysis(
                    artifact_types=[kind],
                    requires_workflow=kind == "trama_workflow",
                    requires_lua=kind in _SCRIPT_TYPES,
                    summary=req.intent,
                )
            elif proposal is not None:
                initial_proposal, proposal_stats = proposal
                intent_analysis = initial_proposal.analysis
            else:
                with stage("intent", "mixed"):
                    intent_analysis, _ = await self.llm.analyze_intent(
                        goal=req.intent, constraints=constraints.model_dump(), context=req.context
                    )

        # 2. Determine which artifact types to generate. "lua_script" from the intent
        # analysis means "a script": it is generated in the configured language.
        artifact_types = (
            req.requested_artifact_types or intent_analysis.artifact_types or ["lua_script"]
        )
        if not req.requested_artifact_types:
            script_type = script_artifact_type(effective_script_language(self.settings))
            artifact_types = [script_type if t in _SCRIPT_TYPES else t for t in artifact_types]
        # A workflow can reference a script generated by the same proposal.
        artifact_types = sorted(dict.fromkeys(artifact_types), key=lambda t: t not in _SCRIPT_TYPES)

        trace = current_trace.get()
        if trace is not None:
            trace.has_dependencies = len(artifact_types) > 1
        # 3. Search for reusable artifacts and generate
        steps: list[PlanStep] = []
        artifacts: list[Artifact] = []
        validations: list[ValidationResult] = []
        policy_decisions: list[PolicyDecision] = []
        any_blocked = False
        any_requires_approval = False

        for i, artifact_type in enumerate(artifact_types):
            step_id = f"step_{i + 1}"

            if artifact_type in _SCRIPT_TYPES:
                step, artifact, validation = await self._handle_lua_artifact(
                    req=req,
                    constraints=constraints,
                    plan_id=plan_id,
                    step_id=step_id,
                    artifact_type=artifact_type,
                    intent_analysis=intent_analysis,
                    initial=(initial_proposal.script, proposal_stats)
                    if proposal is not None and initial_proposal.script is not None
                    else None,
                    recall=recall,
                    pre_reused=reusable,
                    initial_tests=(initial_proposal._test_cases, initial_proposal._tests_attempted)
                    if proposal is not None
                    else None,
                )
            elif artifact_type == "trama_workflow":
                step, artifact, validation = await self._handle_workflow_artifact(
                    req=req,
                    plan_id=plan_id,
                    step_id=step_id,
                    initial=(initial_proposal.workflow, proposal_stats)
                    if proposal is not None and initial_proposal.workflow is not None
                    else None,
                    recall=recall,
                    pre_reused=reusable,
                    available_scripts=[
                        a for a in artifacts if a.artifact_type.value in _SCRIPT_TYPES
                    ],
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
            if step.status == "failed":
                any_blocked = True
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
        elif (
            any_requires_approval
            or req.constraints
            and req.constraints.require_approval_for_execution
        ):
            plan_status = PlanStatus.proposed
        else:
            plan_status = PlanStatus.approved

        # Aggregate policy decision
        aggregate_policy: PolicyDecision | None = None
        if policy_decisions:
            decisions = [d.decision for d in policy_decisions]
            if PolicyDecisionValue.deny in decisions:
                aggregate_policy = PolicyDecision(
                    decision=PolicyDecisionValue.deny,
                    reason="One or more artifacts were denied by policy",
                )
            elif PolicyDecisionValue.require_approval in decisions:
                aggregate_policy = PolicyDecision(
                    decision=PolicyDecisionValue.require_approval,
                    reason="One or more artifacts require approval",
                )
            else:
                aggregate_policy = PolicyDecision(
                    decision=PolicyDecisionValue.allow, reason="All artifacts approved by policy"
                )

        # 6. Persist plan
        save_artifacts = req.mode in (GenerationMode.persist, GenerationMode.execute)
        artifact_ids: list[str] = []

        if save_artifacts:
            persisted = []
            persisted_ids = {}
            for artifact in artifacts:
                if artifact.artifact_type.value == "trama_workflow" and persisted_ids:
                    definition = json.loads(artifact.content)

                    def bind_persisted(value):
                        if isinstance(value, list):
                            for item in value:
                                bind_persisted(item)
                        elif isinstance(value, dict):
                            old = value.get("artifact_id")
                            if old in persisted_ids:
                                value["artifact_id"] = persisted_ids[old]
                            for item in value.values():
                                bind_persisted(item)

                    bind_persisted(definition)
                    for node in definition.get("nodes", []):
                        body = node.get("action", {}).get("request", {}).get("body", {})
                        if isinstance(body, dict) and body.get("artifact_id") in persisted_ids:
                            body["artifact_id"] = persisted_ids[body["artifact_id"]]
                    artifact.content = json.dumps(definition)

                if artifact.status == ArtifactStatus.draft and artifact.content:
                    art_orm, ver_orm = await self.registry.create_artifact(
                        name=artifact.name,
                        artifact_type=artifact.artifact_type.value,
                        content=artifact.content,
                        description=artifact.description,
                        tags=artifact.tags,
                        input_schema=artifact._input_schema,
                        output_schema=artifact._output_schema,
                        risk_level=artifact.risk_level or "low",
                        generated_by_plan_id=plan_id,
                        metadata={
                            **(artifact.metadata or {}),
                            "runtime_profile": artifact.runtime_profile,
                        },
                    )
                    persisted_ids[artifact.artifact_id] = art_orm.artifact_id
                    persisted.append(_build_artifact(art_orm, ver_orm))
                    artifact_ids.append(art_orm.artifact_id)
                else:
                    persisted.append(artifact)
                    if artifact.artifact_id:
                        artifact_ids.append(artifact.artifact_id)
            for step in steps:
                step.artifact_id = persisted_ids.get(step.artifact_id, step.artifact_id)
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
            callback_url=req.callback_url,
            created_at=now,
            updated_at=now,
        )
        self.db.add(plan_orm)
        await self.db.flush()

        if self.audit:
            await self.audit.log_event(
                "plan.created",
                "plan",
                plan_id,
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
        initial=None,
        recall=None,
        pre_reused=None,
        initial_tests=None,
    ) -> tuple[PlanStep, Artifact | None, ValidationResult | None]:
        explicit_profile = (constraints.model_extra or {}).get("runtime_profile")
        if explicit_profile:
            runtime_profile = explicit_profile
        elif self.settings.casp_providers:
            # CASP configured: always trusted_internal so casp.* host functions are available.
            # The LLM uses them only when the intent involves device control.
            runtime_profile = "trusted_internal"
        elif intent_analysis.requires_network:
            runtime_profile = "readonly_network"
        else:
            runtime_profile = "transform_only"

        # Generate
        gen_req = GenerateScriptRequest(
            intent=req.intent,
            language="luau" if artifact_type == "luau_script" else "lua",
            constraints=ScriptConstraints(
                **{
                    **constraints.model_dump(),
                    "runtime_profile": runtime_profile,
                    "reuse_policy": req.reuse_policy.value,
                }
            ),
            context=req.context,
            save=False,
        )
        gen_response = await self._generation_service.generate_lua(
            gen_req,
            plan_id=plan_id,
            initial=initial,
            recall=recall,
            pre_reused=pre_reused,
            initial_tests=initial_tests,
            reuse_checked=initial is not None,
        )
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
        initial=None,
        recall=None,
        pre_reused=None,
        available_scripts=None,
    ) -> tuple[PlanStep, Artifact | None, ValidationResult | None]:
        scripts = available_scripts or []
        workflow_context = {**(req.context or {})}
        if scripts:
            workflow_context["available_lua_artifacts"] = [
                {
                    "artifact_id": a.artifact_id,
                    "name": a.name,
                    "input_schema": a._input_schema,
                    "output_schema": a._output_schema,
                }
                for a in scripts
            ]
            if initial is not None:
                candidate, stats = initial
                candidate = candidate.model_copy(deep=True)
                by_name = {a.name: a.artifact_id for a in scripts}

                def bind_semantic(value):
                    if isinstance(value, list):
                        for item in value:
                            bind_semantic(item)
                    elif isinstance(value, dict):
                        name = value.get("capability_name")
                        if name in by_name:
                            value["artifact_id"] = by_name[name]
                            value.pop("capability_name")
                        for item in value.values():
                            bind_semantic(item)

                bind_semantic(candidate.definition)
                for node in candidate.definition.get("nodes", []):
                    body = node.get("action", {}).get("request", {}).get("body", {})
                    if isinstance(body, dict) and body.get("capability_name") in by_name:
                        body["artifact_id"] = by_name[body.pop("capability_name")]
                initial = (candidate, stats)
        wf_req = GenerateWorkflowRequest(
            intent=req.intent,
            constraints={
                **(req.constraints.model_dump() if req.constraints else {}),
                "reuse_policy": req.reuse_policy.value,
            },
            context=workflow_context or None,
            save=False,
        )
        gen_resp = await self._workflow_service.generate_workflow(
            wf_req,
            plan_id=plan_id,
            initial=initial,
            recall=recall,
            pre_reused=pre_reused,
            reuse_checked=initial is not None,
            validated_scripts=scripts,
        )
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

        await self.db.flush()
        if self.audit:
            await self.audit.log_event(
                "plan.approved", "plan", plan_id, payload={"comment": req.comment}
            )
        await self.db.commit()
        return await self._orm_to_schema(plan_orm)

    async def reject_plan(self, plan_id: str, req: RejectPlanRequest) -> Plan:
        plan_orm = await self._get_orm(plan_id)
        self._assert_transition(plan_orm, PlanStatus.rejected)
        plan_orm.status = PlanStatus.rejected
        plan_orm.updated_at = datetime.now(UTC)
        await self.db.flush()
        if self.audit:
            await self.audit.log_event(
                "plan.rejected", "plan", plan_id, payload={"reason": req.reason}
            )
        await self.db.commit()
        return await self._orm_to_schema(plan_orm)

    async def complete_plan(self, plan_id: str, req: CompletePlanRequest) -> Plan:
        plan_orm = await self._get_orm(plan_id)
        target = PlanStatus.completed if req.status == "completed" else PlanStatus.failed
        await self._finish(plan_orm, target, req.summary)
        return await self._orm_to_schema(plan_orm)

    async def cancel_plan(self, plan_id: str, reason: str | None = None) -> Plan:
        """Stop a plan. Running workflows notice on their next /trama/execute call
        (it refuses calls carrying a cancelled plan's X-RS-Plan header) and fail."""
        plan_orm = await self._get_orm(plan_id)
        await self._finish(plan_orm, PlanStatus.cancelled, reason)
        return await self._orm_to_schema(plan_orm)

    async def list_plans(self, status: list[str] | None = None, limit: int = 50) -> list[Plan]:
        stmt = select(PlanORM).order_by(PlanORM.created_at.desc()).limit(min(limit, 200))
        if status:
            stmt = stmt.where(PlanORM.status.in_(status))
        rows = (await self.db.execute(stmt)).scalars().all()
        return [await self._orm_to_schema(p) for p in rows]

    async def _finish(self, plan_orm: PlanORM, target: PlanStatus, summary: str | None) -> None:
        """Move to a terminal status, commit, then fire the plan's callback."""
        import asyncio

        self._assert_transition(plan_orm, target)
        plan_orm.status = target
        plan_orm.updated_at = datetime.now(UTC)
        if summary:
            plan_orm.summary = summary
        await self.db.flush()
        if self.audit:
            await self.audit.log_event(f"plan.{target.value}", "plan", plan_orm.plan_id)
        await self.db.commit()

        if plan_orm.callback_url:
            extra = await self._callback_extra(plan_orm.plan_id)
            asyncio.create_task(
                _fire_callback(
                    plan_orm.callback_url,
                    plan_orm.plan_id,
                    target.value,
                    plan_orm.summary,
                    _parse_allowed_hosts(self.settings.callback_allowed_hosts),
                    extra,
                )
            )

    async def _executions(self, plan_id: str) -> list[dict]:
        rows = (
            (
                await self.db.execute(
                    select(ExecutionORM)
                    .where(ExecutionORM.plan_id == plan_id)
                    .order_by(ExecutionORM.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "execution_id": e.execution_id,
                "artifact_id": e.artifact_id,
                "status": e.status,
                "delegated_execution_id": e.delegated_execution_id,
                "error": e.error_json,
            }
            for e in rows
        ]

    async def _callback_extra(self, plan_id: str) -> dict:
        execs = await self._executions(plan_id)
        if not execs:
            return {}
        last = execs[-1]
        return {
            "execution_id": last["execution_id"],
            "artifact_id": last["artifact_id"],
            "delegated_execution_id": last["delegated_execution_id"],
            "error": last["error"],
        }

    async def replan(self, failed_plan_id: str, failure_reason: str) -> Plan | None:
        """Create a successor plan when execution fails.

        Returns None if the re-plan budget is exhausted — caller should surface
        the failure to the user instead of retrying.
        """
        failed_orm = await self._get_orm(failed_plan_id)
        context = failed_orm.context or {}
        replan_count = context.get("replan_count", 0)

        if replan_count >= self.settings.casp_max_replan_attempts:
            return None

        self._assert_transition(failed_orm, PlanStatus.superseded)
        failed_orm.status = PlanStatus.superseded
        failed_orm.updated_at = datetime.now(UTC)
        await self.db.flush()

        if self.audit:
            await self.audit.log_event(
                "plan.superseded",
                "plan",
                failed_plan_id,
                payload={"reason": failure_reason, "replan_count": replan_count},
            )

        new_context = {
            **context,
            "replan_of": failed_plan_id,
            "replan_count": replan_count + 1,
            "previous_failure": failure_reason,
        }
        new_plan = await self.build_plan(
            CreatePlanRequest(
                intent=failed_orm.intent,
                context=new_context,
                reuse_policy=ReusePolicy.force_new,
            )
        )
        await self.db.commit()
        return new_plan

    async def persist_artifacts(self, plan_id: str) -> Plan:
        plan_orm = await self._get_orm(plan_id)
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
            raise InvalidTransitionError(f"Cannot transition plan from '{current}' to '{target}'")

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
            executions=await self._executions(plan_orm.plan_id),
        )
