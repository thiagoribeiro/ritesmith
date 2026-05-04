import asyncio
import json
import time

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError

from ritesmith.config import Settings
from ritesmith.core.exceptions import LLMError, LLMRateLimitError, LLMTimeoutError
from ritesmith.observability.metrics import llm_errors_total, llm_request_duration, llm_tokens_total
from ritesmith.llm.base import (
    IntentAnalysis,
    LLMCallStats,
    LLMProvider,
    LuaGenerationResponse,
    RepairResponse,
    WorkflowGenerationResponse,
    WorkflowRepairResponse,
)
from ritesmith.llm import prompts

# JSON schemas como string para injetar nos prompts
_LUA_RESPONSE_SCHEMA = json.dumps({
    "script": "<lua code string>",
    "name": "<snake_case_name>",
    "description": "<one line description>",
    "tags": ["<tag1>", "<tag2>"],
    "risk_assessment": "low|medium|high",
    "runtime_profile": "transform_only|readonly_network",
}, indent=2)

_REPAIR_RESPONSE_SCHEMA = json.dumps({
    "repaired_content": "<corrected lua code>",
    "changes_made": "<brief explanation of what was fixed>",
}, indent=2)

_INTENT_SCHEMA = json.dumps({
    "requires_lua": True,
    "requires_workflow": False,
    "requires_network": False,
    "requires_filesystem": False,
    "domain": "text|math|crypto|network|documents|general",
    "suggested_name": "domain.verb_noun",
    "summary": "<one sentence summary>",
    "artifact_types": ["lua_script"],
}, indent=2)

_WORKFLOW_RESPONSE_SCHEMA = json.dumps({
    "definition": {"<workflow definition object>": "..."},
    "name": "<workflow_name>",
    "description": "<description>",
    "required_capabilities": ["<capability_id>"],
}, indent=2)

_WORKFLOW_REPAIR_SCHEMA = json.dumps({
    "definition": {"<corrected workflow definition>": "..."},
    "changes_made": "<brief explanation of what was fixed>",
}, indent=2)

_MAX_RETRIES = 3
_RETRY_BASE_S = 1.0

_MAX_TOKENS: dict[str, int] = {
    "intent":         512,
    "lua_gen":       2048,
    "lua_repair":    2048,
    "workflow_gen":  4096,
    "workflow_repair": 4096,
}
_TEMPERATURE: dict[str, float] = {
    "intent":          0.0,
    "lua_gen":         0.2,
    "lua_repair":      0.0,
    "workflow_gen":    0.2,
    "workflow_repair": 0.0,
}


class OpenAIProvider(LLMProvider):
    def __init__(self, settings: Settings):
        self.client = AsyncOpenAI(timeout=settings.llm_timeout_seconds)
        self.model = settings.llm_model
        self.model_fast = settings.llm_model_fast

    async def _chat_with_retry(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        method: str = "unknown",
    ) -> tuple[str, "LLMCallStats"]:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await self._chat(model, system, user, max_tokens, temperature, method)
            except (LLMRateLimitError, LLMTimeoutError) as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            except LLMError:
                raise
        raise last_exc  # type: ignore[misc]

    async def _chat(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        method: str = "unknown",
    ) -> tuple[str, LLMCallStats]:
        _start = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except APITimeoutError as e:
            llm_errors_total.labels(provider="openai", error_type="LLMTimeoutError").inc()
            raise LLMTimeoutError(f"OpenAI request timed out: {e}") from e
        except RateLimitError as e:
            llm_errors_total.labels(provider="openai", error_type="LLMRateLimitError").inc()
            raise LLMRateLimitError(f"OpenAI rate limit: {e}") from e
        except APIConnectionError as e:
            llm_errors_total.labels(provider="openai", error_type="LLMError").inc()
            raise LLMError(f"OpenAI connection error: {e}") from e

        usage = response.usage
        stats = LLMCallStats(
            model=model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
        )
        elapsed = time.perf_counter() - _start
        llm_request_duration.labels(provider="openai", method=method).observe(elapsed)
        llm_tokens_total.labels(provider="openai", method=method, token_type="prompt").inc(stats.prompt_tokens)
        llm_tokens_total.labels(provider="openai", method=method, token_type="completion").inc(stats.completion_tokens)
        content = response.choices[0].message.content or "{}"
        return content, stats

    async def generate_lua(
        self,
        goal: str,
        input_schema: dict | None,
        output_schema: dict | None,
        allowed_host_functions: list[str],
        similar_artifacts: list[dict],
        constraints: dict,
    ) -> tuple[LuaGenerationResponse, LLMCallStats]:
        user_msg = prompts.lua_generation_user(
            goal=goal,
            input_schema=input_schema,
            output_schema=output_schema,
            allowed_host_functions=allowed_host_functions,
            similar_artifacts=similar_artifacts,
            constraints=constraints,
            response_schema=_LUA_RESPONSE_SCHEMA,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.lua_generation_system(),
            user=user_msg,
            max_tokens=_MAX_TOKENS["lua_gen"],
            temperature=_TEMPERATURE["lua_gen"],
        )
        try:
            data = json.loads(raw)
            return LuaGenerationResponse(**data), stats
        except Exception as e:
            raise LLMError(f"Failed to parse LLM response: {e}\nRaw: {raw[:200]}") from e

    async def repair_lua(
        self,
        original_goal: str,
        current_script: str,
        validation_errors: list[str],
        attempt_number: int,
    ) -> tuple[RepairResponse, LLMCallStats]:
        user_msg = prompts.lua_repair_user(
            original_goal=original_goal,
            current_script=current_script,
            validation_errors=validation_errors,
            attempt_number=attempt_number,
            response_schema=_REPAIR_RESPONSE_SCHEMA,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.lua_repair_system(),
            user=user_msg,
            max_tokens=_MAX_TOKENS["lua_repair"],
            temperature=_TEMPERATURE["lua_repair"],
        )
        try:
            data = json.loads(raw)
            return RepairResponse(**data), stats
        except Exception as e:
            raise LLMError(f"Failed to parse repair response: {e}") from e

    async def analyze_intent(
        self,
        goal: str,
        constraints: dict,
    ) -> tuple[IntentAnalysis, LLMCallStats]:
        user_msg = prompts.intent_analysis_user(
            goal=goal,
            constraints=constraints,
            response_schema=_INTENT_SCHEMA,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model_fast,
            system=prompts.intent_analysis_system(),
            user=user_msg,
            max_tokens=_MAX_TOKENS["intent"],
            temperature=_TEMPERATURE["intent"],
        )
        try:
            data = json.loads(raw)
            return IntentAnalysis(**data), stats
        except Exception as e:
            raise LLMError(f"Failed to parse intent analysis: {e}") from e

    async def generate_workflow(
        self,
        goal: str,
        available_capabilities: list[dict],
        constraints: dict,
        similar_workflows: list[dict],
        ritesmith_base_url: str = "http://ritesmith:8081",
        context: dict | None = None,
    ) -> tuple[WorkflowGenerationResponse, LLMCallStats]:
        user_msg = prompts.workflow_generation_user(
            goal=goal,
            available_capabilities=available_capabilities,
            constraints=constraints,
            similar_workflows=similar_workflows,
            response_schema=_WORKFLOW_RESPONSE_SCHEMA,
            context=context,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.workflow_generation_system(
                ritesmith_base_url=ritesmith_base_url,
            ),
            user=user_msg,
            max_tokens=_MAX_TOKENS["workflow_gen"],
            temperature=_TEMPERATURE["workflow_gen"],
        )
        try:
            data = json.loads(raw)
            return WorkflowGenerationResponse(**data), stats
        except Exception as e:
            raise LLMError(f"Failed to parse workflow response: {e}") from e

    async def repair_workflow(
        self,
        original_goal: str,
        current_definition: dict,
        validation_errors: list[str],
        attempt_number: int,
        available_capability_names: list[str] | None = None,
    ) -> tuple[WorkflowRepairResponse, LLMCallStats]:
        user_msg = prompts.workflow_repair_user(
            original_goal=original_goal,
            current_definition=current_definition,
            validation_errors=validation_errors,
            attempt_number=attempt_number,
            response_schema=_WORKFLOW_REPAIR_SCHEMA,
            available_capability_names=available_capability_names,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.workflow_repair_system(),
            user=user_msg,
            max_tokens=_MAX_TOKENS["workflow_repair"],
            temperature=_TEMPERATURE["workflow_repair"],
        )
        try:
            data = json.loads(raw)
            return WorkflowRepairResponse(**data), stats
        except Exception as e:
            raise LLMError(f"Failed to parse workflow repair response: {e}") from e
