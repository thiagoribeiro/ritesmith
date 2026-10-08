"""Black-box validation specifications, independent of the generated implementation."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    args: dict = Field(default_factory=dict)
    output: Any
    times: int = Field(default=1, ge=1, le=100)


class FunctionalAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    op: Literal["equals", "length", "max_length", "contains", "subset"]
    value: Any


class TestSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    expected_output: Any = None
    assertions: list[FunctionalAssertion] = Field(default_factory=list)
    tool_fixtures: list[ToolFixture] = Field(default_factory=list)
    expected_calls: dict[str, int] = Field(default_factory=dict)
    source: Literal["client", "fixture", "intent"] = "client"

    @model_validator(mode="after")
    def valid_call_counts(self):
        if any(not isinstance(n, int) or n < 0 for n in self.expected_calls.values()):
            raise ValueError("expected_calls must contain nonnegative integers")
        return self

    @property
    def functional(self):
        return self.expected_output is not None or bool(self.assertions)


def functional_tests(cases):
    try:
        return any(TestSpec.model_validate(case).functional for case in cases or [])
    except ValueError:
        return False


def test_context(context):
    if context is None:
        return None
    if "workflow_continuation" in context and not isinstance(
        context["workflow_continuation"], dict
    ):
        raise ValueError("workflow_continuation must be an internal continuation object")
    if "test_cases" in context:
        if not isinstance(context["test_cases"], list):
            raise ValueError("test_cases must be a list")
        for case in context["test_cases"]:
            TestSpec.model_validate(case)
    if "test_fixtures" in context:
        if not isinstance(context["test_fixtures"], list):
            raise ValueError("test_fixtures must be a list")
        for fixture in context["test_fixtures"]:
            ToolFixture.model_validate(fixture)
    return context


def inline_schema(model):
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: expand(item) for key, item in value.items()}
        return value

    return expand(schema)


TEST_CONTEXT_PROPERTIES = {
    "test_cases": {
        "type": "array",
        "items": inline_schema(TestSpec),
        "description": "Independent black-box cases. Input-only cases check execution, not functional certification. External tools require controlled fixtures.",
    },
    "test_fixtures": {
        "type": "array",
        "items": inline_schema(ToolFixture),
        "description": "Known tool inputs and responses supplied to the independent test generator; never run external effects during validation.",
    },
}


def bind_known_fixtures(cases, fixtures):
    """Caller records override synthetic data; unrecorded arguments remain unknown."""
    known = [ToolFixture.model_validate(fixture) for fixture in fixtures or []]
    if not known:
        return cases
    known_tools = {fixture.tool for fixture in known}
    result = []
    for case in cases:
        spec = TestSpec.model_validate(case)
        synthetic = [fixture for fixture in spec.tool_fixtures if fixture.tool not in known_tools]
        spec.tool_fixtures = [*synthetic, *known]
        result.append(spec.model_dump())
    return result
