"""Frozen pre-change workflow user prompt for controlled ablations."""

import json

from ritesmith.llm.prompts import _sanitize_goal


def workflow_generation_user(
    goal: str,
    available_capabilities: list[dict],
    constraints: dict,
    similar_workflows: list[dict],
    response_schema: str,
    context: dict | None = None,
) -> str:
    caps_json = json.dumps(
        [
            {
                "capability_name": c.get("capability_name") or c.get("capability_id"),
                "description": c.get("description", ""),
                "input_schema": c.get("input_schema") or {},
                "output_schema": c.get("output_schema") or {},
            }
            for c in available_capabilities[:25]
            if c.get("capability_name") or c.get("capability_id")
        ],
        indent=2,
    )

    parts = [
        f"GOAL: {_sanitize_goal(goal)}",
        (
            "AVAILABLE CAPABILITIES\n"
            "Use 'capability_name' in the POST body to /trama/execute.\n"
            "Access node output via: {{ nodes.<node_id>.response.body.output.<field> }}\n"
            "  — If the capability returns a JSON object, <field> is a top-level key.\n"
            "  — If the capability returns a JSON array, use the key 'result'.\n"
            f"{caps_json}"
        ),
    ]

    _mon_keys = ("schedule_hours", "duration_days", "runs_per_day", "trigger", "notify_style")
    _mon = {k: (context or {}).get(k) for k in _mon_keys if (context or {}).get(k) is not None}
    if _mon:
        _lines = ["USER-SPECIFIED PARAMETERS (use these EXACTLY — do NOT re-infer):"]
        if _mon.get("schedule_hours"):
            _lines.append(
                f"- run every {_mon['schedule_hours']}h -> sleep node durationSeconds = "
                f"{int(_mon['schedule_hours']) * 3600}"
            )
        if _mon.get("duration_days") and _mon.get("runs_per_day"):
            _iters = int(_mon["duration_days"]) * int(_mon["runs_per_day"])
            _lines.append(
                f"- run for {_mon['duration_days']} days x {_mon['runs_per_day']}/day -> "
                f"max_iterations = {_iters} (cap 20; use Pattern C cron-chain if it would exceed 20)"
            )
        elif _mon.get("duration_days"):
            _lines.append(
                f"- run for {_mon['duration_days']} days -> size max_iterations to match the sleep interval"
            )
        if _mon.get("trigger"):
            _lines.append(f"- a notification is warranted ONLY when: {_mon['trigger']}")
        if _mon.get("notify_style"):
            _lines.append(
                f"- notification style: {_mon['notify_style']} "
                "(resumo = short summary of what changed; aviso = one-line heads-up)"
            )
        parts.append("\n".join(_lines))

    lua_artifacts = (context or {}).get("available_lua_artifacts", [])
    if lua_artifacts:
        lua_json = json.dumps(lua_artifacts, indent=2)
        parts.append(
            f"AVAILABLE LUA ARTIFACTS (use artifact_id instead of capability_name for these):\n{lua_json}"
        )

    relevant = {k: v for k, v in constraints.items() if v is not None}
    if relevant:
        parts.append(f"CONSTRAINTS:\n{json.dumps(relevant, indent=2)}")

    if similar_workflows:
        parts.append("SIMILAR WORKFLOWS FOR REFERENCE (structure only):")
        for wf in similar_workflows[:2]:
            parts.append(f"--- {wf.get('name', 'unknown')}: {wf.get('description', '')} ---")
            if wf.get("content"):
                parts.append(wf["content"][:1200])

    parts.append(f"\nRespond with JSON matching this schema exactly:\n{response_schema}")
    return "\n\n".join(parts)
