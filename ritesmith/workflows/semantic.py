"""Semantic workflow plans compiled into native Trama; no LLM in continuation paths."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ritesmith.workflows.compact import compile_workflow

FORMAT_VERSION = "workflow-plan-v1"


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "call", "sequence", "wait", "condition", "repeat", "parallel", "state", "callback"
    ]
    id: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    capability_name: str | None = None
    artifact_id: str | None = None
    args: dict = Field(default_factory=dict)
    steps: list[Operation] = Field(default_factory=list)
    branches: list[Operation] = Field(default_factory=list)
    otherwise: list[Operation] = Field(default_factory=list)
    when: dict | None = None
    seconds: float | None = Field(default=None, gt=0)
    interval_seconds: float | None = Field(default=None, gt=0)
    count: int | None = Field(default=None, ge=1, le=1000000)
    duration_seconds: float | None = Field(default=None, gt=0)
    continuous: bool = False
    deadline_unix: float | None = Field(default=None, gt=0)
    state: dict | None = None
    action: dict | None = None
    compensation: dict | None = None

    @model_validator(mode="after")
    def contracts(self):
        allowed = {
            "call": {"capability_name", "artifact_id", "args", "action", "compensation"},
            "state": {"capability_name", "artifact_id", "args", "action", "compensation"},
            "sequence": {"steps"},
            "wait": {"seconds"},
            "condition": {"when", "steps", "otherwise"},
            "parallel": {"branches"},
            "repeat": {
                "steps",
                "interval_seconds",
                "count",
                "duration_seconds",
                "continuous",
                "deadline_unix",
                "state",
            },
            "callback": {"action", "compensation"},
        }
        unsupported = self.model_fields_set - {"kind", "id"} - allowed[self.kind]
        if unsupported:
            raise ValueError(f"Fields unsupported for {self.kind}: {sorted(unsupported)}")
        if (
            self.kind in ("call", "state")
            and sum(
                value is not None for value in (self.capability_name, self.artifact_id, self.action)
            )
            != 1
        ):
            raise ValueError("call/state needs exactly one capability, artifact or action")
        if self.kind == "wait" and self.seconds is None:
            raise ValueError("wait requires seconds")
        if self.kind == "condition" and self.when is None:
            raise ValueError("condition requires when")
        if self.kind == "parallel" and len(self.branches) < 2:
            raise ValueError("parallel needs at least two branches")
        if self.kind == "repeat":
            if not self.id or not self.steps or self.interval_seconds is None:
                raise ValueError("repeat needs steps and interval_seconds")
            if (
                sum(
                    [
                        self.count is not None,
                        self.duration_seconds is not None,
                        self.continuous,
                        self.deadline_unix is not None,
                    ]
                )
                != 1
            ):
                raise ValueError("repeat requires exactly one termination rule")
        if self.kind == "callback" and (
            not self.action or self.action.get("mode") != "async-http-callback"
        ):
            raise ValueError("callback requires an async-http-callback action")
        return self


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["workflow-plan-v1"] = FORMAT_VERSION
    name: str = Field(min_length=1)
    steps: list[Operation] = Field(min_length=1)
    failureHandling: dict | None = None


class PlanCompiler:
    def __init__(
        self, plan, base_url, contract_version="", continuation=None, intent=None, constraints=None
    ):
        self.plan = WorkflowPlan.model_validate(plan)
        self.base_url = base_url
        self.contract_version = contract_version
        self.intent = intent or self.plan.name
        self.constraints = constraints or {}
        self.continuation = dict(continuation or {})
        self.resume = self.continuation.pop("_resume_repeat", None) if continuation else None
        self.nodes = []
        self.ids = set()
        self.loop_limits = []
        self.scopes = {}
        self.branch_results = {}
        self.repeat_count = 0
        self._validate_shape(self.plan.steps)
        for index, operation in enumerate(self.plan.steps):
            chunked = operation.kind == "repeat" and (
                operation.continuous
                or operation.deadline_unix is not None
                or (operation.count is not None and operation.count > 20)
                or (
                    operation.duration_seconds is not None
                    and operation.duration_seconds / operation.interval_seconds > 20
                )
            )
            if index > 0 and chunked:
                raise ValueError(
                    "Pre-repeat actions with continuation require compact_graph or payload state"
                )

    def _validate_shape(self, operations, in_loop=False, in_branch=False, depth=0):
        for operation in operations:
            if operation.kind == "repeat":
                self.repeat_count += 1
                if in_loop or in_branch or self.repeat_count > 1 or depth > 0:
                    raise ValueError("Nested/multiple/branch repeat requires compact_graph")
            self._validate_shape(
                operation.steps, in_loop or operation.kind == "repeat", in_branch, depth + 1
            )
            self._validate_shape(operation.otherwise, in_loop, in_branch, depth + 1)
            self._validate_shape(operation.branches, in_loop, True, depth + 1)

    def id(self, wanted=None):
        candidate = wanted or f"rs_{len(self.ids)}"
        if candidate == "end" or candidate in self.ids:
            raise ValueError(f"Duplicate/reserved operation id: {candidate}")
        self.ids.add(candidate)
        return candidate

    def reference(self, value, node, scope, condition=False):
        if isinstance(value, list):
            return [self.reference(v, node, scope, condition) for v in value]
        if isinstance(value, dict):
            if "ref" in value:
                if set(value) - {"ref", "path"}:
                    raise ValueError("reference only accepts ref/path")
                return {"__rs_reference": value, "__condition": condition}
            return {k: self.reference(v, node, scope, condition) for k, v in value.items()}
        return value

    def resolve(self, value, node, scope):
        if isinstance(value, list):
            return [self.resolve(v, node, scope) for v in value]
        if isinstance(value, dict):
            if "__rs_reference" in value:
                reference = value["__rs_reference"]
                ref, suffix = reference["ref"], reference.get("path", "")
                if not isinstance(ref, str) or not isinstance(suffix, str):
                    raise ValueError("reference ref/path must be strings")
                if ref in ("payload", "runtime", "callback"):
                    path = ref
                else:
                    if ref not in self.scopes:
                        raise ValueError(f"Node {node}: unknown reference {ref}")
                    if scope != self.scopes[ref]:
                        if scope or ref not in self.branch_results:
                            raise ValueError(f"Node {node}: inaccessible reference {ref}")
                        join, index = self.branch_results[ref]
                        path = f"nodes.{join}.response.body.branches.{index}.result.output"
                    else:
                        path = f"nodes.{ref}.response.body.output"
                if suffix:
                    path += "." + suffix
                return {"var": path} if value["__condition"] else "{{ " + path + " }}"
            return {k: self.resolve(v, node, scope) for k, v in value.items()}
        return value

    def add(self, node, scope):
        self.nodes.append(node)
        self.scopes[node["id"]] = scope
        return node["id"]

    def sequence(self, steps, after="end", scope=""):
        entry = after
        for operation in reversed(steps):
            entry = self.operation(operation, entry, scope)
        return entry

    def operation(self, op, after, scope):
        if op.kind == "sequence":
            return self.sequence(op.steps, after, scope)
        nid = self.id(op.id)
        if op.kind in ("call", "state", "callback"):
            node = {"id": nid, "kind": "task", "next": after}
            if op.action is not None:
                node["action"] = self.reference(op.action, nid, scope)
            else:
                key = "capability_name" if op.capability_name is not None else "artifact_id"
                node[key] = getattr(op, key)
                node["input"] = self.reference(op.args, nid, scope)
            if op.compensation:
                node["compensation"] = self.reference(op.compensation, nid, scope)
            return self.add(node, scope)
        if op.kind == "wait":
            return self.add(
                {"id": nid, "kind": "sleep", "durationSeconds": op.seconds, "next": after}, scope
            )
        if op.kind == "condition":
            yes = self.sequence(op.steps, after, scope)
            no = self.sequence(op.otherwise, after, scope)
            return self.add(
                {
                    "id": nid,
                    "kind": "switch",
                    "cases": [
                        {
                            "name": "match",
                            "when": self.reference(op.when, nid, scope, True),
                            "target": yes,
                        }
                    ],
                    "default": no,
                },
                scope,
            )
        if op.kind == "parallel":
            join = self.id(nid + "_join")
            self.add({"id": join, "kind": "join", "next": after}, scope)
            entries = []
            for index, branch in enumerate(op.branches):
                before = set(self.ids)
                entry = self.operation(branch, "end", f"{scope}/{nid}:{index}")
                entries.append(entry)
                # Trama exposes the child's LAST result, not arbitrary child-node outputs.
                terminal = [
                    n["id"] for n in self.nodes if n["id"] not in before and n.get("next") == "end"
                ]
                for node_id in terminal:
                    self.branch_results[node_id] = (join, index)
            return self.add({"id": nid, "kind": "split", "branches": entries, "join": join}, scope)
        if op.kind == "repeat":
            count = op.count
            if op.duration_seconds is not None:
                count = max(1, math.ceil(op.duration_seconds / op.interval_seconds))
            remaining = self.continuation.get("remaining_iterations", count)
            if remaining is not None and (
                not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0
            ):
                raise ValueError("continuation.remaining_iterations must be a nonnegative integer")
            if remaining == 0:
                return after
            chunk = min(20, remaining) if remaining is not None else 20
            tick, check, wait = (
                self.id(nid + "_tick"),
                self.id(nid + "_check"),
                self.id(nid + "_wait"),
            )
            body = self.sequence(op.steps, tick, scope)
            entry = body
            deadline = op.deadline_unix
            if op.deadline_unix is not None or op.duration_seconds is not None:
                clock = self.id(nid + "_clock")
                expired = self.id(nid + "_expired")
                self.add(
                    {
                        "id": clock,
                        "kind": "task",
                        "capability_name": "stat.chain_step",
                        "input": {
                            "deadline_unix": op.deadline_unix
                            or "{{ nodes." + clock + ".response.body.output.deadline_unix }}",
                            "duration_seconds": op.duration_seconds,
                            "initial_deadline": self.continuation.get("deadline_unix"),
                        },
                        "next": expired,
                    },
                    scope,
                )
                self.add(
                    {
                        "id": expired,
                        "kind": "switch",
                        "cases": [
                            {
                                "name": "expired",
                                "when": {
                                    "==": [
                                        {"var": f"nodes.{clock}.response.body.output.done"},
                                        True,
                                    ]
                                },
                                "target": after,
                            }
                        ],
                        "default": body,
                    },
                    scope,
                )
                entry = clock
                deadline = "{{ nodes." + clock + ".response.body.output.deadline_unix }}"
            state = self.reference(op.state or {}, tick, scope)
            self.add(
                {
                    "id": tick,
                    "kind": "task",
                    "capability_name": "stat.chain_step",
                    "input": {
                        "iteration": "{{ nodes." + tick + ".response.body.output.iteration }}",
                        "remaining_iterations": "{{ nodes."
                        + tick
                        + ".response.body.output.remaining_iterations }}",
                        "initial_remaining": remaining,
                        "deadline_unix": deadline,
                        "state": state or None,
                        "previous_state": "{{ nodes." + tick + ".response.body.output.state }}",
                        "initial_state": self.continuation.get("state", {}),
                    },
                    "next": check,
                },
                scope,
            )
            next_chunk = after
            if remaining is None or remaining > 20:
                continue_id = self.id(nid + "_continue")
                boundary_wait = self.id(nid + "_boundary_wait")
                native_plan = self.plan.model_dump(exclude_none=True, exclude_defaults=True)
                self.add(
                    {
                        "id": continue_id,
                        "kind": "task",
                        "plan": {
                            "intent": self.intent,
                            "constraints": self.constraints,
                            "mode": "execute",
                            "requested_artifact_types": ["trama_workflow"],
                            "context": {
                                "workflow_continuation": {
                                    "version": FORMAT_VERSION,
                                    "intent": self.intent,
                                    "plan": native_plan,
                                    "contract_version": self.contract_version,
                                    "completed_repeat": nid,
                                },
                                "continuation": {
                                    "deadline_unix": "{{ nodes."
                                    + tick
                                    + ".response.body.output.deadline_unix }}",
                                    "remaining_iterations": "{{ nodes."
                                    + tick
                                    + ".response.body.output.remaining_iterations }}",
                                    "state": "{{ nodes." + tick + ".response.body.output.state }}",
                                },
                            },
                        },
                        "next": "end",
                    },
                    scope,
                )
                self.add(
                    {
                        "id": boundary_wait,
                        "kind": "sleep",
                        "durationSeconds": op.interval_seconds,
                        "next": continue_id,
                    },
                    scope,
                )
                next_chunk = boundary_wait
            self.add(
                {
                    "id": check,
                    "kind": "switch",
                    "cases": [
                        {
                            "name": "done",
                            "when": {
                                "==": [{"var": f"nodes.{tick}.response.body.output.done"}, True]
                            },
                            "target": after,
                        },
                        {
                            "name": "chunk",
                            "when": {
                                ">=": [
                                    {"var": f"nodes.{tick}.response.body.output.iteration"},
                                    chunk,
                                ]
                            },
                            "target": next_chunk,
                        },
                    ],
                    "default": wait,
                },
                scope,
            )
            self.add(
                {
                    "id": wait,
                    "kind": "sleep",
                    "durationSeconds": op.interval_seconds,
                    "next": entry,
                },
                scope,
            )
            self.loop_limits.append(chunk)
            return entry
        raise ValueError(f"Unsupported operation: {op.kind}")

    def compile(self):
        steps = self.plan.steps
        if self.resume:
            index = next(
                (i for i, op in enumerate(steps) if op.kind == "repeat" and op.id == self.resume),
                None,
            )
            if index is None:
                raise ValueError(
                    "Continuation must resume a top-level repeat; advanced composition requires compact_graph"
                )
            steps = steps[index:]
        entry = self.sequence(steps)
        self.nodes = [
            self.resolve(node, node["id"], self.scopes[node["id"]]) for node in self.nodes
        ]
        definition = {
            "name": self.plan.name,
            "entrypoint": entry,
            "nodes": list(reversed(self.nodes)),
        }
        if self.loop_limits:
            definition["max_iterations"] = max(self.loop_limits)
        if self.plan.failureHandling:
            definition["failureHandling"] = self.plan.failureHandling
        return compile_workflow(definition, self.base_url)


def compile_plan(
    plan, base_url, *, contract_version="", continuation=None, intent=None, constraints=None
):
    return PlanCompiler(
        plan, base_url, contract_version, continuation, intent, constraints
    ).compile()


SEMANTIC_RULES = """
Prefer definition {format:"semantic_plan",plan:{version:"workflow-plan-v1",name,steps}}.
Operations: call/state {id,capability_name OR artifact_id,args}; sequence {steps}; wait {seconds};
condition {when:JSONLogic,steps,otherwise}; parallel {id,branches:[operations]};
repeat {id,steps,interval_seconds, exactly ONE of count,duration_seconds,continuous:true,deadline_unix}.
References are {ref:"operation_id",path:"output_field"} or {ref:"payload",path:"field"}.
After parallel, only the last result of each branch is available through its terminal operation id.
State across repeat chunks: repeat.state; initial values from payload.continuation.state.
Callback {id,kind:"callback",action} uses native async-http-callback contract.
Calls may carry native compensation. Conditions use references for variables.
Compiler constructs counters, scopes, joins, HTTP fields and continuation in blocks of 20.
For nested/multiple repetitions, complex actions or unsupported topology return
{format:"compact_graph",graph:{name,entrypoint,nodes,...}} using compact graph rules.
Never drop requirements to fit a pattern. One response; no format classification call.
"""
