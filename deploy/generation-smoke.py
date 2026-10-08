"""Offline ARM deployment checks. No LLM requests or live tool effects."""

import asyncio

from ritesmith.config import Settings
from ritesmith.core.validation import ValidationPipeline
from ritesmith.llm.script_candidate import expand_script
from ritesmith.runtime.validation_session import LuauValidationSession
from ritesmith.schemas.test_spec import TestSpec
from ritesmith.workflows.constructors import semantic_example
from ritesmith.workflows.examples import EXAMPLE_IDS
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.validator import WorkflowValidator


async def main():
    settings = Settings(require_strict_typecheck=True)
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    code = expand_script(
        {
            "script": {
                "format": "luau_body",
                "body": "return {text = tools.text.upper({s = input.text})}",
            }
        }
    )["script"]
    result = await ValidationPipeline(settings).run(
        code,
        "luau_script",
        input_schema=schema,
        output_schema=schema,
        test_cases=[{"input": {"text": "hello"}, "expected_output": {"text": "HELLO"}}],
    )
    assert result.valid and not result.warnings, result.model_dump()
    for pattern in EXAMPLE_IDS:
        graph = compile_plan(semantic_example(pattern), "http://ritesmith:8081")
        assert not WorkflowValidator().validate(graph), pattern
    session = LuauValidationSession(settings, "readonly_network")
    try:
        _, fixture_error, _ = session.execute(
            "function run(input: Input, context: Context): Output\n"
            'return tools.http.get_json({url = "https://example.invalid/"})\nend',
            TestSpec(input={}),
        )
        assert fixture_error and "Missing fixture" in fixture_error
    finally:
        session.close()
    print(
        "Offline checks passed: strict Luau assembly, nine Trama patterns, missing-fixture isolation"
    )


if __name__ == "__main__":
    asyncio.run(main())
