"""Nine composable semantic workflow constructors, sharing one typed compiler."""

from ritesmith.workflows.semantic import WorkflowPlan

CONSTRUCTOR_VERSION = "pattern-parameters-v2"


def construct(
    pattern,
    *,
    name,
    steps=None,
    branches=None,
    interval_seconds=60,
    count=3,
    state=None,
    initial_state=None,
    action=None,
    compensation=None,
    termination=None,
    after=None,
):
    steps = steps or []
    if pattern == "linear":
        operations = steps
    elif pattern in (
        "bounded_polling",
        "fixed_samples",
        "continuation",
        "state_tracking",
        "content_monitor",
    ):
        operation = {
            "kind": "repeat",
            "id": "monitor",
            "steps": steps,
            "interval_seconds": interval_seconds,
        }
        if termination is not None:
            from pydantic import TypeAdapter

            from ritesmith.llm.response_contracts import Termination, termination_fields

            operation.update(
                termination_fields(TypeAdapter(Termination).validate_python(termination))
            )
        else:
            operation.update(
                {"continuous": True} if pattern == "continuation" else {"count": count}
            )
        if state is not None:
            operation["state"] = state
        if initial_state is not None:
            operation["initial_state"] = initial_state
        operations = [operation, *(after or [])]
    elif pattern == "parallel":
        operations = [{"kind": "parallel", "id": "parallel", "branches": branches or []}, *steps]
    elif pattern == "async_callback":
        operations = [{"kind": "callback", "id": "callback", "action": action}, *steps]
    elif pattern == "compensation":
        if not steps or not compensation:
            raise ValueError("compensation needs an action and rollback")
        operations = [{**steps[0], "compensation": compensation}, *steps[1:]]
    else:
        raise ValueError(f"Unknown workflow pattern: {pattern}")
    return WorkflowPlan.model_validate({"name": name, "steps": operations}).model_dump(
        exclude_none=True, exclude_defaults=True
    )


def compile_parameters(candidate, base_url, **kwargs):
    from ritesmith.workflows.semantic import compile_plan

    parameters = dict(candidate["parameters"])
    pattern = parameters.pop("pattern")
    after = parameters.pop("after", [])
    if pattern == "parallel" or pattern == "async_callback":
        parameters["steps"] = after
    else:
        parameters["after"] = after
    plan = construct(pattern, name=candidate["name"], **parameters)
    return compile_plan(plan, base_url, **kwargs)


PATTERN_RULES = """
Prefer definition {format:"pattern_parameters",name,parameters:{pattern,...}} for
supported compositions. Patterns: linear(steps); parallel(branches,after);
bounded_polling/fixed_samples/continuation/state_tracking/content_monitor
(steps,interval_seconds,termination,state,initial_state,after); async_callback(action,after);
compensation(steps,compensation). Preserve conditions, tool arguments and data references.
Repeat termination is exactly {kind:"samples",count}, {kind:"duration",seconds},
{kind:"deadline",unix}, or {kind:"continuous"}; finite requests never use continuous.
The constructor creates monitor/monitor_tick/parallel/parallel_join mechanical IDs.
Business operations still use semantic kinds and IDs for data references.
Use after for final aggregation/notification after a finite repeat or parallel join.
Nested/multiple/branch repeats and pre-repeat actions needing continuation use
semantic_plan or compact_graph instead. Choose the escape in this SAME response.
"""


def pattern_example(pattern):
    from ritesmith.llm.response_contracts import PatternCandidate, operation_transport, packed

    plan = semantic_example(pattern)
    parameters = {"pattern": pattern}
    if pattern in ("linear", "compensation"):
        steps = [dict(item) for item in plan["steps"]]
        if pattern == "compensation":
            parameters["compensation_json"] = packed(steps[0].pop("compensation"))
        parameters["steps"] = [operation_transport(item) for item in steps]
    elif pattern == "parallel":
        parameters["branches"] = [
            operation_transport(item) for item in plan["steps"][0]["branches"]
        ]
        parameters["after"] = [operation_transport(item) for item in plan["steps"][1:]]
    elif pattern == "async_callback":
        parameters["action_json"] = packed(plan["steps"][0]["action"])
        parameters["after"] = [operation_transport(item) for item in plan["steps"][1:]]
    else:
        repeat = operation_transport(plan["steps"][0])
        parameters.update(
            {key: value for key, value in repeat.items() if key not in ("kind", "id")}
        )
    return PatternCandidate.model_validate(
        {
            "format": "pattern_parameters",
            "name": pattern,
            "parameters": parameters,
        }
    ).model_dump(exclude_none=True)


def semantic_example(pattern):
    fetch = {
        "kind": "call",
        "id": "fetch",
        "capability_name": "market.coin_price",
        "args": {"symbol": "btc"},
    }
    notify = {
        "kind": "call",
        "id": "notify",
        "capability_name": "telegram.send",
        "args": {
            "text": {"type": "template", "parts": ["BTC: ", {"ref": "fetch", "path": "price"}]}
        },
    }
    if pattern == "parallel":
        eth = {**fetch, "id": "eth", "args": {"symbol": "eth"}}
        notify["args"]["text"]["parts"].extend([" ETH: ", {"ref": "eth", "path": "price"}])
        return construct(pattern, name=pattern, branches=[fetch, eth], steps=[notify])
    if pattern in ("async_callback", "compensation"):
        action = {
            "mode": "sync",
            "request": {
                "url": "https://service.example/orders",
                "verb": "POST",
                "body": {"id": {"ref": "payload", "path": "id"}},
            },
            "successStatusCodes": [200],
        }
        if pattern == "async_callback":
            action.update(
                mode="async-http-callback",
                acceptedStatusCodes=[202],
                callback={
                    "timeoutMillis": 900000,
                    "successWhen": {"==": [{"var": "callback.body.status"}, "APPROVED"]},
                },
            )
            action.pop("successStatusCodes")
            action["request"]["body"].update(
                callbackUrl="{{ runtime.callback.url }}",
                callbackToken="{{ runtime.callback.token }}",
            )
            return construct(pattern, name=pattern, action=action)
        return construct(
            pattern,
            name=pattern,
            steps=[{"kind": "call", "id": "order", "action": action}],
            compensation={"url": "https://service.example/orders/rollback", "verb": "POST"},
        )
    if pattern == "fixed_samples":
        collect = {
            "kind": "state",
            "id": "samples",
            "capability_name": "stat.collect",
            "args": {
                "current": {"ref": "fetch"},
                "previous_samples": {"ref": "samples", "path": "samples"},
                "initial_samples": {"ref": "payload", "path": "continuation.state.samples"},
            },
        }
        return construct(
            pattern,
            name=pattern,
            count=2,
            steps=[fetch, collect],
            state={"samples": {"ref": "samples", "path": "samples"}},
            initial_state={"samples": []},
        )
    if pattern == "state_tracking":
        track = {
            "kind": "state",
            "id": "minimum",
            "capability_name": "stat.min_value",
            "args": {
                "current": {"ref": "fetch", "path": "price"},
                "previous_min": {"ref": "minimum", "path": "min_value"},
                "initial_min": {"ref": "payload", "path": "continuation.state.minimum"},
            },
        }
        return construct(
            pattern,
            name=pattern,
            steps=[fetch, track],
            state={"minimum": {"ref": "minimum", "path": "min_value"}},
            initial_state={"minimum": None},
        )
    if pattern == "content_monitor":
        search = {
            "kind": "call",
            "id": "fetch",
            "capability_name": "web.search",
            "args": {"query": "solar energy", "limit": 10},
        }
        evaluate = {
            "kind": "call",
            "id": "evaluate",
            "capability_name": "llm.evaluate",
            "args": {
                "task": "Notify nonempty current titles only when changed; skip an empty current snapshot.",
                "previous": {"ref": "monitor_tick", "path": "state.previous"},
                "current": {"ref": "fetch", "path": "result"},
            },
        }
        decision = {
            "kind": "condition",
            "id": "decide",
            "when": {"==": [{"ref": "evaluate", "path": "decision"}, "notify"]},
            "steps": [{**notify, "args": {"text": {"ref": "evaluate", "path": "message"}}}],
        }
        return construct(
            pattern,
            name=pattern,
            steps=[search, evaluate, decision],
            state={"previous": {"ref": "fetch", "path": "result"}},
            initial_state={"previous": ""},
        )
    return construct(
        pattern, name=pattern, steps=[fetch, notify] if pattern == "linear" else [fetch]
    )
