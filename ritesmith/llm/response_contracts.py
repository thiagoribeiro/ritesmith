"""Provider-independent response contracts and lossless JSON transport fields.

JSON strings carry open client data, not provider-validated business contracts.
All decoded values still pass through the existing local validators.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ritesmith.llm.base import IntentAnalysis, LuaGenerationResponse, RepairResponse
from ritesmith.llm.script_candidate import LuauBody
from ritesmith.schemas.test_spec import TestSpec

SCHEMA_VERSION = "typed-response-v2"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def json_value(value: str):
    def invalid(value):
        raise ValueError(f"Non-JSON numeric constant: {value}")

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(value, parse_constant=invalid, object_pairs_hook=unique)


def packed(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Samples(ClosedModel):
    kind: Literal["samples"]
    count: int = Field(ge=1, le=1000000)


class Duration(ClosedModel):
    kind: Literal["duration"]
    seconds: float = Field(gt=0)


class Deadline(ClosedModel):
    kind: Literal["deadline"]
    unix: float = Field(gt=0)


class Continuous(ClosedModel):
    kind: Literal["continuous"]


Termination = Samples | Duration | Deadline | Continuous


def termination_fields(value):
    if isinstance(value, Samples):
        return {"count": value.count}
    if isinstance(value, Duration):
        return {"duration_seconds": value.seconds}
    if isinstance(value, Deadline):
        return {"deadline_unix": value.unix}
    return {"continuous": True}


class Call(ClosedModel):
    kind: Literal["call", "state"]
    id: str | None = None
    capability_name: str | None = None
    artifact_id: str | None = None
    args_json: str = "{}"
    action_json: str | None = None
    compensation_json: str | None = None


class Sequence(ClosedModel):
    kind: Literal["sequence"]
    id: str | None = None
    steps: list[Operation]


class Wait(ClosedModel):
    kind: Literal["wait"]
    id: str | None = None
    seconds: float = Field(gt=0)


class Condition(ClosedModel):
    kind: Literal["condition"]
    id: str | None = None
    when_json: str
    steps: list[Operation]
    otherwise: list[Operation] = Field(default_factory=list)


class Repeat(ClosedModel):
    kind: Literal["repeat"]
    id: str
    interval_seconds: float = Field(gt=0)
    termination: Termination
    steps: list[Operation] = Field(min_length=1)
    state_json: str | None = None
    initial_state_json: str | None = None


class Parallel(ClosedModel):
    kind: Literal["parallel"]
    id: str | None = None
    branches: list[Operation] = Field(min_length=2)


class Callback(ClosedModel):
    kind: Literal["callback"]
    id: str | None = None
    action_json: str
    compensation_json: str | None = None


Operation = Call | Sequence | Wait | Condition | Repeat | Parallel | Callback
for _model in (Sequence, Condition, Repeat, Parallel):
    _model.model_rebuild()


def operation_value(operation):
    result = operation.model_dump(exclude_none=True)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json_value(result.pop(key))
    for key in ("steps", "branches", "otherwise"):
        if hasattr(operation, key):
            result[key] = [operation_value(item) for item in getattr(operation, key)]
    if isinstance(operation, Repeat):
        result.pop("termination")
        result.update(termination_fields(operation.termination))
    return result


def operation_transport(operation):
    result = dict(operation)
    for key in ("args", "when", "state", "initial_state", "action", "compensation"):
        if key in result:
            result[key + "_json"] = packed(result.pop(key))
    for key in ("steps", "branches", "otherwise"):
        if key in result:
            result[key] = [operation_transport(item) for item in result[key]]
    if result["kind"] == "repeat":
        rules = [
            ("count", "samples", "count"),
            ("duration_seconds", "duration", "seconds"),
            ("deadline_unix", "deadline", "unix"),
        ]
        termination = [
            {"kind": kind, target: result.pop(source)}
            for source, kind, target in rules
            if source in result
        ]
        if result.pop("continuous", False):
            termination.append({"kind": "continuous"})
        if len(termination) != 1:
            raise ValueError("repeat requires exactly one termination")
        result["termination"] = termination[0]
    return result


class Plan(ClosedModel):
    version: Literal["workflow-plan-v1"] = "workflow-plan-v1"
    name: str
    steps: list[Operation] = Field(min_length=1)
    failure_handling_json: str | None = None

    def domain(self):
        result = {
            "version": self.version,
            "name": self.name,
            "steps": [operation_value(item) for item in self.steps],
        }
        if self.failure_handling_json is not None:
            result["failureHandling"] = json_value(self.failure_handling_json)
        return result


class SemanticCandidate(ClosedModel):
    format: Literal["semantic_plan"]
    plan: Plan


class CompactCandidate(ClosedModel):
    format: Literal["compact_graph"]
    graph_json: str


class NativeCandidate(ClosedModel):
    format: Literal["native_graph"]
    definition_json: str


class LinearPattern(ClosedModel):
    pattern: Literal["linear"]
    steps: list[Operation] = Field(min_length=1)


class RepeatPattern(ClosedModel):
    pattern: Literal[
        "bounded_polling", "fixed_samples", "continuation", "state_tracking", "content_monitor"
    ]
    interval_seconds: float = Field(gt=0)
    termination: Termination
    steps: list[Operation] = Field(min_length=1)
    state_json: str | None = None
    initial_state_json: str | None = None
    after: list[Operation] = Field(default_factory=list)


class ParallelPattern(ClosedModel):
    pattern: Literal["parallel"]
    branches: list[Operation] = Field(min_length=2)
    after: list[Operation] = Field(default_factory=list)


class CallbackPattern(ClosedModel):
    pattern: Literal["async_callback"]
    action_json: str
    after: list[Operation] = Field(default_factory=list)


class CompensationPattern(ClosedModel):
    pattern: Literal["compensation"]
    steps: list[Operation] = Field(min_length=1)
    compensation_json: str


Pattern = LinearPattern | RepeatPattern | ParallelPattern | CallbackPattern | CompensationPattern


class PatternCandidate(ClosedModel):
    format: Literal["pattern_parameters"]
    name: str
    parameters: Pattern

    def domain(self):
        parameters = self.parameters
        result = {
            "format": self.format,
            "name": self.name,
            "parameters": parameters.model_dump(exclude_none=True),
        }
        for key in ("steps", "branches", "after"):
            if hasattr(parameters, key):
                result["parameters"][key] = [
                    operation_value(item) for item in getattr(parameters, key)
                ]
        for key in ("state_json", "initial_state_json", "action_json", "compensation_json"):
            if getattr(parameters, key, None) is not None:
                result["parameters"][key[:-5]] = json_value(result["parameters"].pop(key))
        return result


Definition = SemanticCandidate | CompactCandidate | NativeCandidate


def definition_value(candidate):
    if isinstance(candidate, SemanticCandidate):
        return {"format": candidate.format, "plan": candidate.plan.domain()}
    if isinstance(candidate, PatternCandidate):
        return candidate.domain()
    if isinstance(candidate, CompactCandidate):
        return {"format": candidate.format, "graph": json_value(candidate.graph_json)}
    return json_value(candidate.definition_json)


class ScriptResponse(LuaGenerationResponse):
    model_config = ConfigDict(extra="forbid")
    script: str | LuauBody


class AnalysisResponse(IntentAnalysis):
    model_config = ConfigDict(extra="forbid")


class ScriptRepairResponse(RepairResponse):
    model_config = ConfigDict(extra="forbid")


class WorkflowResponse(ClosedModel):
    definition: Definition
    name: str
    description: str
    required_capabilities: list[str] = Field(default_factory=list)


class PatternWorkflowResponse(WorkflowResponse):
    definition: Definition | PatternCandidate


class ProposalResponse(ClosedModel):
    analysis: AnalysisResponse
    script: ScriptResponse | None = None
    workflow: WorkflowResponse | None = None


class PatternProposalResponse(ProposalResponse):
    workflow: PatternWorkflowResponse | None = None


class WorkflowRepair(ClosedModel):
    definition: Definition
    changes_made: str


class PatternWorkflowRepair(WorkflowRepair):
    definition: Definition | PatternCandidate


class Fixture(ClosedModel):
    tool: str
    args_json: str
    output_json: str
    times: int = Field(default=1, ge=1, le=100)


class Assertion(ClosedModel):
    path: str
    op: Literal["equals", "length", "max_length", "contains", "subset"]
    value_json: str


class TestCase(ClosedModel):
    input_json: str
    context_json: str = "{}"
    expected_output_json: str | None = None
    assertions: list[Assertion] = Field(default_factory=list)
    tool_fixtures: list[Fixture] = Field(default_factory=list)
    expected_calls_json: str = "{}"
    source: Literal["client", "fixture", "intent"] = "intent"

    def domain(self):
        data = self.model_dump(exclude_none=True)
        for key in tuple(data):
            if key.endswith("_json"):
                data[key[:-5]] = json_value(data.pop(key))
        for key in ("assertions", "tool_fixtures"):
            for item in data[key]:
                for name in tuple(item):
                    if name.endswith("_json"):
                        item[name[:-5]] = json_value(item.pop(name))
        return TestSpec.model_validate(data).model_dump()


class TestsResponse(ClosedModel):
    test_cases: list[TestCase]


class ReuseResponse(ClosedModel):
    choice: int | None = None


@lru_cache
def response_model(method, patterns=False):
    if method.startswith("proposal"):
        return PatternProposalResponse if patterns else ProposalResponse
    return {
        "lua_gen": ScriptResponse,
        "luau_gen": ScriptResponse,
        "lua_repair": ScriptRepairResponse,
        "luau_repair": ScriptRepairResponse,
        "workflow_gen": PatternWorkflowResponse if patterns else WorkflowResponse,
        "workflow_repair": PatternWorkflowRepair if patterns else WorkflowRepair,
        "test_gen": TestsResponse,
        "intent": AnalysisResponse,
        "reuse_judge": ReuseResponse,
    }.get(method)


def decode_response(raw, model):
    response = model.model_validate(json_value(raw))
    result = response.model_dump(exclude_none=True)
    if hasattr(response, "definition"):
        result["definition"] = definition_value(response.definition)
    if getattr(response, "workflow", None) is not None:
        result["workflow"]["definition"] = definition_value(response.workflow.definition)
    if isinstance(response, TestsResponse):
        result["test_cases"] = [case.domain() for case in response.test_cases]
    return packed(result)


TRANSPORT_RULES = """
Use the typed response contract supplied by the API (or the transport schema below).
It overrides example response envelopes. Preserve all business rules and client schemas.
Fields ending in _json are JSON-encoded strings, not prose; use exactly the original data.
For semantic plans, repeat uses termination {kind:samples,count}, {kind:duration,seconds},
{kind:deadline,unix}, or {kind:continuous}. Call arguments use args_json; conditions use
when_json; actions, compensation and state use action_json, compensation_json, state_json.
Native/advanced compact graphs use definition_json/graph_json. Code remains a code string
or luau_body. Null means an absent optional field. Return JSON only, no rationale.
"""
