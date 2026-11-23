"""A switch exit that finishes must name a real node: Trama rejects "end" there."""

from copy import deepcopy

from ritesmith.core.workflow_generation import _inject_completion_step
from ritesmith.workflows.compact import TERMINAL_NODE_ID, to_trama_definition
from ritesmith.workflows.semantic import compile_plan

READ = {"kind": "call", "id": "read", "capability_name": "stat.tick", "args": {}}
WHEN = {">": [{"ref": "read", "path": "v"}, 1]}


def _call(node_id):
    return {"kind": "call", "id": node_id, "capability_name": "stat.tick", "args": {}}


def _plan(*steps):
    return {"version": "workflow-plan-v1", "name": "t", "steps": [READ, *steps]}


def _node(definition, node_id):
    return next(n for n in definition["nodes"] if n["id"] == node_id)


def _assert_trama_ready(native):
    """What Trama's WorkflowDefinitionValidator checks: every target names a node."""
    ids = {n["id"] for n in native["nodes"]}
    assert native["failureHandling"]
    for node in native["nodes"]:
        assert "durationSeconds" not in node
        targets = [node.get("next"), node.get("default")]
        targets += [c["target"] for c in node.get("cases", [])]
        targets += node.get("branches", [])
        assert all(t is None or t in ids for t in targets), node


def test_trailing_condition_finishes_through_a_terminal_sleep():
    compiled = compile_plan(
        _plan({"kind": "condition", "when": WHEN, "steps": [_call("act")]}), "x"
    )
    switch = next(n for n in compiled["nodes"] if n["kind"] == "switch")
    assert switch["default"] == "end"  # compile_plan keeps RiteSmith's shorthand

    native = to_trama_definition(compiled)
    switch = next(n for n in native["nodes"] if n["kind"] == "switch")
    assert switch["default"] == TERMINAL_NODE_ID
    terminal = _node(native, TERMINAL_NODE_ID)
    assert terminal == {"id": TERMINAL_NODE_ID, "kind": "sleep", "durationMillis": 1, "next": None}
    _assert_trama_ready(native)


def test_empty_steps_case_target_is_routed_too():
    plan = _plan({"kind": "condition", "when": WHEN, "steps": [], "otherwise": [_call("act")]})
    native = to_trama_definition(compile_plan(plan, "x"))
    switch = next(n for n in native["nodes"] if n["kind"] == "switch")
    assert switch["cases"][0]["target"] == TERMINAL_NODE_ID and switch["default"] == "act"
    _assert_trama_ready(native)


def test_repeat_at_the_end_is_trama_ready():
    repeat = {
        "kind": "repeat",
        "id": "loop",
        "interval_seconds": 60,
        "count": 3,
        "steps": [_call("p")],
    }
    plan = {"version": "workflow-plan-v1", "name": "m", "steps": [repeat]}
    _assert_trama_ready(to_trama_definition(compile_plan(plan, "x")))


def test_no_terminal_node_when_nothing_finishes_in_a_switch():
    plan = _plan(
        {"kind": "condition", "when": WHEN, "steps": [_call("a")], "otherwise": [_call("b")]},
        _call("after"),
    )
    native = to_trama_definition(compile_plan(plan, "x"))
    assert all(n["id"] != TERMINAL_NODE_ID for n in native["nodes"])


def test_terminal_id_never_collides_with_a_plan_node():
    plan = _plan(
        _call(TERMINAL_NODE_ID), {"kind": "condition", "when": WHEN, "steps": [_call("a")]}
    )
    native = to_trama_definition(compile_plan(plan, "x"))
    ids = [n["id"] for n in native["nodes"]]
    assert len(ids) == len(set(ids))
    switch = next(n for n in native["nodes"] if n["kind"] == "switch")
    assert switch["default"] == f"{TERMINAL_NODE_ID}_1"
    _assert_trama_ready(native)


def test_branch_final_condition_on_the_ritesmith_path():
    # rs_complete only rewires top-level exits; a condition that ends a parallel branch
    # still said "end" and Trama rejected the whole workflow.
    when = {">": [{"ref": "b1", "path": "v"}, 1]}  # branches only see their own nodes
    condition = {"kind": "condition", "when": when, "steps": [_call("b1_act")]}
    branch = {"kind": "sequence", "steps": [_call("b1"), condition]}
    plan = _plan(
        {"kind": "parallel", "id": "par", "branches": [branch, _call("b2")]}, _call("done")
    )
    compiled = _inject_completion_step(deepcopy(compile_plan(plan, "x")))
    native = to_trama_definition(compiled)
    _assert_trama_ready(native)
    # The top-level path still completes the plan; only the branch ends at the terminal.
    assert _node(native, "done")["next"] == "rs_complete"


def test_input_definition_is_not_mutated():
    compiled = compile_plan(_plan({"kind": "condition", "when": WHEN, "steps": [_call("a")]}), "x")
    before = deepcopy(compiled)
    to_trama_definition(compiled)
    assert compiled == before


def test_compile_plan_carries_failure_handling():
    assert compile_plan(_plan(), "x")["failureHandling"] == {
        "type": "retry",
        "maxAttempts": 1,
        "delayMillis": 0,
    }
    custom = {"type": "retry", "maxAttempts": 3, "delayMillis": 500}
    assert compile_plan({**_plan(), "failureHandling": custom}, "x")["failureHandling"] == custom
