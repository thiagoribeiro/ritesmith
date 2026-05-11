from abc import ABC, abstractmethod

from pydantic import BaseModel


class LuaGenerationResponse(BaseModel):
    script: str
    name: str
    description: str
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


class LLMCallStats(BaseModel):
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMProvider(ABC):
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
    ) -> tuple[IntentAnalysis, LLMCallStats]: ...

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
