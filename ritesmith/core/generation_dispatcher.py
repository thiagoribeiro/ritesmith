"""GenerationDispatcher — routes a unified GenerateRequest to the right generation service."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.generation import GenerationService
from ritesmith.core.generation_budget import bounded_generation
from ritesmith.core.proposal import prepare_proposal
from ritesmith.core.workflow_generation import WorkflowGenerationService
from ritesmith.llm.base import LLMProvider
from ritesmith.observability.generation import stage
from ritesmith.schemas.generation import (
    GeneratedArtifactResponse,
    GenerateRequest,
    GenerateScriptRequest,
    GenerateWorkflowRequest,
    ScriptConstraints,
)


class GenerationDispatcher:
    def __init__(
        self,
        db: AsyncSession,
        llm: LLMProvider,
        settings: Settings,
        audit: AuditLogger | None = None,
    ):
        self._llm = llm
        self._db = db
        self._settings = settings
        self._audit = audit
        self._lua = GenerationService(db=db, llm=llm, settings=settings, audit=audit)
        self._workflow = WorkflowGenerationService(db=db, llm=llm, settings=settings, audit=audit)

    @bounded_generation
    async def dispatch(
        self,
        req: GenerateRequest,
        plan_id: str | None = None,
    ) -> GeneratedArtifactResponse:
        if "presence_reminder" in (req.context or {}) or "workflow_continuation" in (
            req.context or {}
        ):
            return await self._workflow.generate_workflow(
                GenerateWorkflowRequest(
                    **req.model_dump(exclude={"input_schema", "output_schema"})
                ),
                plan_id,
            )
        proposal, recall, reusable = await prepare_proposal(
            self._db,
            self._llm,
            self._settings,
            goal=req.intent,
            constraints=req.constraints or {},
            context=req.context,
            input_schema=req.input_schema,
            output_schema=req.output_schema,
            profile=(req.constraints or {}).get("runtime_profile", "transform_only"),
            audit=self._audit,
            allow_dependencies=False,
        )
        if reusable is not None:
            return reusable
        if proposal is not None:
            initial, stats = proposal
            intent = initial.analysis
        else:
            with stage("intent", "mixed"):
                intent, _ = await self._llm.analyze_intent(
                    req.intent, req.constraints or {}, context=req.context
                )

        if intent.requires_workflow:
            wf_req = GenerateWorkflowRequest(
                intent=req.intent,
                save=req.save,
                context=req.context,
                constraints=req.constraints,
            )
            kwargs = (
                {}
                if proposal is None
                else {
                    "initial": (initial.workflow, stats),
                    "recall": recall,
                    "reuse_checked": True,
                }
            )
            return await self._workflow.generate_workflow(wf_req, plan_id, **kwargs)

        lua_constraints: ScriptConstraints | None = None
        if req.constraints:
            try:
                lua_constraints = ScriptConstraints(**req.constraints)
            except Exception:
                lua_constraints = None

        lua_req = GenerateScriptRequest(
            intent=req.intent,
            save=req.save,
            context=req.context,
            constraints=lua_constraints,
            input_schema=req.input_schema,
            output_schema=req.output_schema,
        )
        kwargs = (
            {}
            if proposal is None
            else {
                "initial": (initial.script, stats),
                "recall": recall,
                "initial_tests": (initial._test_cases, initial._tests_attempted),
                "reuse_checked": True,
            }
        )
        return await self._lua.generate_lua(lua_req, plan_id, **kwargs)
