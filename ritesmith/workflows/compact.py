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


def compile_workflow(definition: dict, base_url: str) -> dict:
    """Type validation precedes expansion; callers must also run WorkflowValidator."""
    compact = CompactWorkflow.model_validate(definition)
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
