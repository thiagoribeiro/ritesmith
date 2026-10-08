"""Compact internal Luau candidates; public artifacts always contain complete code."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FORMAT_VERSION = "luau-body-v1"


class LuauBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["luau_body"]
    helpers: str = ""
    body: str = Field(min_length=1)

    def compile(self):
        if re.search(r"\bfunction\s+run\s*\(", self.helpers + "\n" + self.body):
            raise ValueError("luau_body must not redeclare run")
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
"""
