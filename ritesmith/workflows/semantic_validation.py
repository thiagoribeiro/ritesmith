"""Additional semantic checks for generated graphs; legacy validation stays available."""

import json
import re

import jsonschema

from ritesmith.workflows.validator import _branch_sets, _v2_successors


def uninitialized_reference_errors(definition):
    """A body reference must be produced on every route reaching its consumer."""
    from ritesmith.workflows.validator import _data_strings

    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    entry = definition.get("entrypoint")
    if entry not in nodes:
        return []
    reachable = set()
    stack = [entry]
    while stack:
        current = stack.pop()
        if current in reachable or current not in nodes:
            continue
        reachable.add(current)
        stack.extend(_v2_successors(nodes[current]))

    def variable(expression, *, nonnull=False):
        if not isinstance(expression, dict) or set(expression) != {"var"}:
            return set()
        args = expression["var"]
        if isinstance(args, list):
            if not args:
                return set()
            if len(args) > 1 and (
                args[1] is not None if nonnull else args[1] not in (None, False, 0, "")
            ):
                return set()
            args = args[0]
        if isinstance(args, str) and args.startswith("nodes."):
            return {args.split(".", 2)[1]}
        return set()

    def guaranteed(expression):
        if not isinstance(expression, dict) or len(expression) != 1:
            return set()
        operator, args = next(iter(expression.items()))
        if operator == "var":
            return variable(expression)
        if operator == "!!":
            return guaranteed(args[0] if isinstance(args, list) else args)
        if operator in ("and", "or") and isinstance(args, list) and args:
            groups = [guaranteed(item) for item in args]
            return set.union(*groups) if operator == "and" else set.intersection(*groups)
        if operator in ("!=", "!==") and isinstance(args, list) and len(args) == 2:
            if args[0] is None:
                return variable(args[1], nonnull=True)
            if args[1] is None:
                return variable(args[0], nonnull=True)
        return set()

    incoming = {key: [] for key in reachable}
    for key in reachable:
        node = nodes[key]
        if node.get("kind") == "switch":
            edges = [(case["target"], guaranteed(case["when"])) for case in node["cases"]]
            edges.append((node["default"], set()))
        else:
            edges = [(target, set()) for target in _v2_successors(node)]
        for target, guaranteed_producers in edges:
            if target in incoming:
                incoming[target].append((key, guaranteed_producers))
    available = {key: set() if key == entry else set(reachable) for key in reachable}
    changed = True
    while changed:
        changed = False
        for key in reachable - {entry}:
            routes = [
                available[parent]
                | guards
                | ({parent} if nodes[parent].get("kind") in ("task", "join") else set())
                for parent, guards in incoming[key]
            ]
            value = set.intersection(*routes) if routes else set()
            if value != available[key]:
                available[key] = value
                changed = True
    errors = []
    for key in sorted(reachable):
        node = nodes[key]
        if node.get("kind") != "task":
            continue
        request = node.get("action", {}).get("request", {})
        body = request.get("body", {})
        content_type = next(
            (
                value
                for key, value in request.get("headers", {}).items()
                if key.lower() == "content-type"
            ),
            None,
        )
        if content_type is not None and "json" not in str(content_type).lower():
            continue
        if (
            content_type is None
            and isinstance(body, str)
            and not body.lstrip().startswith(("{", "["))
        ):
            continue
        for text in _data_strings(body):
            for reference in re.findall(r"\{\{\s*(nodes\.[^{}\s]+)\s*\}\}", text):
                producer = reference.split(".", 2)[1]
                if producer == key or producer not in available[key]:
                    errors.append(
                        f"Node '{key}': request.body reference '{reference}' is not initialized on every path; supply an explicit first-pass seed"
                    )
    return errors


def reference_contract_errors(definition, registry):
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    branches = _branch_sets(list(nodes.values()))
    errors = []

    def contract(node):
        body = node.get("action", {}).get("request", {}).get("body", {})
        return (
            registry.get(body.get("capability_name") or body.get("artifact_id"), {})
            if isinstance(body, dict)
            else {}
        )

    def paths(value):
        if isinstance(value, str):
            yield from re.findall(r"\{\{\s*(nodes\.[^{}\s]+)\s*\}\}", value)
        elif isinstance(value, list):
            for item in value:
                yield from paths(item)
        elif isinstance(value, dict):
            if set(value) == {"__trama_literal_json__"}:
                return
            if "var" in value:
                path = value["var"]
                if isinstance(path, list) and path:
                    path = path[0]
                if isinstance(path, str) and path.startswith("nodes."):
                    yield path
            for item in value.values():
                yield from paths(item)

    for nid, node in nodes.items():
        for path in set(paths(node)):
            parts = re.sub(r"\[(\d+)\]", r".\1", path).split(".")
            target = nodes.get(parts[1])
            if target is None:
                errors.append(f"Node '{nid}': undefined data reference '{parts[1]}'")
                continue
            suffix = parts[2:]
            if suffix[:3] != ["response", "body", "output"]:
                if target.get("kind") != "join" or suffix[:3] != ["response", "body", "branches"]:
                    continue
                split = next(
                    (item for item in nodes.values() if item.get("join") == target["id"]), None
                )
                if not split or len(suffix) < 6 or not suffix[3].isdigit():
                    errors.append(f"Node '{nid}': invalid joined reference '{path}'")
                    continue
                index = int(suffix[3])
                if index >= len(split["branches"]):
                    errors.append(f"Node '{nid}': joined branch index outside contract in '{path}'")
                    continue
                members = branches[split["branches"][index]]
                terminal = next(
                    (nodes[key] for key in members if nodes[key].get("next") == "end"), None
                )
                if terminal is None:
                    continue
                if contract(terminal) and suffix[4:6] != ["result", "output"]:
                    errors.append(
                        f"Node '{nid}': capability branch result requires result.output in '{path}'"
                    )
                    continue
                target, suffix = terminal, suffix[3:]
            schema = contract(target).get("output_schema")
            for part in suffix[3:]:
                if not isinstance(schema, dict):
                    break
                if schema.get("type") == "array" and part.isdigit():
                    schema = schema.get("items")
                    continue
                properties = schema.get("properties", {})
                if part not in properties:
                    if schema.get("additionalProperties") is False:
                        errors.append(f"Node '{nid}': unknown output field '{part}' in '{path}'")
                    break
                schema = properties[part]
    return errors


def validate_semantics(definition, registry, *, check_graph=True):
    nodes = {node["id"]: node for node in definition.get("nodes", [])}
    errors = reference_contract_errors(definition, registry)
    errors.extend(uninitialized_reference_errors(definition))
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

    def visible(value):
        if isinstance(value, dict):
            if set(value) == {"__trama_literal_json__"}:
                return None
            return {key: visible(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visible(item) for item in value]
        return value

    if check_graph:
        for nid, node in nodes.items():
            for ref in re.findall(r"\bnodes\.([A-Za-z0-9_-]+)\.", json.dumps(visible(node))):
                if ref not in nodes:
                    errors.append(f"Node '{nid}': undefined data reference '{ref}'")

    def templated(value):
        if isinstance(value, dict):
            if set(value) == {"__trama_literal_json__"}:
                return False
            return any(templated(item) for item in value.values())
        if isinstance(value, list):
            return any(templated(item) for item in value)
        return isinstance(value, str) and "{{" in value

    def decoded(value):
        if isinstance(value, dict):
            if set(value) == {"__trama_literal_json__"}:
                return value["__trama_literal_json__"]
            return {key: decoded(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decoded(item) for item in value]
        return value

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
                        jsonschema.validate(decoded(args), schema)
                    except jsonschema.ValidationError as exc:
                        errors.append(f"Node '{nid}': input contract: {exc.message}")
                elif isinstance(args, dict):
                    for key in schema.get("required", []):
                        if key not in args:
                            errors.append(f"Node '{nid}': missing required input '{key}'")
                    for key, value in args.items():
                        field_schema = schema.get("properties", {}).get(key)
                        if field_schema is not None and (
                            not templated(value)
                            or (field_schema.get("type") == "string" and not isinstance(value, str))
                        ):
                            try:
                                jsonschema.validate(decoded(value), field_schema)
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
