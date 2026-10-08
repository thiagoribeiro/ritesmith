"""Offline Trama control-flow checks with a virtual clock and explicit tool responses.

This intentionally supports the generated JSONLogic/Mustache subset. It does not
claim equivalence with all Trama runtime features and never performs HTTP calls.
"""

import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field

_TEMPLATE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


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


def render(value, context):
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    if isinstance(value, str):
        match = _TEMPLATE.fullmatch(value)
        if match:
            return deepcopy(lookup(context, match[1]))
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
    def __init__(self, definition, fixtures=None, *, payload=None, now=0, max_steps=10000):
        self.definition = definition
        self.fixtures = deepcopy(fixtures or {})
        self.payload = payload or {}
        self.result = SimulationResult(clock=now)
        self.max_steps = max_steps
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
                action = render(node["action"], context)
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
                    last = {"output": output}
                elif action["request"]["url"].endswith("/plans"):
                    self.result.continuations.append(body)
                    last = {"plan_id": "simulation"}
                elif action["request"]["url"].endswith("/complete"):
                    self.result.completed = True
                    last = {}
                else:
                    last = self.fixture(current, current, body)
                    if action.get("mode") == "async-http-callback" and not logic(
                        action["callback"]["successWhen"], {"callback": {"body": last}}
                    ):
                        raise ValueError("Callback fixture does not satisfy successWhen")
                if isinstance(last, dict) and last.get("error"):
                    if node.get("compensation"):
                        self.result.compensations.append(render(node["compensation"], context))
                    raise ValueError(f"Task failed: {current}")
                nodes[current] = {"response": {"body": last}}
                current = node.get("next", "end")
            elif kind == "sleep":
                self.result.clock += node["durationSeconds"]
                current = node.get("next", "end")
            elif kind == "switch":
                current = next(
                    (case["target"] for case in node["cases"] if logic(case["when"], context)),
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
