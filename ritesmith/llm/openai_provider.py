import asyncio
import json
import logging
import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from ritesmith.config import Settings
from ritesmith.core.exceptions import LLMError, LLMRateLimitError, LLMTimeoutError
from ritesmith.core.generation_budget import current_budget
from ritesmith.core.repair import claim_recovery, current_recovery
from ritesmith.llm import prompts
from ritesmith.llm.base import (
    GenerationProposal,
    IntentAnalysis,
    LLMCallStats,
    LLMProvider,
    LuaGenerationResponse,
    RepairResponse,
    WorkflowGenerationResponse,
    WorkflowRepairResponse,
)
from ritesmith.llm.generation_context import PROMPT_VERSION, prepare_catalog, proposal_kind
from ritesmith.llm.script_candidate import ASSEMBLY_RULES, expand_script
from ritesmith.observability.generation import current_trace
from ritesmith.observability.metrics import llm_errors_total, llm_request_duration, llm_tokens_total
from ritesmith.workflows.compact import compact_workflow, compile_workflow
from ritesmith.workflows.examples import workflow_system
from ritesmith.workflows.mermaid import compile_mermaid, render_mermaid
from ritesmith.workflows.semantic import compile_plan

# gpt-5*/o* reasoning models reject `temperature` on Chat Completions and want
# `max_completion_tokens` in place of `max_tokens`.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

# JSON schemas como string para injetar nos prompts
_LUA_RESPONSE_SCHEMA = json.dumps(
    {
        "script": "<lua code string>",
        "name": "<snake_case_name>",
        "description": "<one line description>",
        "usage_description": "<'Use this when…' — one line naming the trigger/situation this handles>",
        "tags": ["<tag1>", "<tag2>"],
        "risk_assessment": "low|medium|high",
        "runtime_profile": "transform_only|readonly_network",
    },
    indent=2,
)

_REPAIR_RESPONSE_SCHEMA = json.dumps(
    {
        "repaired_content": "<corrected lua code>",
        "changes_made": "<brief explanation of what was fixed>",
    },
    indent=2,
)

_INTENT_SCHEMA = json.dumps(
    {
        "requires_lua": True,
        "requires_workflow": False,
        "requires_network": False,
        "requires_filesystem": False,
        "requires_side_effects": False,
        "domain": "text|math|crypto|network|documents|general",
        "suggested_name": "domain.verb_noun",
        "summary": "<one sentence summary>",
        "artifact_types": ["lua_script OR trama_workflow"],
    },
    indent=2,
)

_WORKFLOW_RESPONSE_SCHEMA = json.dumps(
    {
        "definition": {"<workflow definition object>": "..."},
        "name": "<workflow_name>",
        "description": "<description>",
        "required_capabilities": ["<capability_id>"],
    },
    indent=2,
)

_WORKFLOW_REPAIR_SCHEMA = json.dumps(
    {
        "definition": {"<corrected workflow definition>": "..."},
        "changes_made": "<brief explanation of what was fixed>",
    },
    indent=2,
)


def _script_schema(schema, assembled):
    if not assembled:
        return schema
    data = json.loads(schema)
    data["script"] = {
        "format": "luau_body",
        "helpers": "<local helper functions>",
        "body": "<run body>",
    }
    return json.dumps(data, separators=(",", ":"))


def _workflow_schema(schema: str, mermaid: bool) -> str:
    if not mermaid:
        return schema
    data = json.loads(schema)
    data["definition"] = "flowchart TD\n%% workflow {...}\n%% node ...\n..."
    return json.dumps(data, separators=(",", ":"))


_MAX_RETRIES = 3
_RETRY_BASE_S = 1.0

# Budgets are max_completion_tokens. Reasoning models (gpt-5*) spend a large,
# variable share of this on hidden reasoning before the JSON, so the generation
# budgets carry roughly 2-3x headroom over the largest expected artifact —
# otherwise the workflow JSON is silently truncated and fails to parse.
_MAX_TOKENS: dict[str, int] = {
    "intent": 1024,
    "lua_gen": 6000,
    "lua_repair": 6000,
    "workflow_gen": 8000,
    "workflow_repair": 8000,
    "reuse_judge": 256,
    "test_gen": 1500,
}
_TEMPERATURE: dict[str, float] = {
    "intent": 0.0,
    "lua_gen": 0.2,
    "lua_repair": 0.0,
    "workflow_gen": 0.2,
    "workflow_repair": 0.0,
    "reuse_judge": 0.0,
    "test_gen": 0.0,
}


class OpenAIProvider(LLMProvider):
    supports_luau = True
    supports_proposal = True

    def __init__(
        self,
        settings: Settings,
        *,
        client=None,
        use_fallback: bool = False,
        lazy_client: bool = False,
    ):
        self.settings = settings
        self._client = client
        if client is None and not lazy_client:
            self._client = AsyncOpenAI(timeout=settings.llm_timeout_seconds, max_retries=0)
        self.model = settings.llm_model
        self.model_fast = settings.llm_model_fast
        self.use_fallback = use_fallback

    @property
    def client(self):
        if self._client is None:
            self._client = AsyncOpenAI(timeout=self.settings.llm_timeout_seconds, max_retries=0)
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.close()

    def fallback(self):
        if self.use_fallback or not self.settings.llm_fallback_enabled:
            return self
        return OpenAIProvider(self.settings, client=self.client, use_fallback=True)

    @staticmethod
    def _examples(stats, selected, contract_version="", format_version=""):
        stats.examples = selected
        stats.contract_version = contract_version
        stats.format_version = format_version
        trace = current_trace.get()
        if trace is not None:
            for call in reversed(trace.calls):
                if (
                    call.get("duration_s") == stats.duration_s
                    and call.get("method") == stats.method
                ):
                    call.update(
                        examples=selected,
                        contract_version=contract_version,
                        format_version=format_version,
                    )
                    break

    @staticmethod
    def _invalid_response(stats, exc):
        trace = current_trace.get()
        if trace is not None:
            for call in reversed(trace.calls):
                if (
                    call.get("duration_s") == stats.duration_s
                    and call.get("method") == stats.method
                ):
                    call.update(
                        error="invalid_response",
                        error_type=type(exc).__name__,
                        category="response"
                        if isinstance(exc, json.JSONDecodeError)
                        else "graph"
                        if isinstance(exc, ValueError)
                        else "response",
                        location=getattr(exc, "line", getattr(exc, "lineno", None)),
                    )
                    break

    def operation_config(self, model: str, method: str) -> tuple[str, str | None]:
        if method in ("intent", "reuse_judge", "test_gen", "llm.evaluate"):
            return model, None
        workflow = method.startswith("workflow") or method in ("proposal", "proposal_workflow")
        effort = (
            self.settings.llm_workflow_effort if workflow else self.settings.llm_script_effort
        ) or self.settings.llm_reasoning_effort
        if self.use_fallback:
            return self.model, effort
        if workflow:
            return (
                self.settings.llm_workflow_model or model,
                effort,
            )
        return (
            self.settings.llm_script_model or model,
            effort,
        )

    async def _chat_with_retry(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        method: str = "unknown",
        *,
        examples: list[str] | None = None,
    ) -> tuple[str, "LLMCallStats"]:
        last_exc: Exception | None = None
        started = time.perf_counter()
        model, _ = self.operation_config(model, method)
        max_retries = 2 if self.settings.generation_bounded_recovery else _MAX_RETRIES
        for attempt in range(1, max_retries + 1):
            try:
                raw, stats = await self._chat(model, system, user, max_tokens, temperature, method)
                stats.duration_s = time.perf_counter() - started
                allowance = current_recovery.get()
                if allowance is not None:
                    allowance.expected_seconds = max(allowance.expected_seconds, stats.duration_s)
                budget = current_budget.get()
                if budget is not None:
                    operation = "proposal" if method.startswith("proposal") else method
                    budget.observed_seconds[operation] = stats.duration_s
                stats.api_attempts = attempt
                stats.method = method
                stats.fallback = self.use_fallback
                stats.examples = list(examples or [])
                trace = current_trace.get()
                if trace is not None:
                    trace.calls.append(
                        {**stats.model_dump(), "api_attempts": 1, "operation_api_attempts": attempt}
                    )
                    trace.fallback |= stats.fallback
                logging.getLogger(__name__).info(
                    "llm call method=%s model=%s duration=%.3fs attempts=%s prompt=%s completion=%s reasoning=%s cached=%s fallback=%s",
                    method,
                    model,
                    stats.duration_s,
                    attempt,
                    stats.prompt_tokens,
                    stats.completion_tokens,
                    stats.reasoning_tokens,
                    stats.cached_tokens,
                    stats.fallback,
                )
                return raw, stats
            except (LLMRateLimitError, LLMTimeoutError) as e:
                last_exc = e
                if attempt < max_retries and (
                    not self.settings.generation_bounded_recovery
                    or (
                        method != "test_gen"
                        and claim_recovery(
                            "proposal" if method.startswith("proposal") else method,
                            self.settings.generation_recovery_attempts,
                        )
                    )
                ):
                    trace = current_trace.get()
                    if trace is not None:
                        trace.calls.append(
                            {
                                "method": method,
                                "model": model,
                                "api_attempts": 1,
                                "error": type(e).__name__,
                                "fallback": self.use_fallback,
                                "duration_s": time.perf_counter() - started,
                                "cost_known": False,
                            }
                        )
                    if not self.settings.generation_bounded_recovery:
                        await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
                else:
                    break
            except LLMError as e:
                last_exc = e
                break
            except asyncio.CancelledError:
                trace = current_trace.get()
                if trace is not None:
                    trace.calls.append(
                        {
                            "method": method,
                            "model": model,
                            "duration_s": time.perf_counter() - started,
                            "api_attempts": 1,
                            "cancelled": True,
                            "fallback": self.use_fallback,
                            "examples": list(examples or []),
                        }
                    )
                raise
        trace = current_trace.get()
        if trace is not None:
            trace.calls.append(
                {
                    **(getattr(last_exc, "details", {}).get("stats") or {}),
                    "method": method,
                    "model": model,
                    "duration_s": time.perf_counter() - started,
                    "api_attempts": 1,
                    "operation_api_attempts": attempt,
                    "error": type(last_exc).__name__,
                    "provider_code": getattr(last_exc, "details", {}).get("provider_code"),
                    "fallback": self.use_fallback,
                    "examples": list(examples or []),
                }
            )
            trace.fallback |= self.use_fallback
        if last_exc is not None:
            last_exc.details["api_attempts"] = attempt
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
        system += "\nWrite code comments and documentation in English. Preserve explicitly requested message text and data.\n"
        try:
            create_kwargs: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }
            if model.startswith(_REASONING_PREFIXES):
                create_kwargs["max_completion_tokens"] = max_tokens
                _, effort = self.operation_config(model, method)
                if effort is not None:
                    create_kwargs["reasoning_effort"] = effort
            else:
                create_kwargs["temperature"] = temperature
                create_kwargs["max_tokens"] = max_tokens
            budget = current_budget.get()
            if budget is not None:
                create_kwargs["timeout"] = max(
                    0.01, min(self.settings.llm_timeout_seconds, budget.remaining)
                )
            response = await self.client.chat.completions.create(**create_kwargs)
        except APITimeoutError as e:
            llm_errors_total.labels(provider="openai", error_type="LLMTimeoutError").inc()
            raise LLMTimeoutError(
                f"OpenAI request timed out: {e}", details={"category": "transport"}
            ) from e
        except RateLimitError as e:
            if e.type == "insufficient_quota" or e.code in (
                "insufficient_quota",
                "credit_balance_exhausted",
            ):
                llm_errors_total.labels(provider="openai", error_type="quota_exhausted").inc()
                raise LLMError(
                    "OpenAI account quota exhausted",
                    details={
                        "retryable": False,
                        "provider_code": e.code or e.type,
                        "category": "quota",
                    },
                ) from e
            llm_errors_total.labels(provider="openai", error_type="LLMRateLimitError").inc()
            raise LLMRateLimitError(
                f"OpenAI rate limit: {e}",
                details={"provider_code": e.code, "category": "rate_limit"},
            ) from e
        except APIConnectionError as e:
            llm_errors_total.labels(provider="openai", error_type="LLMError").inc()
            raise LLMTimeoutError(
                f"OpenAI connection error: {e}", details={"category": "transport"}
            ) from e

        except APIStatusError as e:
            llm_errors_total.labels(provider="openai", error_type="LLMError").inc()
            raise (LLMTimeoutError if e.status_code >= 500 else LLMError)(
                f"OpenAI HTTP status {e.status_code}",
                details={"category": "transport", "retryable": e.status_code >= 500},
            ) from e

        usage = response.usage
        stats = LLMCallStats(
            model=model,
            resolved_model=response.model,
            usage_known=usage is not None,
            reasoning_effort=self.operation_config(model, method)[1],
            prompt_version=PROMPT_VERSION,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            reasoning_tokens=getattr(
                getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0
            )
            or 0,
            cached_tokens=getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0)
            or 0,
        )
        elapsed = time.perf_counter() - _start
        llm_request_duration.labels(provider="openai", method=method).observe(elapsed)
        llm_tokens_total.labels(provider="openai", method=method, token_type="prompt").inc(
            stats.prompt_tokens
        )
        llm_tokens_total.labels(provider="openai", method=method, token_type="completion").inc(
            stats.completion_tokens
        )
        if response.choices[0].finish_reason == "length":
            raise LLMError(
                "LLM completion truncated (finish_reason=length)",
                details={"stats": stats.model_dump(), "category": "truncated"},
            )
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
            method="lua_gen",
        )
        try:
            data = json.loads(raw)
            self._examples(
                stats,
                [],
                format_version="luau-body-v1"
                if isinstance(data.get("script"), dict)
                else "script-full-v1",
            )
            return LuaGenerationResponse(**expand_script(data)), stats
        except Exception as e:
            self._invalid_response(stats, e)
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
            method="lua_repair",
        )
        try:
            data = json.loads(raw)
            return RepairResponse(**data), stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse repair response: {e}") from e

    async def generate_luau(
        self,
        goal: str,
        input_schema: dict | None,
        output_schema: dict | None,
        tool_descriptions: list[str],
        type_declarations: str,
        similar_artifacts: list[dict],
        constraints: dict,
    ) -> tuple[LuaGenerationResponse, LLMCallStats]:
        user_msg = prompts.luau_generation_user(
            goal=goal,
            input_schema=input_schema,
            output_schema=output_schema,
            tool_descriptions=tool_descriptions,
            type_declarations=type_declarations,
            similar_artifacts=similar_artifacts,
            constraints=constraints,
            response_schema=_script_schema(
                _LUA_RESPONSE_SCHEMA, self.settings.generation_luau_assembly
            ),
        )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.luau_generation_system()
            + (ASSEMBLY_RULES if self.settings.generation_luau_assembly else ""),
            user=user_msg,
            max_tokens=_MAX_TOKENS["lua_gen"],
            temperature=_TEMPERATURE["lua_gen"],
            method="luau_gen",
        )
        try:
            data = json.loads(raw)
            self._examples(
                stats,
                [],
                format_version="luau-body-v1"
                if isinstance(data.get("script"), dict)
                else "script-full-v1",
            )
            return LuaGenerationResponse(**expand_script(data)), stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse LLM response: {e}\nRaw: {raw[:200]}") from e

    async def repair_luau(
        self,
        original_goal: str,
        current_script: str,
        validation_errors: list[str],
        attempt_number: int,
        tool_descriptions: list[str],
        type_declarations: str,
    ) -> tuple[RepairResponse, LLMCallStats]:
        user_msg = prompts.luau_repair_user(
            original_goal=original_goal,
            current_script=current_script,
            validation_errors=validation_errors,
            attempt_number=attempt_number,
            tool_descriptions=tool_descriptions,
            type_declarations=type_declarations,
            response_schema=_REPAIR_RESPONSE_SCHEMA,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.luau_repair_system(),
            user=user_msg,
            max_tokens=_MAX_TOKENS["lua_repair"],
            temperature=_TEMPERATURE["lua_repair"],
            method="luau_repair",
        )
        try:
            data = json.loads(raw)
            return RepairResponse(**data), stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse repair response: {e}") from e

    async def analyze_intent(
        self,
        goal: str,
        constraints: dict,
        context: dict | None = None,
    ) -> tuple[IntentAnalysis, LLMCallStats]:
        user_msg = prompts.intent_analysis_user(
            goal=goal,
            constraints=constraints,
            response_schema=_INTENT_SCHEMA,
            context=context,
        )
        raw, stats = await self._chat_with_retry(
            model=self.model_fast,
            system=prompts.intent_analysis_system(),
            user=user_msg,
            max_tokens=_MAX_TOKENS["intent"],
            temperature=_TEMPERATURE["intent"],
            method="intent",
        )
        try:
            data = json.loads(raw)
            return IntentAnalysis(**data), stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse intent analysis: {e}") from e

    async def generate_validation_tests(
        self, goal, input_schema, output_schema, *, profile="transform_only", fixtures=None
    ):
        from ritesmith.runtime.luau import luau_tools_for_profile
        from ritesmith.schemas.test_spec import TestSpec

        contracts = [
            {
                "name": name,
                "input_schema": definition.input_schema,
                "output_schema": definition.output_schema,
            }
            for name, definition in luau_tools_for_profile(profile).items()
        ]
        user = prompts.test_generation_user(goal, input_schema, output_schema)
        user += "\nTOOL CONTRACTS: " + json.dumps(contracts, separators=(",", ":"))
        user += "\nKNOWN FIXTURES: " + json.dumps(fixtures or [], separators=(",", ":"))
        system = (
            prompts.test_generation_system()
            + """
Each case may include context, tool_fixtures [{tool,args,output,times}],
expected_calls {tool:count}, and assertions [{path,op,value}] (equals,length,max_length,contains,subset).
For external data, define controlled tool responses BEFORE deriving exact expected_output.
Use supplied fixtures when present. Otherwise construct synthetic schema-valid fixtures.
Cover normal behavior, boundaries and tool failure propagation; never assume live external data.
Every external call must have an exact-argument fixture. Expected IDs must exist in fixtures.
Cases without expected output or functional assertions are execution smoke checks only.
Return source="intent". The implementation is intentionally unavailable.
"""
        )
        raw, stats = await self._chat_with_retry(self.model_fast, system, user, 4500, 0, "test_gen")
        try:
            data = json.loads(raw)
            cases = [
                TestSpec.model_validate({**case, "source": "intent"}).model_dump()
                for case in data.get("test_cases", [])
            ]
            return cases, stats
        except Exception as exc:
            self._invalid_response(stats, exc)
            raise LLMError(
                "Invalid independent test specification",
                details={
                    "category": "fixture",
                    "retryable": False,
                },
            ) from exc

    async def generate_tests(
        self,
        goal: str,
        input_schema: dict | None,
        output_schema: dict | None,
    ) -> tuple[list[dict], LLMCallStats]:
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=prompts.test_generation_system(),
            user=prompts.test_generation_user(goal, input_schema, output_schema),
            max_tokens=_MAX_TOKENS["test_gen"],
            temperature=_TEMPERATURE["test_gen"],
            method="test_gen",
        )
        try:
            data = json.loads(raw)
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse generated tests: {e}") from e
        cases = data.get("test_cases", [])
        if not isinstance(cases, list):
            return [], stats
        valid = [
            c for c in cases if isinstance(c, dict) and "input" in c and "expected_output" in c
        ]
        return valid, stats

    async def judge_reuse(
        self,
        intent: str,
        candidates: list[dict],
    ) -> tuple[int | None, LLMCallStats]:
        raw, stats = await self._chat_with_retry(
            model=self.model_fast,
            system=prompts.reuse_judge_system(),
            user=prompts.reuse_judge_user(intent, candidates),
            max_tokens=_MAX_TOKENS["reuse_judge"],
            temperature=_TEMPERATURE["reuse_judge"],
            method="reuse_judge",
        )
        try:
            data = json.loads(raw)
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse reuse judge response: {e}") from e
        choice = data.get("choice")
        if not isinstance(choice, int) or not (0 <= choice < len(candidates)):
            return None, stats
        return choice, stats

    def _compile_definition(
        self,
        definition,
        base_url,
        contract_version="",
        *,
        goal=None,
        constraints=None,
        context=None,
    ):
        if isinstance(definition, dict) and definition.get("format") == "semantic_plan":
            if not self.settings.generation_semantic_workflows:
                raise ValueError("semantic workflows disabled")
            return compile_plan(
                definition["plan"],
                base_url,
                contract_version=contract_version,
                intent=goal,
                constraints=constraints,
                continuation=(context or {}).get("continuation"),
            )
        if isinstance(definition, dict) and definition.get("format") == "compact_graph":
            return compile_workflow(definition["graph"], base_url)
        if self.settings.generation_mermaid_workflows:
            return compile_mermaid(definition, base_url)
        if self.settings.generation_compact_workflows:
            return compile_workflow(definition, base_url)
        return definition

    async def generate_workflow(
        self,
        goal: str,
        available_capabilities: list[dict],
        constraints: dict,
        similar_workflows: list[dict],
        ritesmith_base_url: str = "http://ritesmith:8081",
        context: dict | None = None,
    ) -> tuple[WorkflowGenerationResponse, LLMCallStats]:
        catalog = prepare_catalog(
            goal, available_capabilities, constraints.get("required_capabilities", [])
        )
        available_capabilities = catalog.contracts
        user_msg = prompts.workflow_generation_user(
            goal=goal,
            available_capabilities=available_capabilities,
            constraints=constraints,
            similar_workflows=similar_workflows,
            response_schema=_workflow_schema(
                _WORKFLOW_RESPONSE_SCHEMA, self.settings.generation_mermaid_workflows
            ),
            context=context,
        )
        user_msg += "\nCAPABILITY INVENTORY: " + json.dumps(
            catalog.inventory, separators=(",", ":")
        )
        system = prompts.workflow_generation_system(ritesmith_base_url=ritesmith_base_url)
        selected = []
        if (
            self.settings.generation_specialized_prompts
            or self.settings.generation_compact_workflows
            or self.settings.generation_mermaid_workflows
            or self.settings.generation_semantic_workflows
            or "workflow_examples" in (context or {})
        ):
            system, selected = workflow_system(
                goal,
                context,
                ritesmith_base_url,
                compact=self.settings.generation_compact_workflows,
                filtered=self.settings.generation_specialized_prompts,
                mermaid=self.settings.generation_mermaid_workflows,
                semantic=self.settings.generation_semantic_workflows,
            )
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=system,
            user=user_msg,
            max_tokens=_MAX_TOKENS["workflow_gen"],
            temperature=_TEMPERATURE["workflow_gen"],
            method="workflow_gen",
            examples=selected,
        )
        self._examples(
            stats,
            selected,
            catalog.version,
            "workflow-plan-v1" if self.settings.generation_semantic_workflows else "compact-v1",
        )
        try:
            data = json.loads(raw)
            data["definition"] = self._compile_definition(
                data["definition"],
                ritesmith_base_url,
                catalog.version,
                goal=goal,
                constraints=constraints,
                context=context,
            )
            return WorkflowGenerationResponse(**data), stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(
                f"Failed to parse workflow response: {e}",
                details={
                    "category": "graph",
                    "candidate": locals().get("data", {}).get("definition"),
                    "location": getattr(e, "line", None),
                    "node": getattr(e, "node", None),
                    "edge": getattr(e, "edge", None),
                },
            ) from e

    async def repair_workflow(
        self,
        original_goal: str,
        current_definition: dict,
        validation_errors: list[str],
        attempt_number: int,
        available_capability_names: list[str] | None = None,
    ) -> tuple[WorkflowRepairResponse, LLMCallStats]:
        base_url = self.settings.public_url or "http://ritesmith:8081"
        if self.settings.generation_mermaid_workflows and isinstance(current_definition, dict):
            try:
                current_definition = render_mermaid(compact_workflow(current_definition, base_url))
            except ValueError:
                # Keep an invalid candidate as repair evidence; output is Mermaid.
                pass
        elif self.settings.generation_compact_workflows and isinstance(current_definition, dict):
            current_definition = compact_workflow(current_definition, base_url)
        user_msg = prompts.workflow_repair_user(
            original_goal=original_goal,
            current_definition=current_definition,
            validation_errors=validation_errors,
            attempt_number=attempt_number,
            response_schema=_workflow_schema(
                _WORKFLOW_REPAIR_SCHEMA, self.settings.generation_mermaid_workflows
            ),
            available_capability_names=available_capability_names,
        )
        system = prompts.workflow_repair_system()
        if (
            self.settings.generation_compact_workflows
            or self.settings.generation_mermaid_workflows
            or self.settings.generation_semantic_workflows
        ):
            system, _ = workflow_system(
                original_goal,
                {"workflow_examples": []},
                base_url,
                compact=True,
                filtered=True,
                mermaid=self.settings.generation_mermaid_workflows,
                semantic=self.settings.generation_semantic_workflows,
            )
            system += "\nRepair the definition from the errors; return definition and changes_made."
        raw, stats = await self._chat_with_retry(
            model=self.model,
            system=system,
            user=user_msg,
            max_tokens=_MAX_TOKENS["workflow_repair"],
            temperature=_TEMPERATURE["workflow_repair"],
            method="workflow_repair",
        )
        try:
            data = json.loads(raw)
            data["definition"] = self._compile_definition(
                data["definition"], base_url, goal=original_goal
            )
            return WorkflowRepairResponse(**data), stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(
                f"Failed to parse workflow repair response: {e}",
                details={
                    "category": "graph",
                    "candidate": locals().get("data", {}).get("definition"),
                    "location": getattr(e, "line", None),
                    "node": getattr(e, "node", None),
                    "edge": getattr(e, "edge", None),
                },
            ) from e

    async def propose(
        self,
        *,
        goal,
        constraints,
        context=None,
        input_schema=None,
        output_schema=None,
        language="luau",
        profile="transform_only",
        available_capabilities=None,
        similar_artifacts=None,
        allow_dependencies=True,
    ):
        from ritesmith.runtime.host_functions import list_names_for_profile
        from ritesmith.runtime.luau import describe_tools_for_prompt, script_type_declarations

        base_url = self.settings.public_url or "http://ritesmith:8081"
        kind = (
            proposal_kind(goal, input_schema, output_schema)
            if self.settings.generation_typed_prompts
            else "mixed"
        )
        selected = []
        catalog = prepare_catalog(
            goal, available_capabilities or [], constraints.get("required_capabilities", [])
        )
        system_parts = []
        user_parts = [
            "GOAL: " + goal,
            "CONSTRAINTS: " + json.dumps(constraints, separators=(",", ":")),
        ]
        if kind != "script":
            wf_system, selected = workflow_system(
                goal,
                context,
                base_url,
                compact=self.settings.generation_compact_workflows,
                filtered=self.settings.generation_specialized_prompts,
                mermaid=self.settings.generation_mermaid_workflows,
                semantic=self.settings.generation_semantic_workflows,
            )
            system_parts.append(wf_system)
            user_parts.append(
                "CAPABILITY INVENTORY: " + json.dumps(catalog.inventory, separators=(",", ":"))
            )
            user_parts.append(
                prompts.workflow_generation_user(
                    goal,
                    catalog.contracts,
                    {},
                    [],
                    _workflow_schema(
                        _WORKFLOW_RESPONSE_SCHEMA, self.settings.generation_mermaid_workflows
                    ),
                    context,
                )
            )
        if kind != "workflow":
            system_parts.append(
                prompts.luau_generation_system()
                if language == "luau"
                else prompts.lua_generation_system()
            )
            if language == "luau" and self.settings.generation_luau_assembly:
                system_parts.append(ASSEMBLY_RULES)
            user_parts.append(
                "SCRIPT CONTRACTS: "
                + json.dumps(
                    {
                        "input_schema": input_schema,
                        "output_schema": output_schema,
                        "runtime_profile": profile,
                    },
                    separators=(",", ":"),
                )
            )
            if language == "luau":
                user_parts.append(script_type_declarations(input_schema, output_schema))
                user_parts.append("SCRIPT TOOLS:\n" + "\n".join(describe_tools_for_prompt(profile)))
            else:
                user_parts.append("SCRIPT TOOLS: " + json.dumps(list_names_for_profile(profile)))
        if similar_artifacts:
            filtered = [
                artifact
                for artifact in similar_artifacts
                if kind == "mixed" or artifact.get("kind", kind) == kind
            ]
            user_parts.append(
                "SIMILAR ARTIFACTS: " + json.dumps(filtered[:1], separators=(",", ":"))
            )
        # Explicit request context is never discarded when selecting a prompt family.
        if context and kind == "script":
            user_parts.append(
                "CONTEXT: "
                + json.dumps(
                    {k: v for k, v in context.items() if k not in ("test_cases", "test_fixtures")},
                    separators=(",", ":"),
                )
            )
        schemas = {"analysis": json.loads(_INTENT_SCHEMA)}
        if kind != "workflow":
            schemas["script"] = json.loads(
                _script_schema(
                    _LUA_RESPONSE_SCHEMA,
                    language == "luau" and self.settings.generation_luau_assembly,
                )
            )
        if kind != "script":
            schemas["workflow"] = json.loads(
                _workflow_schema(
                    _WORKFLOW_RESPONSE_SCHEMA, self.settings.generation_mermaid_workflows
                )
            )
        user_parts.append(
            "Analyse intent and produce candidate(s) in this SAME response. Response shape: "
            + json.dumps(schemas, separators=(",", ":"))
            + ". Set unneeded candidates to null."
        )
        if allow_dependencies:
            user_parts.append(
                "If both a script and workflow are required, use the script's exact name as capability_name; it is resolved after validation."
            )
        else:
            user_parts.append(
                "Return ONE artifact only, script OR workflow. Use existing dependencies only."
            )
        system = "\n".join(system_parts)
        user = "\n".join(user_parts)
        method = "proposal_script" if kind == "script" else "proposal_workflow"
        raw, stats = await self._chat_with_retry(
            self.model, system, user, 10000, 0.2, method, examples=selected
        )
        self._examples(
            stats,
            selected,
            catalog.version,
            "workflow-plan-v1" if self.settings.generation_semantic_workflows else "compact-v1",
        )
        try:
            data = json.loads(raw)
            if data.get("script"):
                data["script"] = expand_script(data["script"])
            if data.get("workflow"):
                data["workflow"]["definition"] = self._compile_definition(
                    data["workflow"]["definition"],
                    base_url,
                    catalog.version,
                    goal=goal,
                    constraints=constraints,
                    context=context,
                )
            proposal = GenerationProposal.model_validate(data)
            if (
                not allow_dependencies
                and proposal.script is not None
                and proposal.workflow is not None
            ):
                raise ValueError(
                    "single-artifact interface cannot materialize a new script dependency"
                )
            types = proposal.analysis.artifact_types
            if not types:
                types = (
                    ["lua_script" if language == "lua" else "luau_script"]
                    if proposal.script
                    else []
                ) + (["trama_workflow"] if proposal.workflow else [])
                proposal.analysis.artifact_types = types
            if not types or any(
                t not in ("lua_script", "luau_script", "trama_workflow") for t in types
            ):
                raise ValueError("proposal must contain supported artifact candidates")
            proposal.analysis.requires_workflow = "trama_workflow" in types
            proposal.analysis.requires_lua = any(t in ("lua_script", "luau_script") for t in types)
            if (
                proposal.analysis.requires_workflow or "trama_workflow" in types
            ) and proposal.workflow is None:
                raise ValueError("proposal missing workflow")
            if (
                not proposal.analysis.requires_workflow
                or any(t in ("lua_script", "luau_script") for t in types)
            ) and proposal.script is None:
                raise ValueError("proposal missing script")
            return proposal, stats
        except Exception as e:
            self._invalid_response(stats, e)
            raise LLMError(f"Failed to parse unified proposal: {e}") from e
