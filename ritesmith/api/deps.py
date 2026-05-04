from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.config import Settings, get_settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.execution import ExecutionService
from ritesmith.core.generation import GenerationService
from ritesmith.core.generation_dispatcher import GenerationDispatcher
from ritesmith.core.idempotency import IdempotencyService
from ritesmith.core.planning import PlanBuilder
from ritesmith.core.workflow_generation import WorkflowGenerationService
from ritesmith.llm.base import LLMProvider
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.storage.postgres import get_db


def get_audit(db: AsyncSession = Depends(get_db)) -> AuditLogger:
    return AuditLogger(db)


def get_idempotency(db: AsyncSession = Depends(get_db)) -> IdempotencyService:
    return IdempotencyService(db)


def get_llm_provider(settings: Settings = Depends(get_settings)) -> LLMProvider:
    return OpenAIProvider(settings)


def get_generation_service(
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
    audit: AuditLogger = Depends(get_audit),
) -> GenerationService:
    return GenerationService(db=db, llm=llm, settings=settings, audit=audit)


def get_plan_builder(
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
    audit: AuditLogger = Depends(get_audit),
) -> PlanBuilder:
    return PlanBuilder(db=db, llm=llm, settings=settings, audit=audit)


def get_execution_service(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    audit: AuditLogger = Depends(get_audit),
) -> ExecutionService:
    return ExecutionService(db=db, settings=settings, audit=audit)


def get_generation_dispatcher(
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
    audit: AuditLogger = Depends(get_audit),
) -> GenerationDispatcher:
    return GenerationDispatcher(db=db, llm=llm, settings=settings, audit=audit)


def get_workflow_generation_service(
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
    audit: AuditLogger = Depends(get_audit),
) -> WorkflowGenerationService:
    return WorkflowGenerationService(db=db, llm=llm, settings=settings, audit=audit)
