"""Semantic workflow plans compiled into native Trama; no LLM in continuation paths."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ritesmith.workflows.compact import compile_workflow
from ritesmith.workflows.expressions import expression_errors

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
    initial_state: dict | None = None
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
                "initial_state",
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
        if self.kind == "condition":
            # Business references are literals resolved by the compiler, not operators.
            def refs(value):
                if isinstance(value, dict):
                    if "ref" in value:
                        return {"var": "payload.placeholder"}
                    return {key: refs(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [refs(item) for item in value]
                return value

            errors = expression_errors(refs(self.when), f"Node '{self.id or 'condition'}'.when")
            if errors:
                raise ValueError("; ".join(errors))
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
            if set(value) == {"__trama_literal_json__"}:
                return deepcopy(value)
            if set(value) == {"type", "parts"} and value["type"] == "template":
                if condition or not isinstance(value["parts"], list):
                    raise ValueError(f"Node {node}: template parts require a string argument")
                return {"__rs_template": self.reference(value["parts"], node, scope)}
            if condition and "var" in value:
                path = value["var"]
                path = path[0] if isinstance(path, list) and path else path
                if isinstance(path, str) and path.split(".", 1)[0] not in (
                    "payload",
                    "runtime",
                    "callback",
                    "nodes",
                    "",
                ):
                    raise ValueError(
                        f"Node {node}: condition path '{path}' needs a structured {{ref,path}} reference"
                    )
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
            if set(value) == {"__trama_literal_json__"}:
                return deepcopy(value)
            if "__rs_template" in value:
                parts = self.resolve(value["__rs_template"], node, scope)
                if not all(isinstance(part, str) for part in parts):
                    raise ValueError(f"Node {node}: template parts must be literals or references")
                return "".join(parts)
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
            return self.repeat(op, nid, after, scope)
        raise ValueError(f"Unsupported operation: {op.kind}")

    @staticmethod
    def literal(value):
        # Seed data is source JSON, so protect any future placeholders it contains.
        import json

        return (
            {"__trama_literal_json__": deepcopy(value)}
            if isinstance(value, (dict, list)) or "{{" in json.dumps(value)
            else deepcopy(value)
        )

    def repeat(self, op, nid, after, scope):
        count = op.count
        if op.duration_seconds is not None:
            count = max(1, math.ceil(op.duration_seconds / op.interval_seconds))
        remaining = self.continuation.get("remaining_iterations", count)
        if remaining is not None and (
            not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0
        ):
            raise ValueError("continuation.remaining_iterations must be a nonnegative integer")
        if count is not None and remaining is not None and remaining > count:
            raise ValueError("continuation.remaining_iterations exceeds the requested total")
        if (
            self.continuation
            and op.duration_seconds is not None
            and self.continuation.get("deadline_unix") is None
        ):
            raise ValueError("Duration continuation requires its original deadline_unix")
        chunk = min(20, remaining) if remaining is not None else 20
        initial_state = deepcopy(op.initial_state or {})
        resumed_state = self.continuation.get("state", {})
        if not isinstance(resumed_state, dict):
            raise TypeError("continuation.state must be an object")
        if self.continuation:
            if count is not None and "remaining_iterations" not in self.continuation:
                raise ValueError(
                    "Finite continuation requires remaining_iterations; it cannot restart the total"
                )
            missing = set(op.initial_state or {}) - resumed_state.keys()
            if missing:
                raise ValueError(f"continuation.state is missing retained keys: {sorted(missing)}")
        initial_state.update(deepcopy(resumed_state))
        seed_paths = {
            (value["ref"], value.get("path", "")): key
            for key, value in (op.state or {}).items()
            if isinstance(value, dict) and "ref" in value
        }

        def seed(reference):
            ref, path = reference["ref"], reference.get("path", "")
            if ref == nid + "_tick":
                if path == "state":
                    return self.literal(initial_state)
                if path.startswith("state."):
                    parts = path[6:].split(".")
                else:
                    values = {
                        "iteration": 0,
                        "remaining_iterations": remaining,
                        "deadline_unix": self.continuation.get("deadline_unix", op.deadline_unix),
                    }
                    if path in values:
                        return self.literal(values[path])
                    raise ValueError(f"Repeat {nid}: unknown initial counter field {path}")
            elif ref == "payload" and path.startswith("continuation.state."):
                parts = path[len("continuation.state.") :].split(".")
            else:
                matches = [
                    (base, key)
                    for (target, base), key in seed_paths.items()
                    if target == ref and (path == base or path.startswith(base + "."))
                ]
                if not matches:
                    raise ValueError(
                        f"Repeat {nid}: previous reference {ref}.{path} needs a state mapping and initial_state"
                    )
                base, key = max(matches, key=lambda match: len(match[0]))
                suffix = path[len(base) :].lstrip(".")
                parts = [key, *(suffix.split(".") if suffix else [])]
            value = initial_state
            for part in parts:
                if isinstance(value, list) and part.isdigit() and int(part) < len(value):
                    value = value[int(part)]
                    continue
                if not isinstance(value, dict) or part not in value:
                    raise ValueError(
                        f"Repeat {nid}: initial_state is missing '{'.'.join(parts)}' for {ref}.{path}"
                    )
                value = value[part]
            return self.literal(value)

        def ids(steps):
            result = set()
            for item in steps:
                if item.id:
                    result.add(item.id)
                result.update(ids(item.steps + item.otherwise + item.branches))
            return result

        business_ids = ids(op.steps)

        def name(original, index):
            return original if index == chunk - 1 else f"{original}__rs_{index + 1}"

        def rewrite(value, index, owner=None, current=False, exit_snapshot=False):
            if isinstance(value, list):
                return [rewrite(item, index, owner, current, exit_snapshot) for item in value]
            if isinstance(value, dict):
                if set(value) == {"__trama_literal_json__"}:
                    return deepcopy(value)
                template_key = (
                    "__rs_template"
                    if "__rs_template" in value
                    else (
                        "parts"
                        if set(value) == {"type", "parts"} and value["type"] == "template"
                        else None
                    )
                )
                if template_key:
                    parts = []
                    for original in value[template_key]:
                        item = rewrite(original, index, owner, current, exit_snapshot)
                        if (
                            isinstance(original, dict)
                            and ("ref" in original or "__rs_reference" in original)
                            and not isinstance(item, dict)
                        ):
                            if isinstance(item, list):
                                raise ValueError(
                                    f"Repeat {nid}: collection seed cannot be interpolated in text"
                                )
                            item = (
                                ""
                                if item is None
                                else str(item).lower()
                                if isinstance(item, bool)
                                else str(item)
                            )
                        parts.append(item)
                    return {**value, template_key: parts}
                if "__rs_reference" in value:
                    resolved = rewrite(
                        value["__rs_reference"], index, owner, current, exit_snapshot
                    )
                    return (
                        {**value, "__rs_reference": resolved}
                        if isinstance(resolved, dict) and "ref" in resolved
                        else resolved
                    )
                if "ref" in value:
                    ref = value["ref"]
                    previous = (ref == nid + "_tick" and not exit_snapshot) or (
                        ref == owner and not current
                    )
                    if ref == "payload" and value.get("path", "").startswith("continuation.state."):
                        return seed(value)
                    if (previous and index == 0) or (
                        index < 0 and (ref in business_ids or ref == nid + "_tick")
                    ):
                        return seed(value)
                    if ref in business_ids or ref == nid + "_tick":
                        return {**value, "ref": name(ref, index - 1 if previous else index)}
                    return deepcopy(value)
                return {
                    key: rewrite(item, index, owner, current, exit_snapshot)
                    for key, item in value.items()
                }
            return deepcopy(value)

        def operations(steps, index):
            result = []
            for item in steps:
                value = item.model_dump(exclude_none=True, exclude_defaults=True)
                for key in ("args", "action", "compensation", "when"):
                    if key in value:
                        value[key] = rewrite(value[key], index, item.id)
                for key in ("steps", "otherwise", "branches"):
                    if key in value:
                        value[key] = [
                            child.model_dump(exclude_none=True, exclude_defaults=True)
                            for child in operations(getattr(item, key), index)
                        ]
                if item.id:
                    value["id"] = name(item.id, index)
                result.append(Operation.model_validate(value))
            return result

        # A deadline can end before the last expanded iteration. Final actions
        # must read the actually completed pass (or the explicit resumed seed).
        tail_nodes = {node["id"]: node for node in self.nodes}
        reachable_tail = set()
        stack = [after]
        from ritesmith.workflows.validator import _v2_successors

        while stack:
            key = stack.pop()
            if key not in tail_nodes or key in reachable_tail:
                continue
            reachable_tail.add(key)
            stack.extend(_v2_successors(tail_nodes[key]))

        exit_cache = {}

        def exit_entry(index):
            if not reachable_tail or index == chunk - 1:
                return after
            if index in exit_cache:
                return exit_cache[index]
            renamed = {
                key: self.id(f"{key}__exit_{nid}_{index + 1}") for key in sorted(reachable_tail)
            }

            def rename_tail(value):
                if isinstance(value, list):
                    return [rename_tail(item) for item in value]
                if isinstance(value, dict):
                    if set(value) == {"__trama_literal_json__"}:
                        return deepcopy(value)
                    if "ref" in value and value["ref"] in renamed:
                        return {**value, "ref": renamed[value["ref"]]}
                    return {key: rename_tail(item) for key, item in value.items()}
                return value

            for key in sorted(reachable_tail):
                node = rename_tail(
                    rewrite(tail_nodes[key], index, current=True, exit_snapshot=True)
                )
                node["id"] = renamed[key]
                for link in ("next", "default", "join"):
                    if node.get(link) in renamed:
                        node[link] = renamed[node[link]]
                if "branches" in node:
                    node["branches"] = [renamed.get(branch, branch) for branch in node["branches"]]
                for case in node.get("cases", []):
                    case["target"] = renamed.get(case["target"], case["target"])
                self.add(node, self.scopes[key])
                if key in self.branch_results:
                    join, branch = self.branch_results[key]
                    self.branch_results[node["id"]] = (renamed.get(join, join), branch)
            exit_cache[index] = renamed[after]
            return renamed[after]

        if remaining == 0:
            self.nodes = [
                rewrite(node, -1, current=True, exit_snapshot=True)
                if node["id"] in reachable_tail
                else node
                for node in self.nodes
            ]
            return after

        # Every reference in the first pass has an explicit seed. Later passes read
        # already completed nodes. Unrolling is bounded to 20 business iterations.
        next_entry = after
        if remaining is None or remaining > chunk:
            continuation = self.id(nid + "_continue")
            boundary = self.id(nid + "_boundary_wait")
            tick_path = "nodes." + nid + "_tick.response.body.output."
            self.add(
                {
                    "id": continuation,
                    "kind": "task",
                    "plan": {
                        "intent": self.literal(self.intent),
                        "constraints": self.literal(self.constraints),
                        "mode": "execute",
                        "requested_artifact_types": ["trama_workflow"],
                        "context": {
                            "workflow_continuation": self.literal(
                                {
                                    "version": FORMAT_VERSION,
                                    "intent": self.intent,
                                    "plan": self.plan.model_dump(
                                        exclude_none=True, exclude_defaults=True
                                    ),
                                    "contract_version": self.contract_version,
                                    "completed_repeat": nid,
                                }
                            ),
                            "continuation": {
                                key: "{{ " + tick_path + key + " }}"
                                for key in ("deadline_unix", "remaining_iterations", "state")
                            },
                        },
                    },
                    "next": "end",
                },
                scope,
            )
            self.add(
                {
                    "id": boundary,
                    "kind": "sleep",
                    "durationSeconds": op.interval_seconds,
                    "next": continuation,
                },
                scope,
            )
            next_entry = boundary

        for index in reversed(range(chunk)):
            tick = self.id(name(nid + "_tick", index))
            check = self.id(name(nid + "_check", index))
            deadline = self.continuation.get("deadline_unix") or op.deadline_unix
            clock = None
            if op.deadline_unix is not None or op.duration_seconds is not None:
                clock = self.id(name(nid + "_clock", index))
                if index and deadline is None:
                    deadline = (
                        "{{ nodes."
                        + name(nid + "_clock", index - 1)
                        + ".response.body.output.deadline_unix }}"
                    )
                self.add(
                    {
                        "id": clock,
                        "kind": "task",
                        "capability_name": "stat.chain_step",
                        "input": {
                            "deadline_unix": deadline,
                            "duration_seconds": op.duration_seconds,
                            "initial_deadline": self.continuation.get("deadline_unix"),
                        },
                        "next": name(nid + "_expired", index),
                    },
                    scope,
                )
                deadline = "{{ nodes." + clock + ".response.body.output.deadline_unix }}"
            previous_state = (
                self.literal(initial_state)
                if index == 0
                else (
                    "{{ nodes." + name(nid + "_tick", index - 1) + ".response.body.output.state }}"
                )
            )
            self.add(
                {
                    "id": tick,
                    "kind": "task",
                    "capability_name": "stat.chain_step",
                    "input": {
                        "iteration": index,
                        "remaining_iterations": None if remaining is None else remaining - index,
                        "initial_remaining": remaining,
                        "deadline_unix": deadline,
                        "state": self.reference(
                            rewrite(op.state or {}, index, current=True), tick, scope
                        )
                        or None,
                        "previous_state": previous_state,
                        "initial_state": self.literal(initial_state),
                    },
                    "next": check,
                },
                scope,
            )
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
                            "target": exit_entry(index) if clock else after,
                        }
                    ],
                    "default": next_entry,
                },
                scope,
            )
            body = self.sequence(operations(op.steps, index), tick, scope)
            entry = body
            if clock:
                expired = self.id(name(nid + "_expired", index))
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
                                "target": exit_entry(index - 1),
                            }
                        ],
                        "default": body,
                    },
                    scope,
                )
                entry = clock
            if index:
                wait = self.id(name(nid + "_wait", index - 1))
                self.add(
                    {
                        "id": wait,
                        "kind": "sleep",
                        "durationSeconds": op.interval_seconds,
                        "next": entry,
                    },
                    scope,
                )
                next_entry = wait
            else:
                next_entry = entry
        self.loop_limits.append(chunk)
        return next_entry

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
For text combining references use {type:"template",parts:["BTC: ",{ref:"fetch",path:"price"}]};
the compiler emits a native string, never a template object passed to a tool.
After parallel, only the last result of each branch is available through its terminal operation id.
State across repeat chunks: repeat.state and explicit repeat.initial_state defaults.
The compiler overlays continuation.state on those defaults and initializes the first pass.
Self-references need a repeat.state mapping and an explicit initial_state key; never use absent node results as null.
Callback {id,kind:"callback",action} uses native async-http-callback contract.
Calls may carry native compensation. Conditions use references for variables.
Example: condition {when:{">": [{ref:"fetch",path:"price"},100000]},steps:[...]};
do not use raw var paths, "greater", inline Mustache paths or mechanical counter IDs.
Specify TOTAL work: seven days at four samples/day means count:28 and interval_seconds:21600.
Do not cap count at 20, add manual advance_chain actions or build continuation yourself.
Compiler constructs counters, scopes, joins, HTTP fields and continuation in blocks of 20.
For nested/multiple repetitions, complex actions or unsupported topology return
{format:"compact_graph",graph:{name,entrypoint,nodes,...}} using compact graph rules.
Never drop requirements to fit a pattern. One response; no format classification call.
"""
