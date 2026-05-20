"""ExecutionService — routes artifact execution to the correct runtime or delegate.

Fluxo:
1. Idempotency check — se já existe uma execução com essa key, retorna ela
2. Cria Execution record com status=queued
3. Avalia política com PolicyEngine
   - deny → status=rejected, para aqui
   - require_approval sem approval_token → status=waiting, para aqui
4. Roteia por artifact_type:
   - lua_script → LuaScriptRuntime.execute()
   - trama_workflow → DelegationService.delegate()
5. Atualiza Execution com resultado e audit log
"""
import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.config import Settings
from ritesmith.core.audit import AuditLogger
from ritesmith.core.exceptions import NotFoundError, RiteSmithError
from ritesmith.observability.metrics import execution_duration, execution_status_total
from ritesmith.core.ids import generate_id
from ritesmith.core.policy import PolicyEngine
from ritesmith.registry.models import Execution as ExecutionORM, GenerationJob
from ritesmith.registry.service import RegistryService
from ritesmith.runtime.lua import LuaScriptRuntime
from ritesmith.schemas.execution import CreateExecutionRequest, Execution, ExecutionStatus
from ritesmith.schemas.generation import GenerateScriptRequest, ScriptConstraints
from ritesmith.schemas.policy import PolicyDecisionValue, PolicyEvaluationRequest


log = logging.getLogger(__name__)

_CRASH_PREFIXES = ("Runtime error:", "Syntax/load error:")


def _is_contract_crash(error_json: dict | None) -> bool:
    if not error_json:
        return False
    msg = error_json.get("message", "")
    return any(msg.startswith(p) for p in _CRASH_PREFIXES)


class ExecutionService:
    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        audit: AuditLogger | None = None,
    ):
        self.db = db
        self.settings = settings
        self.audit = audit
        self.policy = PolicyEngine(settings)
        self.registry = RegistryService(db)
        self.lua_runtime = LuaScriptRuntime(settings)

    async def create_execution(self, req: CreateExecutionRequest) -> Execution:
        # 1. Idempotency
        if req.idempotency_key:
            existing = await self._find_by_idempotency_key(req.idempotency_key)
            if existing:
                return self._orm_to_schema(existing)

        # 2. Verify artifact exists
        pair = await self.registry.get_artifact_with_version(req.artifact_id, req.version)
        if not pair:
            raise NotFoundError(f"Artifact '{req.artifact_id}' not found")
        artifact_orm, version_orm = pair

        # 3. Create execution record
        now = datetime.now(UTC)
        execution_id = generate_id("exec")
        exec_orm = ExecutionORM(
            execution_id=execution_id,
            artifact_id=req.artifact_id,
            artifact_version=version_orm.version,
            plan_id=req.plan_id,
            status=ExecutionStatus.queued,
            runtime=req.runtime or artifact_orm.artifact_type,
            input_json=req.input,
            dry_run=req.dry_run,
            idempotency_key=req.idempotency_key,
            approval_token=req.approval_token,
            created_at=now,
        )
        self.db.add(exec_orm)
        await self.db.flush()

        # 4. Policy evaluation
        _exec_start = time.perf_counter()
        _runtime = "trama" if artifact_orm.artifact_type == "trama_workflow" else "lua"
        policy_req = PolicyEvaluationRequest(
            operation="execute",
            artifact_type=artifact_orm.artifact_type,
            risk_level=version_orm.risk_level or "low",
        )
        decision = self.policy.evaluate(policy_req)

        if decision.decision == PolicyDecisionValue.deny:
            exec_orm.status = ExecutionStatus.rejected
            exec_orm.error_json = {"reason": decision.reason}
            exec_orm.finished_at = datetime.now(UTC)
            await self.db.flush()
            log.warning(
                "execution rejected by policy",
                extra={"execution_id": execution_id, "artifact_id": req.artifact_id, "reason": decision.reason},
            )
            if self.audit:
                await self.audit.log_event("execution.rejected", "execution", execution_id,
                                           payload={"reason": decision.reason})
            execution_status_total.labels(status=exec_orm.status).inc()
            execution_duration.labels(runtime=_runtime).observe(time.perf_counter() - _exec_start)
            await self.db.commit()
            return self._orm_to_schema(exec_orm)

        if decision.decision == PolicyDecisionValue.require_approval and not req.approval_token:
            exec_orm.status = ExecutionStatus.waiting
            exec_orm.error_json = {"reason": decision.reason, "requires_approval": True}
            await self.db.flush()
            execution_status_total.labels(status=exec_orm.status).inc()
            execution_duration.labels(runtime=_runtime).observe(time.perf_counter() - _exec_start)
            await self.db.commit()
            return self._orm_to_schema(exec_orm)

        # 5. Route and execute
        exec_orm.status = ExecutionStatus.running
        exec_orm.started_at = datetime.now(UTC)
        await self.db.flush()
        log.info(
            "execution started runtime=%s dry_run=%s",
            _runtime, req.dry_run,
            extra={"execution_id": execution_id, "artifact_id": req.artifact_id, "runtime": _runtime},
        )

        if self.audit:
            await self.audit.log_event("execution.started", "execution", execution_id,
                                       payload={"artifact_id": req.artifact_id, "dry_run": req.dry_run})

        try:
            if artifact_orm.artifact_type == "trama_workflow":
                await self._run_workflow(exec_orm, version_orm.content, req.input)
            else:
                profile = (version_orm.metadata_ or {}).get("runtime_profile", "transform_only")
                await self._run_lua(exec_orm, version_orm, req.input, profile=profile)
        except Exception as e:
            exec_orm.status = ExecutionStatus.failed
            exec_orm.error_json = {"message": str(e)}
            exec_orm.finished_at = datetime.now(UTC)
            log.error(
                "execution failed with exception",
                extra={"execution_id": execution_id, "artifact_id": req.artifact_id},
                exc_info=True,
            )
            if self.audit:
                await self.audit.log_event("execution.failed", "execution", execution_id,
                                           payload={"error": str(e)})

        execution_status_total.labels(status=exec_orm.status).inc()
        elapsed = time.perf_counter() - _exec_start
        execution_duration.labels(runtime=_runtime).observe(elapsed)
        log.info(
            "execution finished status=%s duration=%.3fs",
            exec_orm.status, elapsed,
            extra={"execution_id": execution_id, "status": exec_orm.status, "duration_s": round(elapsed, 3)},
        )
        await self.db.flush()
        await self.db.commit()
        return self._orm_to_schema(exec_orm)

    async def get_execution(self, execution_id: str) -> Execution:
        result = await self.db.execute(
            select(ExecutionORM).where(ExecutionORM.execution_id == execution_id)
        )
        orm = result.scalar_one_or_none()
        if not orm:
            raise NotFoundError(f"Execution '{execution_id}' not found")
        return self._orm_to_schema(orm)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _run_lua(self, exec_orm: ExecutionORM, version_orm, input_data: dict | None, profile: str = "transform_only") -> None:
        if exec_orm.dry_run:
            exec_orm.status = ExecutionStatus.succeeded
            exec_orm.output_json = {"dry_run": True, "message": "Dry run — script not executed"}
            exec_orm.finished_at = datetime.now(UTC)
            return

        result = await self.lua_runtime.execute(
            content=version_orm.content,
            input_data=input_data or {},
            context={},
            output_schema=version_orm.output_schema,
            profile=profile,
        )

        if result.timed_out:
            exec_orm.status = ExecutionStatus.failed
            exec_orm.error_json = {"message": "Script execution timed out"}
        elif result.error:
            exec_orm.status = ExecutionStatus.failed
            exec_orm.error_json = {"message": result.error}
            if _is_contract_crash(exec_orm.error_json):
                await self._deprecate_and_regen(exec_orm.artifact_id, version_orm)
        else:
            exec_orm.status = ExecutionStatus.succeeded
            exec_orm.output_json = result.output

        exec_orm.finished_at = datetime.now(UTC)

        if self.audit:
            await self.audit.log_event(
                "execution.completed", "execution", exec_orm.execution_id,
                payload={"status": exec_orm.status, "duration_ms": result.duration_ms},
            )

    async def _deprecate_and_regen(self, artifact_id: str, version_orm) -> None:
        try:
            await self.registry.update_artifact_status(artifact_id, "deprecated")
            log.warning("artifact deprecated after runtime crash: %s", artifact_id)
        except Exception:
            log.warning("could not deprecate crashed artifact %s", artifact_id, exc_info=True)
        t = asyncio.create_task(self._background_regen(artifact_id, version_orm))
        t.add_done_callback(lambda task: (
            log.error("background_regen failed artifact=%s: %s", artifact_id, task.exception(), exc_info=task.exception())
            if not task.cancelled() and task.exception() else None
        ))

    async def _background_regen(self, artifact_id: str, version_orm) -> None:
        from ritesmith.core.generation import GenerationService
        from ritesmith.llm.openai_provider import OpenAIProvider
        from ritesmith.storage.postgres import AsyncSessionLocal

        job_row = await self.db.execute(
            select(GenerationJob).where(GenerationJob.final_artifact_id == artifact_id)
        )
        job = job_row.scalar_one_or_none()
        if not job:
            log.warning("no GenerationJob found for crashed artifact %s, skipping regen", artifact_id)
            return

        profile = (version_orm.metadata_ or {}).get("runtime_profile", "transform_only")
        req = GenerateScriptRequest(
            intent=job.goal,
            save=True,
            constraints=ScriptConstraints(
                runtime_profile=profile,
                reuse_policy="force_new",
            ),
        )

        try:
            async with AsyncSessionLocal() as fresh_db:
                llm = OpenAIProvider(self.settings)
                await GenerationService(fresh_db, llm, self.settings).generate_lua(req)
            log.info("background regen succeeded for crashed artifact %s", artifact_id)
        except Exception:
            log.error("background regen failed for crashed artifact %s", artifact_id, exc_info=True)

    async def _run_workflow(self, exec_orm: ExecutionORM, content: str, input_data: dict | None) -> None:
        import json
        if self.settings.trama_token:
            content = content.replace("__TRAMA_TOKEN__", self.settings.trama_token)
        if self.settings.public_url:
            content = content.replace("__RS_BASE_URL__", self.settings.public_url)
        content = content.replace("__RS_PLAN_ID__", exec_orm.plan_id or "")
        try:
            definition = json.loads(content)
        except Exception:
            definition = content

        if self.settings.workflow_delegation_enabled and self.settings.workflow_engine_url:
            from ritesmith.core.delegation import DelegationService
            from ritesmith.workflows.adapters.http_workflow_engine import HttpWorkflowEngineAdapter
            adapter = HttpWorkflowEngineAdapter(self.settings.workflow_engine_url)
            delegation = DelegationService(adapter)
            delegated_id = await delegation.delegate(definition, input_data)
            exec_orm.delegated_execution_id = delegated_id
            exec_orm.status = ExecutionStatus.waiting
            exec_orm.finished_at = None
            if self.audit:
                await self.audit.log_event("execution.delegated", "execution", exec_orm.execution_id,
                                           payload={"delegated_id": delegated_id, "engine": self.settings.workflow_engine_url})
        else:
            delegated_id = generate_id("trama")
            exec_orm.delegated_execution_id = delegated_id
            exec_orm.status = ExecutionStatus.waiting
            exec_orm.finished_at = None
            if self.audit:
                await self.audit.log_event("execution.delegated", "execution", exec_orm.execution_id,
                                           payload={"delegated_id": delegated_id})

    async def _find_by_idempotency_key(self, key: str) -> ExecutionORM | None:
        result = await self.db.execute(
            select(ExecutionORM).where(ExecutionORM.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    def _orm_to_schema(self, orm: ExecutionORM) -> Execution:
        return Execution(
            execution_id=orm.execution_id,
            artifact_id=orm.artifact_id,
            artifact_version=orm.artifact_version,
            plan_id=orm.plan_id,
            runtime=orm.runtime,
            status=ExecutionStatus(orm.status),
            input=orm.input_json,
            output=orm.output_json,
            error=orm.error_json,
            idempotency_key=orm.idempotency_key,
            delegated_execution_id=orm.delegated_execution_id,
            dry_run=orm.dry_run,
            started_at=orm.started_at,
            finished_at=orm.finished_at,
            created_at=orm.created_at,
        )
