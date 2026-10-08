"""Internal actionable diagnostics; public validation strings remain compatible."""

from typing import Literal

from pydantic import BaseModel


class Diagnostic(BaseModel):
    category: Literal[
        "response",
        "truncated",
        "syntax",
        "type",
        "contract",
        "functional",
        "fixture",
        "graph",
        "transport",
        "rate_limit",
        "quota",
        "deadline",
    ]
    message: str
    location: str | None = None
    cause: str | None = None
    repairable: bool = True


def validation_diagnostics(validation):
    result = []
    for check in validation.checks:
        if check.status not in ("failed", "warning"):
            continue
        name = check.name
        category = (
            "fixture"
            if "fixture" in name or "test_spec" in name
            else "syntax"
            if "syntax" in name
            else "type"
            if "type_check" in name
            else "functional"
            if name.startswith("test_case")
            else "contract"
        )
        result.append(
            Diagnostic(
                category=category,
                message=check.message or name,
                location=name,
                repairable=category != "fixture",
            )
        )
    return result
