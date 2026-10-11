"""Compact internal Luau candidates; public artifacts always contain complete code."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FORMAT_VERSION = "luau-body-v2"

_NON_CODE = re.compile(
    r'--\[(=*)\[.*?\]\1\]|--[^\n]*|\[(=*)\[.*?\]\2\]|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
    re.DOTALL,
)


def run_declarations(source):
    """Count entry point declarations without treating literals as executable code."""
    return len(re.findall(r"\bfunction\s+run\s*\(", _NON_CODE.sub(" ", source)))


class ScriptCandidateError(ValueError):
    location = "script.body"
    category = "response"


class LuauBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["luau_body"]
    helpers: str = ""
    body: str = Field(min_length=1)

    def compile(self):
        if run_declarations(self.helpers + "\n" + self.body):
            raise ScriptCandidateError(
                "script.body: luau_body must not redeclare run; return statements only, "
                "or put the complete function in the script string"
            )
        return (self.helpers.rstrip() + "\n" if self.helpers.strip() else "") + (
            "function run(input: Input, context: Context): Output\n"
            + self.body.rstrip()
            + "\nend\n"
        )


def expand_script(data):
    result = dict(data)
    if isinstance(result.get("script"), dict):
        result["script"] = LuauBody.model_validate(result["script"]).compile()
    return result


ASSEMBLY_RULES = """
Prefer compact script {format:"luau_body",helpers:"local helper functions",body:"run body"}.
RiteSmith supplies exactly function run(input: Input, context: Context): Output ... end.
Input, Output and Context are predeclared; do not redeclare them or the run function.
Helpers must be local. Return full script text for code incompatible with body assembly.
Return essential metadata only, short descriptions, no explanations.
Complete example: {"script":{"format":"luau_body","helpers":"local function convert(c: number): number return c * 9 / 5 + 32 end","body":"return {fahrenheit = convert(input.celsius)}"},"name":"celsius_to_fahrenheit","description":"Convert Celsius to Fahrenheit","usage_description":"Temperature conversion","tags":["conversion"],"risk_assessment":"low","runtime_profile":"transform_only"}.
"""
