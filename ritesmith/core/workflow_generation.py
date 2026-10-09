"""Generate native Trama workflows; exhausted validation returns generation_failed."""

import json
import logging
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.api.routes.artifacts import _build_artifact
from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.diagnostics import Diagnostic
from ritesmith.core.exceptions import GenerationFailedError, LLMError
from ritesmith.core.generation_budget import bounded_generation, current_budget
from ritesmith.core.ids import generate_id
from ritesmith.core.presence_workflow import PresenceReminder, presence_arrival_workflow
from ritesmith.core.repair import run_repair_loop
from ritesmith.core.reuse import check_reuse
from ritesmith.llm.base import LLMCallStats, LLMProvider, WorkflowGenerationResponse
from ritesmith.llm.generation_context import prepare_catalog
from ritesmith.observability.generation import current_trace, outcome, stage
from ritesmith.registry.models import Artifact as ArtifactORM
from ritesmith.registry.models import ArtifactVersion, GenerationAttempt, GenerationJob
from ritesmith.registry.search import SearchResult, fts_search
from ritesmith.registry.service import RegistryService
from ritesmith.schemas.artifact import Artifact, ArtifactStatus, ArtifactType, ValidationResult
from ritesmith.schemas.generation import GeneratedArtifactResponse, GenerateWorkflowRequest
from ritesmith.workflows.semantic import FORMAT_VERSION, compile_plan
from ritesmith.workflows.semantic_validation import validate_semantics
from ritesmith.workflows.validator import WorkflowValidator, branch_node_ids

log = logging.getLogger(__name__)

_COMPLETION_NODE_ID = "rs_complete"


def _inject_completion_step(definition: dict) -> dict:
    """Append an rs_complete task node and rewire terminal nodes ('next': 'end') to it."""
    nodes = definition.get("nodes")
    if not isinstance(nodes, list):
        return definition

    # Already injected
    if any(n.get("id") == _COMPLETION_NODE_ID for n in nodes):
        return definition

    # Rewire all nodes whose next is "end" to rs_complete — except inside split branches,
    # where "end" finishes the branch (a child execution) and Trama resumes the parent
    # at the join; completing the plan there would fire once per branch, before the join.
    inside_branches = branch_node_ids(nodes)
    for node in nodes:
        if node.get("id") in inside_branches:
            continue
        if node.get("next") == "end":
            node["next"] = _COMPLETION_NODE_ID
        # Switch nodes: rewire default and case targets that point to "end"
        if node.get("kind") == "switch":
            if node.get("default") == "end":
                node["default"] = _COMPLETION_NODE_ID
            for case in node.get("cases", []):
                if case.get("target") == "end":
                    case["target"] = _COMPLETION_NODE_ID

    completion_node = {
        "id": _COMPLETION_NODE_ID,
        "kind": "task",
        "action": {
            "mode": "sync",
            "request": {
                "verb": "POST",
                "url": "__RS_BASE_URL__/plans/__RS_PLAN_ID__/complete",
                "headers": {"Content-Type": "application/json"},
                "body": {"status": "completed"},
            },
            "successStatusCodes": [200],
        },
        "next": "end",
    }
    if definition.get("entrypoint") == "end":
        definition["entrypoint"] = _COMPLETION_NODE_ID
    nodes.append(completion_node)
    return definition


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

    @bounded_generation
    async def generate_workflow(
        self,
        req: GenerateWorkflowRequest,
        plan_id: str | None = None,
        *,
        initial: tuple[WorkflowGenerationResponse, LLMCallStats] | None = None,
        recall: list[SearchResult] | None = None,
        pre_reused: GeneratedArtifactResponse | None = None,
        reuse_checked: bool = False,
        validated_scripts: list[Artifact] | None = None,
    ) -> GeneratedArtifactResponse:
        trace = current_trace.get()
        if trace is not None:
            trace.artifact_types.add("trama_workflow")
        if pre_reused is not None:
            return pre_reused
        constraints = req.constraints or {}
        if req.required_capabilities is not None:
            constraints = {**constraints, "required_capabilities": req.required_capabilities}
        reuse_policy = constraints.get("reuse_policy", "prefer_reuse")
        continuation = (
            (req.context or {}).get("workflow_continuation")
            if self.settings.generation_semantic_workflows
            else None
        )
        if continuation is not None and not isinstance(continuation, dict):
            outcome("trama_workflow", False)
            raise GenerationFailedError(
                "Invalid workflow continuation",
                details={
                    "category": "graph",
                    "diagnostics": ["workflow_continuation must be an object"],
                },
            )
        if continuation and continuation.get("intent") != req.intent:
            continuation = None
        presence = None
        if "presence_reminder" in (req.context or {}):
            presence = PresenceReminder.model_validate(req.context["presence_reminder"])

        if recall is None and not presence and not continuation:
            with stage("recall", "trama_workflow"):
                recall = await fts_search(
                    self.db,
                    req.intent,
                    artifact_types=["trama_workflow"],
                    limit=max(2, self.settings.reuse_recall_limit),
                )

        # 1. Reuse check
        if (
            not presence
            and not continuation
            and not reuse_checked
            and (
                reuse := await check_reuse(
                    self.db,
                    req.intent,
                    ["trama_workflow"],
                    reuse_policy,
                    self.audit,
                    results=recall,
                    llm=self.llm,
                    settings=self.settings,
                )
            )
        ):
            return reuse

        # 2. Collect available capabilities — provider caps (runtime) + registered Lua artifacts (DB)
        from ritesmith.runtime.host_functions import get_available_provider_capabilities

        provider_caps = get_available_provider_capabilities()
        if presence:
            missing = {"home.execute", "telegram.send"} - {
                c["capability_name"] for c in provider_caps
            }
            if missing:
                raise ValueError(
                    "Presence reminder capabilities unavailable: " + ", ".join(sorted(missing))
                )
        db_caps = await self._get_capabilities()

        # Combined: provider caps first (they're the primary ones), then DB artifacts
        all_caps_for_prompt = provider_caps + db_caps

        # Registry keyed by capability_name for the validator
        cap_registry: dict[str, dict] = {c["capability_name"]: c for c in provider_caps}
        for c in db_caps:
            key = c.get("capability_name") or c.get("capability_id", "")
            if key:
                cap_registry.setdefault(key, c)

        # 3. Similar workflows for few-shot examples — include full content like Lua generation does
        similar = (
            []
            if presence
            else [r for r in recall or [] if r.artifact.artifact_type == "trama_workflow"][:2]
        )
        similar_dicts = [
            {
                "name": r.artifact.name,
                "description": r.artifact.description or "",
                "content": r.version.content if r.version else None,
            }
            for r in similar
        ]

        artifact_contracts = {
            artifact.artifact_id: {
                "input_schema": artifact._input_schema,
                "output_schema": artifact._output_schema,
            }
            for artifact in validated_scripts or []
        }
        validator = WorkflowValidator(cap_registry)

        last_definition: dict | None = None
        last_errors: list[str] = []
        final_response: WorkflowGenerationResponse | None = None
        active_llm = self.llm
        attempts = []

        async def attempt_fn(attempt: int) -> bool:
            nonlocal last_definition, last_errors, final_response, active_llm
            if attempt > 1 and last_definition is None:
                active_llm = self.llm.fallback()

            if continuation:
                catalog = prepare_catalog(req.intent, all_caps_for_prompt)
                try:
                    if (
                        continuation.get("version") != FORMAT_VERSION
                        or continuation.get("contract_version") != catalog.version
                    ):
                        raise ValueError(
                            "Continuation version/contracts changed; explicit regeneration required"
                        )
                    state = dict((req.context or {}).get("continuation", {}))
                    state["_resume_repeat"] = continuation.get("completed_repeat")
                    definition = compile_plan(
                        continuation["plan"],
                        self.settings.public_url or "http://ritesmith:8081",
                        contract_version=catalog.version,
                        continuation=state,
                        intent=req.intent,
                        constraints=constraints,
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    last_errors = ["workflow_continuation: " + str(exc)]
                    last_definition = continuation.get("plan")
                    attempts.append((attempt, last_definition, last_errors))
                    raise GenerationFailedError(
                        "Invalid workflow continuation; explicit correction is required",
                        details={"category": "graph", "diagnostics": last_errors},
                    ) from exc
                llm_resp = WorkflowGenerationResponse(
                    definition=definition,
                    name=definition["name"],
                    description=req.intent,
                )
            elif presence:
                definition = presence_arrival_workflow(
                    presence, self.settings.public_url or "http://ritesmith:8081"
                )
                llm_resp = WorkflowGenerationResponse(
                    definition=definition,
                    name=definition["name"],
                    description=req.intent,
                    required_capabilities=["home.execute", "telegram.send"],
                )
            elif attempt == 1 and initial is not None:
                llm_resp, stats = initial
                definition = llm_resp.definition
            elif attempt == 1 or last_definition is None:
                base_url = self.settings.public_url or "http://ritesmith:8081"
                try:
                    llm_resp, stats = await active_llm.generate_workflow(
                        goal=req.intent,
                        available_capabilities=all_caps_for_prompt,
                        constraints=constraints,
                        similar_workflows=similar_dicts,
                        ritesmith_base_url=base_url,
                        context=req.context,
                    )
                except LLMError as exc:
                    last_errors = [str(exc)]
                    candidate = exc.details.get("candidate")
                    if isinstance(candidate, (dict, str)):
                        last_definition = candidate
                    attempts.append((attempt, candidate, [str(exc)]))
                    raise
                definition = llm_resp.definition
            else:
                repair_resp, stats = await active_llm.repair_workflow(
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
                    required_capabilities=final_response.required_capabilities
                    if final_response
                    else [],
                )

            final_response = llm_resp
            from ritesmith.workflows.validator import native_shape_errors

            shape_errors = native_shape_errors(definition)
            if shape_errors:
                last_definition, last_errors = definition, shape_errors
                attempts.append((attempt, definition, shape_errors))
                return False
            if plan_id:
                definition = _inject_completion_step(deepcopy(definition))

            from ritesmith.workflows.transport import typed_json_workflow

            try:
                definition = typed_json_workflow(definition)
            except ValueError as exc:
                last_definition, last_errors = definition, [str(exc)]
                attempts.append((attempt, definition, last_errors))
                return False

            if self.audit and not presence and not continuation:
                await self.audit.log_event(
                    "workflow_generation.llm_call",
                    "workflow_generation",
                    plan_id or "none",
                    payload={
                        "attempt": attempt,
                        "tokens": stats.total_tokens,
                        "stats": stats.model_dump(),
                    },
                )

            with stage("validation", "trama_workflow"):
                errors = validator.validate(definition)
                if not errors:
                    ids = {
                        body.get("artifact_id")
                        for node in definition.get("nodes", [])
                        if node.get("kind") == "task"
                        and isinstance(
                            body := node.get("action", {}).get("request", {}).get("body", {}), dict
                        )
                    } - {None}
                    missing = ids - artifact_contracts.keys()
                    if missing:
                        result = await self.db.execute(
                            select(ArtifactORM, ArtifactVersion)
                            .join(
                                ArtifactVersion,
                                (ArtifactORM.artifact_id == ArtifactVersion.artifact_id)
                                & (ArtifactORM.current_version == ArtifactVersion.version),
                            )
                            .where(
                                ArtifactORM.artifact_id.in_(missing),
                                ArtifactORM.artifact_type.in_(["lua_script", "luau_script"]),
                                ArtifactORM.status.not_in(["deprecated", "rejected", "archived"]),
                            )
                        )
                        for artifact, version in result.all():
                            artifact_contracts[artifact.artifact_id] = {
                                "input_schema": version.input_schema,
                                "output_schema": version.output_schema,
                            }
                        for unknown in missing - artifact_contracts.keys():
                            errors.append(f"Unknown/unavailable script artifact: {unknown}")
                    if not errors:
                        errors.extend(
                            validate_semantics(
                                definition,
                                {**cap_registry, **artifact_contracts},
                                check_graph=self.settings.generation_semantic_workflows,
                            )
                        )
            last_definition = definition
            last_errors = errors
            attempts.append((attempt, definition, errors))
            log.info(
                "workflow generation attempt %d valid=%s",
                attempt,
                not errors,
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
                settings=self.settings,
                state_fn=lambda: (
                    json.dumps(last_definition, sort_keys=True),
                    [Diagnostic(category="graph", message=error) for error in last_errors],
                ),
                initial_recoveries=max(0, initial[1].api_attempts - 1) if initial else 0,
                initial_duration=initial[1].duration_s if initial else 0,
            )

            # 5. Build ValidationResult
            valid = last_definition is not None and not last_errors
            validation = ValidationResult(
                valid=valid,
                errors=last_errors,
                warnings=[],
                checks=[],
            )
            if not valid:
                outcome("trama_workflow", False)
                raise GenerationFailedError(
                    "No acceptable workflow after bounded recovery",
                    details={
                        "category": "graph",
                        "diagnostics": last_errors or ["Missing workflow candidate"],
                    },
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
                        "artifact.created",
                        "artifact",
                        artifact_orm.artifact_id,
                        payload={"goal": req.intent},
                    )
        except Exception:
            outcome("trama_workflow", False)
            await self.db.rollback()
            job = GenerationJob(
                generation_id=generate_id("gen"),
                goal=req.intent,
                target_type="trama_workflow",
                status="failed",
                constraints=constraints,
                plan_id=plan_id,
                attempts=len(attempts),
                finished_at=datetime.now(UTC),
            )
            self.db.add(job)
            await self.db.flush()
            for number, candidate, errors in attempts:
                self.db.add(
                    GenerationAttempt(
                        generation_id=job.generation_id,
                        attempt_number=number,
                        content=candidate if isinstance(candidate, str) else json.dumps(candidate),
                        validation_result={"valid": False, "errors": errors},
                    )
                )
            if self.audit:
                await self.audit.log_event(
                    "generation.failed",
                    "workflow_generation",
                    plan_id,
                    payload={"candidate": last_definition, "diagnostics": last_errors},
                )
            await self.db.commit()
            log.error("workflow generation failed with exception", exc_info=True)
            raise
        else:
            # Audit is durable even when the generated artifact is transient.
            with stage("persist", "trama_workflow"):
                if not req.save and self.audit is None:
                    await self.db.rollback()
                else:
                    await self.db.commit()

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

        outcome("trama_workflow", valid)
        return GeneratedArtifactResponse(
            artifact=artifact_schema,
            validation=validation,
            reused=False,
        )

    async def _get_capabilities(self) -> list[dict]:
        budget = current_budget.get()
        if budget and "capabilities" in budget.prepared:
            return budget.prepared["capabilities"]
        caps = await self.registry.list_capabilities(limit=None, status="available")
        result = [
            {
                "capability_id": c.capability_id,
                "capability_name": c.name,
                "name": c.name,
                "description": c.description,
                "input_schema": c.input_schema,
                "output_schema": c.output_schema,
            }
            for c in caps
        ]
        if budget is not None:
            budget.prepared["capabilities"] = result
        return result
