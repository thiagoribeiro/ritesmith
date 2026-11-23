"""Lossless internal workflow shorthand; public artifacts remain native Trama v2."""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)


class Task(Node):
    kind: Literal["task"]
    capability_name: str | None = None
    artifact_id: str | None = None
    input: dict = Field(default_factory=dict)
    plan: dict | None = None
    action: dict | None = None
    compensation: dict | None = None
    next: str = "end"

    @model_validator(mode="after")
    def one_target(self):
        if (
            sum(
                v is not None
                for v in (self.capability_name, self.artifact_id, self.plan, self.action)
            )
            != 1
        ):
            raise ValueError("task needs exactly one capability_name, artifact_id, plan or action")
        if (self.capability_name == "") or (self.artifact_id == ""):
            raise ValueError("task target must not be empty")
        if (self.action is not None or self.plan is not None) and self.input:
            raise ValueError("raw action/plan carries its own input")
        return self


class Sleep(Node):
    kind: Literal["sleep"]
    durationSeconds: int | float = Field(gt=0)
    next: str = "end"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    when: dict
    target: str


class Switch(Node):
    kind: Literal["switch"]
    cases: list[Case]
    default: str


class Split(Node):
    kind: Literal["split"]
    branches: list[str] = Field(min_length=2)
    join: str


class Join(Node):
    kind: Literal["join"]
    next: str = "end"


CompactNode = Annotated[Task | Sleep | Switch | Split | Join, Field(discriminator="kind")]


class CompactWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    version: str = "2.0.0"
    entrypoint: str
    nodes: list[CompactNode]
    max_iterations: int | None = Field(default=None, ge=1)
    failureHandling: dict | None = None


def capability_action(base_url: str, body: dict) -> dict:
    return {
        "mode": "sync",
        "request": {
            "url": base_url.rstrip("/") + "/trama/execute",
            "verb": "POST",
            "headers": {
                "Authorization": "Bearer __TRAMA_TOKEN__",
                "Content-Type": "application/json",
            },
            "body": body,
        },
        "successStatusCodes": [200],
    }


def _without_nulls(definition):
    # Every optional field defaults when absent, so an explicit null (often an
    # invented key, or next: null on a terminal node) carries no information.
    if not isinstance(definition, dict):
        return definition
    cleaned = {k: v for k, v in definition.items() if v is not None}
    if isinstance(cleaned.get("nodes"), list):
        cleaned["nodes"] = [
            {k: v for k, v in n.items() if v is not None} if isinstance(n, dict) else n
            for n in cleaned["nodes"]
        ]
    return cleaned


def compile_workflow(definition: dict, base_url: str) -> dict:
    """Type validation precedes expansion; callers must also run WorkflowValidator."""
    compact = CompactWorkflow.model_validate(_without_nulls(definition))
    result = compact.model_dump(exclude_none=True)
    nodes = []
    for node in compact.nodes:
        if not isinstance(node, Task):
            nodes.append(node.model_dump(exclude_none=True))
            continue
        native = {"id": node.id, "kind": "task", "next": node.next}
        if node.action is not None:
            action = deepcopy(node.action)
            request = action.get("request", {})
            url = request.get("url", "")
            if isinstance(url, str) and url.startswith("__RS_BASE_URL__/"):
                request["url"] = base_url.rstrip("/") + url[len("__RS_BASE_URL__") :]
        elif node.plan is not None:
            action = {
                "mode": "sync",
                "request": {
                    "url": base_url.rstrip("/") + "/plans",
                    "verb": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "body": deepcopy(node.plan),
                },
                "successStatusCodes": [200, 201],
            }
        else:
            key = "capability_name" if node.capability_name is not None else "artifact_id"
            action = capability_action(
                base_url, {key: getattr(node, key), "input": deepcopy(node.input)}
            )
        native["action"] = action
        if node.compensation is not None:
            native["compensation"] = deepcopy(node.compensation)
        nodes.append(native)
    result["nodes"] = nodes
    return result


def compact_workflow(definition: dict, base_url: str) -> dict:
    """Only compress canonical capability calls. Advanced actions stay lossless."""
    result = deepcopy(definition)
    for node in result.get("nodes", []):
        if node.get("kind") != "task":
            continue
        action = node.get("action", {})
        body = action.get("request", {}).get("body", {})
        if not isinstance(body, dict):
            continue
        targets = [k for k in ("capability_name", "artifact_id") if k in body]
        if (
            len(targets) == 1
            and set(body) == {targets[0], "input"}
            and action == capability_action(base_url, body)
        ):
            node.pop("action")
            node[targets[0]] = body[targets[0]]
            node["input"] = body["input"]
    return result


DEFAULT_FAILURE_HANDLING = {"type": "retry", "maxAttempts": 1, "delayMillis": 0}
TERMINAL_KEYWORDS = {"end", "done", "finish", "terminal", "stop", "complete"}
TERMINAL_NODE_ID = "rs_end"


def to_trama_definition(definition: dict) -> dict:
    """Turn RiteSmith's compact workflow (compile_workflow / compile_plan output) into a
    definition Trama accepts.

    - Injects default failureHandling when absent.
    - Replaces terminal keyword strings in 'next' fields with null (Trama uses null for terminal).
    - Points switch targets/defaults that mean "finish" at a terminal node: Trama accepts
      null only for `next`, and a switch must name an existing node. compile_plan emits
      "end" there for a condition that ends the plan or a parallel branch; RiteSmith's
      completion step rewires top-level ones to rs_complete, but direct compile_plan
      callers and branch-final conditions reached Trama with "end" and were rejected.
      The terminal is a 1 ms sleep, the only node kind that ends a path with no effect.
    """
    if not isinstance(definition, dict):
        return definition

    out = dict(definition)

    if "failureHandling" not in out:
        out["failureHandling"] = deepcopy(DEFAULT_FAILURE_HANDLING)

    if "nodes" in out and isinstance(out["nodes"], list):
        normalized_nodes = []
        for node in out["nodes"]:
            if not isinstance(node, dict):
                normalized_nodes.append(node)
                continue
            n = dict(node)
            # Terminal node references must be null, not a keyword string
            if n.get("next") in TERMINAL_KEYWORDS:
                n["next"] = None
            # Sleep nodes: convert durationSeconds → durationMillis if needed
            if n.get("kind") == "sleep" and "durationSeconds" in n and "durationMillis" not in n:
                n["durationMillis"] = int(n.pop("durationSeconds") * 1000)
            normalized_nodes.append(n)
        terminal = _route_switch_finishes(normalized_nodes)
        if terminal:
            normalized_nodes.append(
                {"id": terminal, "kind": "sleep", "durationMillis": 1, "next": None}
            )
        out["nodes"] = normalized_nodes

    return out


def _route_switch_finishes(nodes: list) -> str | None:
    """Rewrite switch exits that finish to one terminal id; return it if any was needed."""
    taken = {n.get("id") for n in nodes if isinstance(n, dict)}
    terminal = TERMINAL_NODE_ID
    suffix = 1
    while terminal in taken:
        terminal = f"{TERMINAL_NODE_ID}_{suffix}"
        suffix += 1
    used = False
    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") != "switch":
            continue
        cases = [dict(c) if isinstance(c, dict) else c for c in node.get("cases") or []]
        for case in cases:
            if isinstance(case, dict) and case.get("target") in TERMINAL_KEYWORDS:
                case["target"] = terminal
                used = True
        if "cases" in node:
            node["cases"] = cases
        if node.get("default") in TERMINAL_KEYWORDS:
            node["default"] = terminal
            used = True
    return terminal if used else None
