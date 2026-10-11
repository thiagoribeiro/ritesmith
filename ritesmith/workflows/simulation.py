"""Offline Trama control-flow checks with a virtual clock and explicit tool responses.

This intentionally supports the generated JSONLogic/Mustache subset. It does not
claim equivalence with all Trama runtime features and never performs HTTP calls.
"""

import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field

_TEMPLATE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class UnsupportedSimulation(ValueError):
    """An engine-supported expression outside the controlled simulator subset."""


@dataclass(frozen=True)
class HttpFailure:
    """An explicit failed transport response, distinct from a tool's JSON output."""

    status: int = 500
    message: str = "controlled HTTP failure"


def lookup(data, path):
    value = data
    for part in path.split("."):
        if isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def render(value, context, native_templates=False):
    if isinstance(value, dict):
        if not native_templates and set(value) == {"__trama_literal_json__"}:
            return deepcopy(value["__trama_literal_json__"])
        return {key: render(item, context, native_templates) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context, native_templates) for item in value]
    if isinstance(value, str):
        match = _TEMPLATE.fullmatch(value)
        if match:
            if native_templates:
                path = match[1]
                data = context
                for part in path.split("."):
                    if isinstance(data, list):
                        raise UnsupportedSimulation(
                            f"Trama Mustache does not support indexed list reference '{path}'"
                        )
                    data = data.get(part) if isinstance(data, dict) else None
                if isinstance(data, (dict, list)):
                    raise UnsupportedSimulation(
                        f"Trama renders '{path}' as collection text, not typed JSON"
                    )
                return (
                    ""
                    if data is None
                    else str(data).lower()
                    if isinstance(data, bool)
                    else str(data)
                )
            return deepcopy(lookup(context, match[1]))
        if native_templates:

            def substitute(match):
                return render(match[0], context, native_templates=True)

            return _TEMPLATE.sub(substitute, value)
        return _TEMPLATE.sub(lambda match: str(lookup(context, match[1]) or ""), value)
    return value


def logic(expression, context):
    if isinstance(expression, list):
        return [logic(item, context) for item in expression]
    if not isinstance(expression, dict):
        return expression
    if len(expression) != 1:
        raise ValueError("JSONLogic expression must have exactly one operator")
    operator, operands = next(iter(expression.items()))
    if operator == "var":
        return lookup(context, operands if isinstance(operands, str) else operands[0])
    values = logic(operands if isinstance(operands, list) else [operands], context)
    if operator == "and":
        return all(values)
    if operator == "or":
        return any(values)
    if operator == "!":
        return not values[0]
    if operator == "in":
        return values[0] in values[1]
    from ritesmith.workflows.expressions import OPERATORS

    supported = {"==", "===", "!=", ">=", "<=", ">", "<", "+", "-", "*", "/"}
    if operator not in supported:
        if operator in OPERATORS:
            raise UnsupportedSimulation(f"Engine-supported operator '{operator}' is not simulated")
        raise ValueError(f"Unsupported JSONLogic operator: {operator}")
    a, b = values
    operations = {
        "==": lambda: a == b,
        "===": lambda: type(a) is type(b) and a == b,
        "!=": lambda: a != b,
        ">=": lambda: a >= b,
        "<=": lambda: a <= b,
        ">": lambda: a > b,
        "<": lambda: a < b,
        "+": lambda: a + b,
        "-": lambda: a - b,
        "*": lambda: a * b,
        "/": lambda: a / b,
    }
    if operator not in operations:
        raise ValueError(f"Unsupported JSONLogic operator: {operator}")
    return operations[operator]()


@dataclass
class SimulationResult:
    clock: float = 0
    calls: Counter = field(default_factory=Counter)
    nodes: dict = field(default_factory=dict)
    continuations: list[dict] = field(default_factory=list)
    compensations: list[dict] = field(default_factory=list)
    completed: bool = False
    steps: int = 0


class WorkflowSimulator:
    def __init__(
        self,
        definition,
        fixtures=None,
        *,
        payload=None,
        now=0,
        max_steps=10000,
        native_templates=False,
        transport=None,
    ):
        self.definition = definition
        self.fixtures = deepcopy(fixtures or {})
        self.payload = payload or {}
        self.result = SimulationResult(clock=now)
        self.max_steps = max_steps
        self.native_templates = native_templates
        self.transport = transport
        self.node_map = {node["id"]: node for node in definition["nodes"]}
        self.fixture_offsets = Counter()

    def fixture(self, node_id, capability, args):
        name = node_id if node_id in self.fixtures else capability
        if name not in self.fixtures:
            raise ValueError(f"Missing workflow fixture: {name}")
        values = self.fixtures[name]
        if not isinstance(values, list):
            return deepcopy(values)
        index = self.fixture_offsets[name]
        if index >= len(values):
            raise ValueError(f"Workflow fixture exhausted: {name}")
        self.fixture_offsets[name] += 1
        return deepcopy(values[index])

    def run(self):
        self.walk(self.definition["entrypoint"], self.result.nodes)
        return self.result

    def condition(self, expression, context):
        return (
            self.transport.condition(expression, context)
            if self.transport
            else logic(expression, context)
        )

    def walk(self, entry, nodes):
        from ritesmith.runtime.providers.stat import (
            _advance_chain,
            _collect,
            _max_value,
            _min_value,
            _tick,
        )

        current = entry
        last = None
        compensation_stack = []
        while current != "end":
            self.result.steps += 1
            if self.result.steps > self.max_steps:
                raise ValueError("Workflow failed to terminate within simulation budget")
            node = self.node_map[current]
            context = {
                "payload": self.payload,
                "nodes": nodes,
                "runtime": {"callback": {"url": "https://callback.example", "token": "fixture"}},
            }
            kind = node["kind"]
            if kind == "task":
                action = (
                    self.transport.action(node["action"], context)
                    if self.transport
                    else render(node["action"], context, self.native_templates)
                )
                body = action["request"].get("body", {})
                capability = body.get("capability_name") or body.get("artifact_id")
                args = body.get("input", {})
                if capability:
                    self.result.calls[capability] += 1
                    if capability == "stat.chain_step":
                        output = _advance_chain(now=self.result.clock, **args)
                    elif capability == "stat.collect":
                        output = _collect(**args)
                    elif capability == "stat.tick":
                        output = _tick(**args)
                    elif capability == "stat.min_value":
                        output = _min_value(**args)
                    elif capability == "stat.max_value":
                        output = _max_value(**args)
                    else:
                        output = self.fixture(current, capability, args)
                    last = (
                        {"error": output.message, "status": output.status}
                        if isinstance(output, HttpFailure)
                        else {"output": output}
                    )
                elif action["request"]["url"].endswith("/plans"):
                    self.result.continuations.append(body)
                    last = {"plan_id": "simulation"}
                elif action["request"]["url"].endswith("/complete"):
                    self.result.completed = True
                    last = {}
                else:
                    last = self.fixture(current, current, body)
                    if isinstance(last, HttpFailure):
                        last = {"error": last.message, "status": last.status}
                    if action.get("mode") == "async-http-callback" and not self.condition(
                        action["callback"]["successWhen"], {"callback": {"body": last}}
                    ):
                        raise ValueError("Callback fixture does not satisfy successWhen")
                if isinstance(last, dict) and last.get("error"):
                    # Trama unwinds completed tasks; the failed task is never pushed.
                    for compensation in reversed(compensation_stack):
                        if self.transport:
                            call = self.transport.action({"request": compensation}, context)[
                                "request"
                            ]
                        else:
                            call = render(compensation, context, self.native_templates)
                        self.result.compensations.append(call)
                    raise ValueError(f"Task failed: {current}")
                nodes[current] = {"response": {"body": last}}
                if node.get("compensation"):
                    compensation_stack.append(node["compensation"])
                current = node.get("next", "end")
            elif kind == "sleep":
                self.result.clock += node["durationSeconds"]
                current = node.get("next", "end")
            elif kind == "switch":
                current = next(
                    (
                        case["target"]
                        for case in node["cases"]
                        if self.condition(case["when"], context)
                    ),
                    node["default"],
                )
            elif kind == "split":
                start = self.result.clock
                clocks, branches = [], []
                for branch in node["branches"]:
                    self.result.clock = start
                    branch_nodes = {}
                    result = self.walk(branch, branch_nodes)
                    clocks.append(self.result.clock)
                    branches.append({"branchId": branch, "status": "SUCCEEDED", "result": result})
                self.result.clock = max(clocks)
                join = node["join"]
                last = {"branches": branches, "allSucceeded": True, "expected": len(branches)}
                nodes[join] = {"response": {"body": last}}
                current = self.node_map[join].get("next", "end")
            elif kind == "join":
                raise ValueError("Join may only be entered by its split")
            else:
                raise ValueError(f"Unknown node kind: {kind}")
        return last
