"""Additional semantic checks for generated graphs; legacy validation stays available."""

import json
import re

import jsonschema

from ritesmith.workflows.validator import _v2_successors


def validate_semantics(definition, registry, *, check_graph=True):
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    errors = []
    reachable = set()
    stack = [definition.get("entrypoint")]
    while stack:
        nid = stack.pop()
        if nid in reachable or nid not in nodes:
            continue
        reachable.add(nid)
        stack.extend(_v2_successors(nodes[nid]))
    for nid in (nodes.keys() - reachable) if check_graph else []:
        errors.append(f"Node '{nid}': unreachable from entrypoint")

    if check_graph:
        for nid, node in nodes.items():
            for ref in re.findall(r"\bnodes\.([A-Za-z0-9_-]+)\.", json.dumps(node)):
                if ref not in nodes:
                    errors.append(f"Node '{nid}': undefined data reference '{ref}'")

    def templated(value):
        return "{{" in json.dumps(value)

    for nid, node in nodes.items():
        if node.get("kind") != "task":
            continue
        body = node.get("action", {}).get("request", {}).get("body", {})
        if not isinstance(body, dict):
            continue
        capability = body.get("capability_name") or body.get("artifact_id")
        contract = registry.get(capability)
        if contract:
            schema = contract.get("input_schema")
            args = body.get("input", {})
            if schema:
                if not templated(args):
                    try:
                        jsonschema.validate(args, schema)
                    except jsonschema.ValidationError as exc:
                        errors.append(f"Node '{nid}': input contract: {exc.message}")
                elif isinstance(args, dict):
                    for key in schema.get("required", []):
                        if key not in args:
                            errors.append(f"Node '{nid}': missing required input '{key}'")
                    for key, value in args.items():
                        field_schema = schema.get("properties", {}).get(key)
                        if field_schema is not None and not templated(value):
                            try:
                                jsonschema.validate(value, field_schema)
                            except jsonschema.ValidationError as exc:
                                errors.append(f"Node '{nid}': input '{key}': {exc.message}")
        if (
            check_graph
            and capability in ("stat.tick", "stat.chain_step")
            and "iteration" in body.get("input", {})
        ):
            # A self-referential counter must retain the previous iteration in a cycle.
            iteration = body["input"]["iteration"]
            seen, search = set(), _v2_successors(node)
            cyclic = False
            while search:
                target = search.pop()
                if target == nid:
                    cyclic = True
                    break
                if target in seen or target not in nodes:
                    continue
                seen.add(target)
                search.extend(_v2_successors(nodes[target]))
            if cyclic and (not isinstance(iteration, str) or f"nodes.{nid}." not in iteration):
                errors.append(f"Node '{nid}': loop counter resets instead of progressing")
    return errors
