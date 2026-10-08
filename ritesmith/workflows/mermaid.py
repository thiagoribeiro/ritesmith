"""Restricted Mermaid flowcharts with lossless Trama semantic annotations.

Edges determine control flow; JSON comments carry contracts that Mermaid itself
cannot express. Parsing never invokes a model or evaluates annotation content.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy

from ritesmith.workflows.compact import CompactWorkflow, compile_workflow

_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_EDGE = re.compile(rf'({_ID})\s*-->\s*(?:\|"?([^"|]+)"?\|\s*)?({_ID})\s*;?')
_LABEL = r'(?:"[^"\r\n]*"|[^"\r\n\[\]{}()]+)'
_DECL = re.compile(
    rf"({_ID})(?:\[{_LABEL}\]|\{{{_LABEL}\}}|\{{\{{{_LABEL}\}}\}}|"
    rf"\(\[{_LABEL}\]\)|\(\({_LABEL}\)\)|\({_LABEL}\))\s*;?"
)
_END = "RS_END"


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_mermaid(definition: dict) -> str:
    """Render typed compact nodes; preserve their ordering and every native field."""
    root = CompactWorkflow.model_validate(definition).model_dump(exclude_none=True)
    nodes = root.pop("nodes")
    used = {_END}
    symbols = {}
    for index, node in enumerate(nodes):
        candidate = node["id"]
        if not re.fullmatch(_ID, candidate) or candidate in used:
            candidate = f"rs_node_{index}"
            while candidate in used or any(n["id"] == candidate for n in nodes):
                candidate += "_"
        if node["id"] in symbols:
            raise ValueError("duplicate node id")
        used.add(candidate)
        symbols[node["id"]] = candidate

    def target(id_: str) -> str:
        if id_ == "end":
            return _END
        if id_ not in symbols:
            raise ValueError(f"undefined target: {id_}")
        return symbols[id_]

    if root["entrypoint"] not in symbols:
        raise ValueError("undefined entrypoint")
    lines = ["flowchart TD", "%% workflow " + _json(root)]
    edges = []
    shapes = {
        "task": '["task"]',
        "sleep": '(["sleep"])',
        "switch": '{"switch"}',
        "split": '{{"split"}}',
        "join": '(("join"))',
    }
    for node in nodes:
        payload = deepcopy(node)
        symbol = symbols[node["id"]]
        if payload["kind"] == "switch":
            for index, case in enumerate(payload["cases"]):
                edges.append(f'{symbol} -->|"case:{index}"| {target(case.pop("target"))}')
            edges.append(f'{symbol} -->|"default"| {target(payload.pop("default"))}')
        elif payload["kind"] == "split":
            for index, branch in enumerate(payload.pop("branches")):
                edges.append(f'{symbol} -->|"branch:{index}"| {target(branch)}')
            edges.append(f'{symbol} -->|"join"| {target(payload.pop("join"))}')
        else:
            edges.append(f"{symbol} --> {target(payload.pop('next'))}")
        if payload["id"] == symbol:
            payload.pop("id")
        lines.extend(["%% node " + symbol + " " + _json(payload), symbol + shapes[node["kind"]]])
    return "\n".join(lines + [f'{_END}(["END"])'] + edges)


def _parse_mermaid(source: str) -> dict:
    """Reject ambiguous/missing flow and unsupported syntax instead of guessing."""
    if not isinstance(source, str) or len(source) > 1_000_000:
        raise ValueError("Mermaid definition must be a string of at most 1 MB")
    source = source.strip()
    if source.startswith("```mermaid\n") and source.endswith("```"):
        source = source[len("```mermaid\n") : -3].strip()
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if not lines or not re.fullmatch(r"(?:flowchart|graph)\s+(?:TD|TB|LR|RL|BT)\s*;?", lines[0]):
        raise ValueError("expected Mermaid flowchart header")
    metadata = None
    nodes = {}
    declarations = set()
    edges = []
    edge_lines = {}
    node_lines = {}
    numbered = [
        (index, line.strip()) for index, line in enumerate(source.splitlines(), 1) if line.strip()
    ]
    for line_number, line in numbered[1:]:
        if line.startswith("%% workflow "):
            if metadata is not None:
                raise ValueError("duplicate workflow metadata")
            metadata = json.loads(line[len("%% workflow ") :])
        elif line.startswith("%% node "):
            match = re.fullmatch(rf"%% node\s+({_ID})\s+(\{{.*\}})", line)
            if not match or match[1] in nodes or match[1] == _END:
                raise ValueError("invalid or duplicate node annotation")
            payload = json.loads(match[2])
            if any(key in payload for key in ("next", "default", "branches", "join")):
                raise ValueError("control flow belongs in Mermaid edges")
            if any("target" in case for case in payload.get("cases", [])):
                raise ValueError("case targets belong in Mermaid edges")
            nodes[match[1]] = {"id": match[1], **payload}
            node_lines[match[1]] = line_number
        elif line.startswith("%%"):
            continue
        elif match := _EDGE.fullmatch(line):
            edge = (match[1], match[2] or "", match[3])
            edges.append(edge)
            edge_lines[edge] = line_number
        elif match := _DECL.fullmatch(line):
            if match[1] in declarations:
                raise ValueError("duplicate node declaration")
            declarations.add(match[1])
        else:
            raise MermaidError(
                f"unsupported Mermaid syntax: {line[:100]}",
                line=line_number,
            )
    if not isinstance(metadata, dict) or "nodes" in metadata:
        raise ValueError("workflow metadata required; nodes use annotations")
    if declarations - set(nodes) - {_END}:
        raise ValueError("unannotated node declaration")
    outgoing = {symbol: {} for symbol in nodes}
    for origin, label, destination in edges:
        if origin not in nodes or (destination not in nodes and destination != _END):
            raise MermaidError(
                "edge references an undefined node",
                line=edge_lines[(origin, label, destination)],
                node=origin,
                edge=(origin, label, destination),
            )
        if label in outgoing[origin]:
            raise MermaidError(
                "ambiguous duplicate edge label",
                line=edge_lines[(origin, label, destination)],
                node=origin,
                edge=(origin, label, destination),
            )
        outgoing[origin][label] = "end" if destination == _END else nodes[destination]["id"]
    for symbol, node in nodes.items():
        links = outgoing[symbol]
        kind = node.get("kind")
        if kind == "switch":
            cases = node.get("cases", [])
            expected = {"default"} | {f"case:{i}" for i in range(len(cases))}
            if set(links) != expected:
                raise MermaidError(
                    "switch requires one edge per case and a default",
                    line=node_lines[symbol],
                    node=symbol,
                )
            for index, case in enumerate(cases):
                case["target"] = links[f"case:{index}"]
            node["default"] = links["default"]
        elif kind == "split":
            branches = [key for key in links if key.startswith("branch:")]
            expected = {"join"} | {f"branch:{i}" for i in range(len(branches))}
            if set(links) != expected or len(branches) < 2:
                raise MermaidError(
                    "split requires contiguous branch indices and a join",
                    line=node_lines[symbol],
                    node=symbol,
                )
            node["branches"] = [links[f"branch:{i}"] for i in range(len(branches))]
            node["join"] = links["join"]
        else:
            if set(links) != {""}:
                raise MermaidError(
                    "task/sleep/join requires exactly one unlabeled next edge",
                    line=node_lines[symbol],
                    node=symbol,
                )
            node["next"] = links[""]
    result = {**metadata, "nodes": list(nodes.values())}
    typed = CompactWorkflow.model_validate(result)
    ids = [node.id for node in typed.nodes]
    if len(ids) != len(set(ids)) or "end" in ids or typed.entrypoint not in ids:
        raise ValueError("invalid node ids or entrypoint")
    return typed.model_dump(exclude_none=True)


def compile_mermaid(source: str, base_url: str) -> dict:
    return compile_workflow(parse_mermaid(source), base_url)


MERMAID_RULES = """
Return definition as a Mermaid STRING inside the JSON response (escape newlines).
Use flowchart TD. Each line has one declaration, annotation, or edge.
%% workflow {"name":"...","entrypoint":"node_id","max_iterations":3} carries metadata.
%% node ID {"kind":"task","capability_name":"...","input":{...}} carries semantics.
Task annotations use capability_name OR artifact_id with input; OR plan with the /plans body;
OR native action for advanced HTTP/async callbacks. Compensation remains a native object.
Sleep annotations carry durationSeconds. Switch annotations carry cases:[{name,when}].
Split/join annotations carry kind only. Never put next/default/branches/join/case targets in annotations.
Task/sleep/join have exactly one edge: ID --> NEXT (RS_END means end).
Switch has ID -->|"case:0"| TARGET per case and ID -->|"default"| TARGET.
Split has ID -->|"branch:0"| TARGET per branch and ID -->|"join"| JOIN.
End each split branch with RS_END, not the join. Use stable unique alphanumeric IDs.
Declare nodes with familiar Mermaid shapes; declarations' labels are only visual.
No subgraphs, styles, implicit chained arrows, or other Mermaid syntax.
Python parses the edges, validates typed nodes, and expands HTTP/auth deterministically.
"""


class MermaidError(ValueError):
    def __init__(self, message, *, line=None, node=None, edge=None):
        super().__init__(message)
        self.line, self.node, self.edge = line, node, edge


def parse_mermaid(source):
    try:
        return _parse_mermaid(source)
    except MermaidError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise MermaidError(str(exc)) from exc
