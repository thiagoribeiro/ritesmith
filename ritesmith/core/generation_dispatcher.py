"""GenerationDispatcher — routes a unified GenerateRequest to the right generation service."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.generation import GenerationService
from ritesmith.core.workflow_generation import WorkflowGenerationService
from ritesmith.llm.base import LLMProvider
from ritesmith.schemas.generation import (
    GenerateRequest,
    GeneratedArtifactResponse,
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
        self._lua = GenerationService(db=db, llm=llm, settings=settings, audit=audit)
        self._workflow = WorkflowGenerationService(db=db, llm=llm, settings=settings, audit=audit)

    async def dispatch(
        self,
        req: GenerateRequest,
        plan_id: str | None = None,
    ) -> GeneratedArtifactResponse:
        intent, _ = await self._llm.analyze_intent(req.intent, req.constraints or {})

        if intent.requires_workflow:
            wf_req = GenerateWorkflowRequest(
                intent=req.intent,
                save=req.save,
                context=req.context,
                constraints=req.constraints,
            )
            return await self._workflow.generate_workflow(wf_req, plan_id)

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
        return await self._lua.generate_lua(lua_req, plan_id)
