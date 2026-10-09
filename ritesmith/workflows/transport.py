"""Prepare native typed JSON bodies and protect future continuation templates."""

import json
import re
from copy import deepcopy

_REFERENCE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.\[\]-]*)\s*}}")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+|\[\d+])*\Z")


def _validate_source(value, location):
    if isinstance(value, dict):
        if set(value) == {"__trama_literal_json__"}:
            return
        for key, item in value.items():
            _validate_source(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_source(item, f"{location}[{index}]")
    elif isinstance(value, str):
        if "{{{" in value or "{{" in _REFERENCE.sub("", value):
            raise ValueError(f"{location}: unsupported JSON template expression: {value}")
        for match in _REFERENCE.finditer(value):
            if not _PATH.fullmatch(match[1]):
                raise ValueError(f"{location}: invalid JSON reference '{match[1]}'")


def typed_json_workflow(definition: dict) -> dict:
    from ritesmith.workflows.validator import native_shape_errors

    errors = native_shape_errors(definition, partial=True)
    if errors:
        raise ValueError("; ".join(errors))
    result = deepcopy(definition)
    for node in result.get("nodes", []):
        requests = [node.get("action", {}).get("request", {}), node.get("compensation", {})]
        for request in requests:
            if isinstance(request.get("url"), dict):
                request["url"] = request["url"]["value"]
            if "headers" in request:
                request["headers"] = {
                    key: value["value"] if isinstance(value, dict) else value
                    for key, value in request["headers"].items()
                }
            if "body" not in request:
                continue
            headers = request.setdefault("headers", {})
            for key in list(headers):
                if key.lower() == "x-trama-template-mode":
                    del headers[key]
            content_type = next(
                (v for k, v in headers.items() if k.lower() == "content-type"), None
            )
            if content_type is not None and "json" not in content_type.lower():
                continue
            body = request["body"]
            if isinstance(body, dict) and isinstance(body.get("value"), str):
                if set(body) != {"value"}:
                    raise ValueError(
                        f"Node '{node.get('id')}': request.body has an ambiguous value wrapper"
                    )
                body = body["value"]
            if isinstance(body, str) and (
                body.lstrip().startswith(("{", "[")) or content_type is not None
            ):
                # Native TemplateString also accepts an already serialized JSON body.
                source = body
                try:
                    body = json.loads(source)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Node '{node.get('id')}': request.body is invalid JSON: {exc}"
                    ) from exc
                # A JSON string scalar needs its surrounding source quotes; a
                # top-level value field otherwise triggers TemplateString's wrapper.
                request["body"] = (
                    source
                    if not isinstance(body, (dict, list))
                    or (isinstance(body, dict) and isinstance(body.get("value"), str))
                    else body
                )
            if content_type is None and not isinstance(request["body"], (dict, list)):
                continue
            if request.get("url", "").endswith("/plans") and isinstance(body, dict):
                for key in ("intent", "constraints"):
                    if key in body and not (
                        isinstance(body[key], dict) and set(body[key]) == {"__trama_literal_json__"}
                    ):
                        body[key] = {"__trama_literal_json__": body[key]}
                context = body.get("context") or {}
                if not isinstance(context, dict):
                    raise ValueError(f"Node '{node.get('id')}': /plans context must be an object")
                cached = context.get("workflow_continuation")
                if cached is not None and not (
                    isinstance(cached, dict) and set(cached) == {"__trama_literal_json__"}
                ):
                    context["workflow_continuation"] = {"__trama_literal_json__": cached}
            _validate_source(body, f"Node '{node.get('id')}': request.body")
    return result
