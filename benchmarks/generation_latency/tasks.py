"""Twenty workflow acceptance scenarios; last four are held out from tuning."""

from benchmarks.luau_codegen.tasks import ALL_TASKS

ASSESSMENT_VERSION = 2

WORKFLOWS = [
    (
        "reminder",
        "In five minutes send me a Telegram reminder to go home",
        ["telegram.send"],
        [300],
    ),
    (
        "two_reminders",
        "Send hello on Telegram now, then send goodbye after ten minutes",
        ["telegram.send"],
        [600],
    ),
    (
        "quote",
        "Fetch the BTC quote and notify me on Telegram of its price",
        ["market.coin_price", "telegram.send"],
        [],
    ),
    (
        "three_samples",
        "Fetch BTC price exactly three times, two minutes apart; no notification",
        ["market.coin_price"],
        [120],
    ),
    (
        "four_samples",
        "Collect four ETH prices one minute apart, then send the four values on Telegram",
        ["market.coin_price", "telegram.send"],
        [60],
    ),
    (
        "threshold",
        "Check BTC every minute for three checks; notify on Telegram if price exceeds 100000 USD",
        ["market.coin_price", "telegram.send"],
        [60],
    ),
    (
        "minimum",
        "Check ETH every minute three times, track the running minimum and send the final minimum on Telegram",
        ["market.coin_price", "stat.min_value", "telegram.send"],
        [60],
    ),
    (
        "maximum",
        "Check BTC every five minutes three times, track maximum and send final maximum on Telegram",
        ["market.coin_price", "stat.max_value", "telegram.send"],
        [300],
    ),
    (
        "chain",
        "Monitor BTC indefinitely every minute and track global minimum across continuation chains",
        ["market.coin_price", "stat.min_value"],
        [60],
    ),
    (
        "week",
        "Monitor BTC four times daily for seven days, notify on Telegram when price is above 100000 USD",
        ["market.coin_price", "telegram.send"],
        [21600],
    ),
    (
        "parallel_prices",
        "Fetch BTC and ETH prices independently in parallel, then Telegram the aggregated results",
        ["market.coin_price", "telegram.send"],
        [],
    ),
    (
        "parallel_sources",
        "Search solar energy and battery news independently in parallel, then send both results on Telegram",
        ["web.search", "telegram.send"],
        [],
    ),
    (
        "news",
        "Watch solar energy news every hour for three checks, notify only on new titles, skip the first empty snapshot",
        ["web.search", "llm.evaluate", "stat.tick", "telegram.send_markdown"],
        [3600],
    ),
    (
        "conditional",
        "Fetch BTC price, notify Telegram only if greater than 100000; otherwise end",
        ["market.coin_price", "telegram.send"],
        [],
    ),
    (
        "artifact",
        "Invoke the supplied normalize artifact with payload.text, then send its output text on Telegram",
        ["telegram.send"],
        [],
    ),
    (
        "continuation_seed",
        "Continue indefinite hourly BTC minimum tracking using context.continuation.previous_min; preserve state in next chain",
        ["market.coin_price", "stat.min_value"],
        [3600],
    ),
    (
        "held_delay_parallel",
        "After seven minutes fetch BTC and ETH independently in parallel then Telegram both prices",
        ["market.coin_price", "telegram.send"],
        [420],
    ),
    (
        "held_chain_content",
        "Monitor battery news indefinitely every two hours; compare snapshots, notify new titles only and preserve previous snapshot across chains",
        ["web.search", "llm.evaluate", "stat.tick", "telegram.send_markdown"],
        [7200],
    ),
    (
        "held_callback",
        "Submit a POST to https://service.example/orders with payload.id and runtime callback URL/token. Wait for async callback APPROVED, timeout 900000 milliseconds",
        [],
        [],
    ),
    (
        "held_compensation",
        "Create an order with POST https://service.example/orders carrying payload.id, with POST https://service.example/orders/rollback as compensation, then send success on Telegram",
        ["telegram.send"],
        [],
    ),
]
WF_TASKS = []
for i, (name, goal, caps, sleeps) in enumerate(WORKFLOWS):
    task = {
        "id": "wf_" + name,
        "goal": goal,
        "kind": "trama_workflow",
        "caps": caps,
        "sleeps": sleeps,
        "held_out": i >= 16,
    }
    if name == "artifact":
        task["context"] = {
            "available_lua_artifacts": [
                {
                    "artifact_id": "art_benchmark_normalize",
                    "name": "normalize",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                }
            ]
        }
    if name == "continuation_seed":
        task["context"] = {"continuation": {"previous_min": 50000}}
    if name == "week":
        task["context"] = {"duration_days": 7, "runs_per_day": 4, "schedule_hours": 6}
    WF_TASKS.append(task)
TASKS = [{**t, "kind": "luau_script"} for t in ALL_TASKS] + WF_TASKS
COMPOUND_TASKS = []
for script_id, delay in (("t1_celsius_to_fahrenheit", 0), ("t1_slugify", 300)):
    script = next(t for t in ALL_TASKS if t["id"] == script_id)
    COMPOUND_TASKS.append(
        {
            "id": "plan_" + script_id,
            "kind": "trama_workflow",
            "script_task": script,
            "held_out": True,
            "goal": (
                "Create a reusable strict Luau function: "
                + script["goal"]
                + " Also create a Trama workflow invoking that NEW script artifact with workflow "
                "payload as its input, then sending its output on Telegram. "
                + ("Wait five minutes before invoking it." if delay else "Invoke it immediately.")
            ),
            "caps": ["telegram.send"],
            "sleeps": [delay] if delay else [],
        }
    )
BY_ID = {t["id"]: t for t in TASKS + COMPOUND_TASKS}
TRIAGE = {
    "t1_celsius_to_fahrenheit",
    "t1_percent_change",
    "t3_notify_inactive",
    "wf_reminder",
    "wf_week",
    "wf_parallel_prices",
}


def workflow_checks(task: dict, definition: dict) -> list[str]:
    errors = []
    nodes = definition.get("nodes", [])
    caps = {
        n.get("action", {}).get("request", {}).get("body", {}).get("capability_name")
        for n in nodes
        if isinstance(n.get("action", {}).get("request", {}).get("body", {}), dict)
    }
    for name in task["caps"]:
        aliases = (
            {"telegram.send", "telegram.send_markdown"}
            if name.startswith("telegram.send")
            else {name}
        )
        if (
            name == "market.coin_price"
            and "btc" in task["goal"].lower()
            and "eth" not in task["goal"].lower()
        ):
            aliases.add("market.bitcoin_price")
        if not (aliases & caps):
            errors.append("missing capability " + name)
    sleeps = {n.get("durationSeconds") for n in nodes if n.get("kind") == "sleep"}
    if not set(task["sleeps"]) <= sleeps:
        errors.append("explicit schedule not preserved")
    id_ = task["id"]
    if "parallel" in id_ and not any(n.get("kind") == "split" for n in nodes):
        errors.append("independent actions must run in parallel")
    if id_ in ("wf_chain", "wf_week", "wf_continuation_seed", "wf_held_chain_content"):
        if definition.get("max_iterations") != 20:
            errors.append("continuation chains must use 20 iterations")
        if not any(
            n.get("action", {}).get("request", {}).get("url", "").endswith("/plans") for n in nodes
        ):
            errors.append("missing continuation chain")
    if id_ == "wf_held_callback" and not any(
        n.get("action", {}).get("mode") == "async-http-callback" for n in nodes
    ):
        errors.append("missing async callback")
    if id_ == "wf_held_compensation" and not any(n.get("compensation") for n in nodes):
        errors.append("missing compensation")
    if id_ == "wf_artifact" and not any(
        n.get("action", {}).get("request", {}).get("body", {}).get("artifact_id")
        == "art_benchmark_normalize"
        for n in nodes
    ):
        errors.append("missing explicit artifact binding")
    errors.extend(_counter_progress_errors(nodes))
    return errors


def _counter_progress_errors(nodes):
    """Catch a counter-controlled cycle that always resets to the same value.

    This is a narrow semantic check, not a full workflow execution proof.
    """
    import json
    import re

    from ritesmith.workflows.validator import _v2_successors

    indexed = {n.get("id"): n for n in nodes}
    errors = []
    for node in nodes:
        body = node.get("action", {}).get("request", {}).get("body", {})
        if not isinstance(body, dict) or body.get("capability_name") != "stat.tick":
            continue
        id_ = node.get("id")
        reachable, pending = set(), _v2_successors(node)
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(_v2_successors(indexed.get(current)))
        conditions = json.dumps([indexed[n].get("cases", []) for n in reachable if n in indexed])
        if id_ not in reachable or f"nodes.{id_}.response.body.output.iteration" not in conditions:
            continue
        iteration = body.get("input", {}).get("iteration")
        if not isinstance(iteration, str) or not re.search(
            r"nodes\.[A-Za-z0-9_-]+\.response\.body\.output\.iteration", iteration
        ):
            errors.append(f"counter-controlled cycle resets iteration at {id_}")
    return errors
