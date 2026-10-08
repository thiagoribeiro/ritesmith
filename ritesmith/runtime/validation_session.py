"""Isolated LunarDyson checking and fixture execution. Never register live tools."""

from collections import Counter
from copy import deepcopy

import jsonschema

from ritesmith.runtime.luau import (
    _invoke,
    luau_tools_for_profile,
    normalize_output,
    script_type_declarations,
    tool_signature,
)
from ritesmith.schemas.test_spec import TestSpec


class FixtureError(ValueError):
    pass


def preflight(cases, tools, input_schema=None, output_schema=None):
    specs = [TestSpec.model_validate(case) for case in cases]
    for spec in specs:
        if input_schema:
            jsonschema.validate(spec.input, input_schema)
        if output_schema and spec.expected_output is not None:
            jsonschema.validate(spec.expected_output, output_schema)
        known_ids = set()

        def ids(value, known_ids=known_ids):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "id" and isinstance(item, str):
                        known_ids.add(item)
                    ids(item)
            elif isinstance(value, list):
                for item in value:
                    ids(item)

        expected = spec.expected_output
        if isinstance(expected, dict) and isinstance(expected.get("notified"), list):
            if "count" in expected and expected["count"] != len(expected["notified"]):
                raise FixtureError("Expected count contradicts notified IDs")
            if set(expected["notified"]) & set(expected.get("failed", [])):
                raise FixtureError("Expected notified/failed IDs overlap")
        for fixture in spec.tool_fixtures:
            if fixture.tool not in tools:
                raise FixtureError(f"Tool outside requested profile: {fixture.tool}")
            definition = tools[fixture.tool]
            if definition.input_schema:
                jsonschema.validate(fixture.args, definition.input_schema)
            if definition.output_schema:
                jsonschema.validate(fixture.output, definition.output_schema)
            ids(fixture.output)
        if (
            spec.tool_fixtures
            and spec.source != "client"
            and isinstance(spec.expected_output, dict)
        ):
            for key in ("notified", "failed", "ids"):
                values = spec.expected_output.get(key)
                if isinstance(values, list) and any(value not in known_ids for value in values):
                    raise FixtureError(
                        f"Expected {key} contains IDs absent from controlled fixtures"
                    )
        for tool in spec.expected_calls:
            if tool not in tools:
                raise FixtureError(f"Unknown tool in expected_calls: {tool}")
    return specs


def assertion_errors(spec, output, counts):
    errors = []
    if spec.expected_output is not None and output != spec.expected_output:
        errors.append(f"expected {spec.expected_output}, got {output}")
    for assertion in spec.assertions:
        value = output
        try:
            for part in assertion.path.split(".") if assertion.path else []:
                value = value[int(part)] if isinstance(value, list) else value[part]
            wanted = assertion.value
            passed = {
                "equals": lambda value=value, wanted=wanted: value == wanted,
                "length": lambda value=value, wanted=wanted: len(value) == wanted,
                "max_length": lambda value=value, wanted=wanted: len(value) <= wanted,
                "contains": lambda value=value, wanted=wanted: wanted in value,
                "subset": lambda value=value, wanted=wanted: all(item in wanted for item in value),
            }[assertion.op]()
            if not passed:
                errors.append(f"Assertion {assertion.path} {assertion.op} {wanted} failed")
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"Assertion path or value invalid: {assertion.path}")
    for tool, wanted in spec.expected_calls.items():
        if counts[tool] != wanted:
            errors.append(f"{tool}: expected {wanted} calls, got {counts[tool]}")
    return errors


class LuauValidationSession:
    def __init__(self, settings, profile, input_schema=None, output_schema=None):
        import lunardyson

        self.tools = luau_tools_for_profile(profile)
        self.settings = settings
        self.output_schema = output_schema
        self.types = script_type_declarations(input_schema, output_schema)
        self.runtime = lunardyson.Runtime(
            memory_mb=settings.lua_memory_limit_mb, cpu_time_ms=settings.lua_timeout_ms
        )
        self.runtime.declare_types(self.types)
        self.spec = None
        self.counts = Counter()
        self.used = Counter()
        self.fixture_error = None
        for name, definition in self.tools.items():
            signature, _ = tool_signature(name, definition)
            self.runtime.tool(
                name, self._handler(name), signature=signature, effect="pure", max_calls=100
            )

    def _handler(self, name):
        def handler(args):
            self.counts[name] += 1
            args = args or {}
            for index, fixture in enumerate(self.spec.tool_fixtures):
                if (
                    fixture.tool == name
                    and fixture.args == args
                    and self.used[index] < fixture.times
                ):
                    self.used[index] += 1
                    return deepcopy(fixture.output)
            pure = {
                "text.slugify",
                "text.upper",
                "text.lower",
                "text.strip",
                "stat.tick",
                "stat.min_value",
                "stat.max_value",
                "stat.length",
                "stat.collect",
            }
            if name in pure:
                definition = self.tools[name]
                _, positional = tool_signature(name, definition)
                return _invoke(definition.callable, positional, args)
            self.fixture_error = f"Missing fixture for tools.{name}({args})"
            raise FixtureError(self.fixture_error)

        return handler

    def check(self, content):
        return {
            mode: [
                d
                for d in self.runtime.check(content, strict=mode == "strict")
                if d["severity"] == "error"
            ]
            for mode in ("nonstrict", "strict")
        }

    def execute(self, content, spec):
        self.spec = spec
        self.counts.clear()
        self.used.clear()
        self.fixture_error = None
        result = self.runtime.execute(content, spec.input, spec.context)
        output = normalize_output(result.output, self.output_schema) if result.ok else None
        errors = assertion_errors(spec, output, self.counts) if result.ok else []
        if result.ok and self.output_schema:
            try:
                jsonschema.validate(output, self.output_schema)
            except jsonschema.ValidationError as exc:
                errors.append("Output contract: " + exc.message)
        return result, self.fixture_error, errors

    def close(self):
        self.runtime.close()
