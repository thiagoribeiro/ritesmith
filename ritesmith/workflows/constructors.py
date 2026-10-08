"""Nine composable semantic workflow constructors, sharing one typed compiler."""

from ritesmith.workflows.semantic import WorkflowPlan


def construct(
    pattern,
    *,
    name,
    steps=None,
    branches=None,
    interval_seconds=60,
    count=3,
    state=None,
    action=None,
    compensation=None,
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
        operation.update({"continuous": True} if pattern == "continuation" else {"count": count})
        if state is not None:
            operation["state"] = state
        operations = [operation]
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
        "args": {"text": {"ref": "fetch", "path": "price"}},
    }
    if pattern == "parallel":
        eth = {**fetch, "id": "eth", "args": {"symbol": "eth"}}
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
                "task": "Notify new titles only; skip an empty previous snapshot.",
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
        )
    return construct(
        pattern, name=pattern, steps=[fetch, notify] if pattern == "linear" else [fetch]
    )
