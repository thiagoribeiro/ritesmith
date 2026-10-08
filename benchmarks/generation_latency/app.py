"""Staging-only API. Never import this module into the production application.

Scripts are checked/executed by production LunarDyson with pure benchmark stubs.
No production tool or workflow is executed. HTTP/storage flow is production code;
stubbed script validation latency is reported separately.
"""

import asyncio
import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from benchmarks.generation_latency.baseline_prompts import workflow_generation_user as baseline_user
from benchmarks.generation_latency.native import validate
from benchmarks.generation_latency.tasks import BY_ID
from benchmarks.generation_latency.version import software_manifest
from ritesmith.api.app import create_app
from ritesmith.api.deps import get_llm_provider
from ritesmith.api.limiter import limiter
from ritesmith.config import get_settings
from ritesmith.core import generation, proposal, workflow_generation
from ritesmith.core.validation import ValidationPipeline
from ritesmith.llm import prompts
from ritesmith.llm.openai_provider import OpenAIProvider
from ritesmith.runtime import luau
from ritesmith.schemas.artifact import ValidationCheck, ValidationResult


@dataclass
class Scope:
    task: dict
    phase: str
    model: str
    calls: list = field(default_factory=list)
    cold: bool = False


scope: ContextVar[Scope | None] = ContextVar("benchmark_scope", default=None)
SOFTWARE = software_manifest()
_original_types = luau.script_type_declarations
_original_tools = luau.describe_tools_for_prompt
_original_run = ValidationPipeline.run
_original_workflow_user = prompts.workflow_generation_user
_original_search = generation.fts_search


async def cold_search(*args, **kwargs):
    results = await _original_search(*args, **kwargs)
    # Preserve the database lookup's time, while excluding previously generated
    # artifacts from both reuse and few-shot context in this cold-artifact run.
    return [] if scope.get() else results


def workflow_user(*args, **kwargs):
    current = scope.get()
    if current and current.phase not in ("full", "mermaid"):
        return baseline_user(*args, **kwargs)
    return _original_workflow_user(*args, **kwargs)


def script_types(input_schema, output_schema):
    current = scope.get()
    task = current.task.get("script_task", current.task) if current else None
    if task and "types" in task:
        return "type Context = { [string]: any }\n" + task["types"]
    return _original_types(input_schema, output_schema)


def script_tools(profile):
    current = scope.get()
    task = current.task.get("script_task", current.task) if current else None
    if task and "types" in task:
        return [
            "tools." + t["name"] + t["signature"] + " -- " + t["description"]
            for t in task.get("tools", [])
        ]
    return _original_tools(profile)


async def safe_validation(self, *, content, artifact_type, **kwargs):
    current = scope.get()
    if not current or artifact_type != "luau_script":
        return await _original_run(self, content=content, artifact_type=artifact_type, **kwargs)
    task = current.task.get("script_task", current.task)
    if "test_cases" not in task:
        return ValidationResult(valid=False, errors=["Unexpected script in workflow-only scenario"])
    result = await asyncio.to_thread(
        validate, task, content, "luau", "strict", test_cases=kwargs.get("test_cases")
    )
    checks = [
        ValidationCheck(
            name="strict_type_check",
            status="passed" if not result.typecheck.get("strict", {}).get("errors") else "warning",
        )
    ]
    checks.extend(self._check_forbidden_tokens(content))
    checks.extend(self._check_size(content, kwargs.get("constraints"), factor=1.5))
    checks.extend(self._check_schema_presence(content))
    structural_errors = [c.message or c.name for c in checks if c.status == "failed"]
    return ValidationResult(
        valid=result.passed and not structural_errors,
        errors=result.repair_errors() + structural_errors,
        checks=checks,
        warnings=result.typecheck.get("strict", {}).get("errors", []),
    )


luau.script_type_declarations = script_types
luau.describe_tools_for_prompt = script_tools
ValidationPipeline.run = safe_validation
prompts.workflow_generation_user = workflow_user
generation.fts_search = cold_search
proposal.fts_search = cold_search
workflow_generation.fts_search = cold_search


class BenchProvider(OpenAIProvider):
    def fallback(self):
        if self.use_fallback or not self.settings.llm_fallback_enabled:
            return self
        return BenchProvider(self.settings, client=self.client, use_fallback=True)

    async def _chat_with_retry(self, *args, **kwargs):
        current = scope.get()
        if current and current.cold:
            # Unique prefix prevents cross-request prompt-cache hits for the cold
            # arm; record actual cached_tokens rather than assume it succeeded.
            prefix = "Evaluation request " + uuid.uuid4().hex + ".\n"
            if len(args) > 1:
                args = (*args[:1], prefix + args[1], *args[2:])
            else:
                kwargs["system"] = prefix + kwargs["system"]
        raw, stats = await super()._chat_with_retry(*args, **kwargs)
        if current:
            current.calls.append(stats.model_dump())
        return raw, stats

    async def generate_luau(self, **kwargs):
        kwargs["tool_descriptions"] = script_tools("transform_only")
        kwargs["type_declarations"] = script_types(None, None)
        return await super().generate_luau(**kwargs)

    async def repair_luau(self, **kwargs):
        kwargs["tool_descriptions"] = script_tools("transform_only")
        kwargs["type_declarations"] = script_types(None, None)
        return await super().repair_luau(**kwargs)


app = create_app()
limiter.enabled = False
providers = {}


def settings_for(request: Request):
    current = scope.get()
    settings = get_settings()
    if current is None:
        return settings
    baseline = current.phase == "baseline"
    return settings.model_copy(
        update={
            "llm_model": "gpt-5-mini",
            "llm_script_model": None if baseline else current.model,
            "llm_workflow_model": None if baseline else current.model,
            "llm_reasoning_effort": None if baseline else "low",
            "llm_fallback_enabled": True,
            "generation_specialized_prompts": current.phase in ("filtered", "full", "mermaid"),
            "generation_compact_workflows": current.phase in ("full", "mermaid"),
            "generation_mermaid_workflows": current.phase == "mermaid",
            "generation_unified_proposal": current.phase in ("full", "mermaid"),
            "generation_parallel_tests": current.phase in ("full", "mermaid"),
            "require_tests_min_risk": "medium",
        }
    )


def llm_for(request: Request):
    settings = settings_for(request)
    current = scope.get()
    key = (current.phase, current.model) if current else ("baseline", "gpt-5-mini")
    if key not in providers:
        providers[key] = BenchProvider(settings, client=app.state.llm_provider.client)
    return providers[key]


app.dependency_overrides[get_settings] = settings_for
app.dependency_overrides[get_llm_provider] = llm_for


class BenchMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        id_ = request.headers.get("x-benchmark-task")
        if id_ not in BY_ID:
            return await call_next(request)
        current = Scope(
            BY_ID[id_],
            request.headers.get("x-benchmark-phase", "baseline"),
            request.headers.get("x-benchmark-model", "gpt-5-mini"),
            cold=request.headers.get("x-benchmark-provider-cache") == "cold",
        )
        token = scope.set(current)
        try:
            response = await call_next(request)
            trace = getattr(request.state, "generation_trace", None)
            response.headers["x-benchmark-calls"] = json.dumps(
                trace.calls if trace else current.calls, separators=(",", ":")
            )
            response.headers["x-benchmark-stages"] = json.dumps(
                trace.stages if trace else [], separators=(",", ":")
            )
            response.headers["x-benchmark-accepted"] = str(bool(trace and trace.accepted)).lower()
            response.headers["x-benchmark-software"] = SOFTWARE["source_sha256"]
            return response
        finally:
            scope.reset(token)


app.add_middleware(BenchMiddleware)
