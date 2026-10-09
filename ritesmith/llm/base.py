from abc import ABC, abstractmethod

from pydantic import BaseModel, PrivateAttr


class LuaGenerationResponse(BaseModel):
    script: str
    name: str
    description: str
    # "Use this capability when…" — a canonical trigger phrasing, distinct from the
    # prose description, used to sharpen reuse recall (FTS) and the reuse judge.
    usage_description: str = ""
    tags: list[str] = []
    risk_assessment: str = "low"
    runtime_profile: str = "transform_only"


class RepairResponse(BaseModel):
    repaired_content: str
    changes_made: str


class IntentAnalysis(BaseModel):
    requires_lua: bool = True
    requires_workflow: bool = False
    requires_network: bool = False
    requires_filesystem: bool = False
    requires_side_effects: bool = False
    domain: str = "general"
    suggested_name: str = "unnamed_capability"
    summary: str = ""
    artifact_types: list[str] = []


class WorkflowGenerationResponse(BaseModel):
    definition: dict
    name: str
    description: str
    required_capabilities: list[str] = []


class WorkflowRepairResponse(BaseModel):
    definition: dict
    changes_made: str


class GenerationProposal(BaseModel):
    analysis: IntentAnalysis
    script: LuaGenerationResponse | None = None
    workflow: WorkflowGenerationResponse | None = None
    # Populated only by the service's independent intent/schema test generator.
    _test_cases: list[dict] | None = PrivateAttr(default=None)
    _tests_attempted: bool = PrivateAttr(default=False)


class LLMCallStats(BaseModel):
    model: str
    resolved_model: str = ""
    reasoning_effort: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_s: float = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    api_attempts: int = 1
    method: str = ""
    fallback: bool = False
    examples: list[str] = []
    usage_known: bool = True
    prompt_version: str = ""
    contract_version: str = ""
    format_version: str = ""
    variant: str = "A"
    response_schema_version: str = ""
    constructor_version: str = ""
    response_mode: str = "json_object"
    schema_fallback_reason: str | None = None
    locally_validated_json: bool = False
    representation: str = ""
    representation_fallback: bool = False


class LLMProvider(ABC):
    # Providers that implement generate_luau/repair_luau set this to True; the
    # GenerationService falls back to Lua for providers that do not.
    supports_luau: bool = False
    supports_proposal: bool = False

    async def generate_validation_tests(
        self, goal, input_schema, output_schema, *, profile="transform_only", fixtures=None
    ):
        # Compatibility for providers that implement the original independent test API.
        return await self.generate_tests(goal, input_schema, output_schema)

    async def propose(self, **kwargs) -> tuple[GenerationProposal, LLMCallStats]:
        raise NotImplementedError("unified proposal not supported")

    def fallback(self) -> "LLMProvider":
        return self

    @abstractmethod
    async def generate_lua(
        self,
        goal: str,
        input_schema: dict | None,
        output_schema: dict | None,
        allowed_host_functions: list[str],
        similar_artifacts: list[dict],
        constraints: dict,
    ) -> tuple[LuaGenerationResponse, LLMCallStats]: ...

    @abstractmethod
    async def repair_lua(
        self,
        original_goal: str,
        current_script: str,
        validation_errors: list[str],
        attempt_number: int,
    ) -> tuple[RepairResponse, LLMCallStats]: ...

    @abstractmethod
    async def analyze_intent(
        self,
        goal: str,
        constraints: dict,
        context: dict | None = None,
    ) -> tuple[IntentAnalysis, LLMCallStats]: ...

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
        raise NotImplementedError("Luau generation not implemented in this provider")

    async def repair_luau(
        self,
        original_goal: str,
        current_script: str,
        validation_errors: list[str],
        attempt_number: int,
        tool_descriptions: list[str],
        type_declarations: str,
    ) -> tuple[RepairResponse, LLMCallStats]:
        raise NotImplementedError("Luau repair not implemented in this provider")

    async def generate_tests(
        self,
        goal: str,
        input_schema: dict | None,
        output_schema: dict | None,
    ) -> tuple[list[dict], LLMCallStats]:
        """Produce sanity test cases ([{input, expected_output}]) from the goal+schemas.

        Derived from the intent, not from any candidate script, so they exercise
        expected behaviour rather than mirroring whatever was generated. These are
        a sanity gate, not a correctness proof — the `expected_output` can be wrong.
        """
        raise NotImplementedError("test generation not implemented in this provider")

    async def judge_reuse(
        self,
        intent: str,
        candidates: list[dict],
    ) -> tuple[int | None, LLMCallStats]:
        """Pick the candidate that actually satisfies the intent, or None.

        `candidates` is an ordered list of already contract-compatible artifacts
        (name, usage_description, description, schemas). Return the 0-based index
        of the best fit, or None if none genuinely matches the intent. Relevance
        only — permissions/risk were already enforced upstream.
        """
        raise NotImplementedError("reuse judge not implemented in this provider")

    async def generate_workflow(
        self,
        goal: str,
        available_capabilities: list[dict],
        constraints: dict,
        similar_workflows: list[dict],
        ritesmith_base_url: str = "http://ritesmith:8081",
        context: dict | None = None,
    ) -> tuple[WorkflowGenerationResponse, LLMCallStats]:
        raise NotImplementedError("workflow generation not implemented in this provider")

    async def repair_workflow(
        self,
        original_goal: str,
        current_definition: dict,
        validation_errors: list[str],
        attempt_number: int,
        available_capability_names: list[str] | None = None,
    ) -> tuple[WorkflowRepairResponse, LLMCallStats]:
        raise NotImplementedError("workflow repair not implemented in this provider")
