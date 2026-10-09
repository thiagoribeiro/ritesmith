"""OpenAI schema lowering; application contracts remain provider-independent."""

from copy import deepcopy
from functools import lru_cache


@lru_cache
def strict_schema(model):
    schema = deepcopy(model.model_json_schema())

    def lower(value):
        if isinstance(value, list):
            return [lower(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: lower(item)
            for key, item in value.items()
            if key not in ("default", "title", "discriminator")
        }
        if "oneOf" in result:
            result["anyOf"] = result.pop("oneOf")
        if result.get("type") == "object":
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result

    return lower(schema)


def unsupported_reason(schema):
    """Route unsupported shapes before sending; never silently weaken a contract."""
    if schema.get("type") != "object" or "anyOf" in schema:
        return "root_must_be_object"
    unsupported = {
        "allOf",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "patternProperties",
    }

    def inspect(value):
        if isinstance(value, list):
            return next((reason for item in value if (reason := inspect(item))), None)
        if not isinstance(value, dict):
            return None
        if unsupported & value.keys():
            return "unsupported_keyword"
        if value.get("type") == "object" and value.get("additionalProperties") is not False:
            return "open_object"
        return next((reason for item in value.values() if (reason := inspect(item))), None)

    return inspect(schema)


def supports_schema(model):
    return model.startswith(("gpt-5", "gpt-4.1", "gpt-4o", "o3", "o4"))
