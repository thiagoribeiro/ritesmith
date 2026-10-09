"""Locally selected, short workflow examples. Selection changes guidance, not permission."""

from __future__ import annotations

import json
import re
import unicodedata

from ritesmith.schemas.test_spec import TEST_CONTEXT_PROPERTIES, test_context
from ritesmith.workflows.compact import compile_workflow

EXAMPLE_IDS = (
    "linear",
    "bounded_polling",
    "fixed_samples",
    "continuation",
    "parallel",
    "state_tracking",
    "content_monitor",
    "async_callback",
    "compensation",
)

WORKFLOW_CONTEXT_SCHEMA = {
    "properties": {
        **TEST_CONTEXT_PROPERTIES,
        "workflow_examples": {
            "type": "array",
            "items": {"type": "string", "enum": list(EXAMPLE_IDS)},
            "description": "Omit for local automatic selection; [] sends essential rules only. Examples guide generation without restricting node kinds. Unknown identifiers return 422.",
        },
    },
    "additionalProperties": True,
}


def validate_examples(context: dict | None) -> dict | None:
    if context is not None and "workflow_examples" in context:
        selected = context["workflow_examples"]
        if not isinstance(selected, list) or any(
            not isinstance(x, str) or x not in EXAMPLE_IDS for x in selected
        ):
            raise ValueError("workflow_examples must be a list of: " + ", ".join(EXAMPLE_IDS))
    return test_context(context)


def select_examples(goal: str, context: dict | None = None) -> list[str]:
    validate_examples(context)
    ctx = context or {}
    if "workflow_examples" in ctx:
        return list(dict.fromkeys(ctx["workflow_examples"]))
    text = unicodedata.normalize("NFKD", goal.lower()).encode("ascii", "ignore").decode()
    numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "quatro": 4,
        "cinco": 5,
        "seis": 6,
        "sete": 7,
        "oito": 8,
        "nove": 9,
        "dez": 10,
    }
    numeric_text = re.sub(r"\b(" + "|".join(numbers) + r")\b", lambda m: str(numbers[m[0]]), text)
    chosen = {"linear"}

    def has(pattern: str) -> bool:
        return bool(re.search(pattern, text))

    if has(r"monitor|poll|a cada|every|periodic|recorr|interval") or any(
        ctx.get(k) for k in ("schedule_hours", "runs_per_day", "trigger")
    ):
        chosen.add("bounded_polling")
    if has(
        r"amostra|sample|exactly|exatamente|vezes|checks|checagens|fetch.*sources|collect|gather|colet|captur"
    ):
        chosen.add("fixed_samples")
    try:
        count = float(ctx.get("duration_days") or 0) * float(ctx.get("runs_per_day") or 0)
    except (TypeError, ValueError):
        count = 0
    for key in ("sample_count", "num_samples", "samples", "iterations"):
        value = ctx.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            quantity = len(value) if isinstance(value, list) else float(value)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            chosen.add("fixed_samples")
            count = max(count, quantity)
    days = re.search(r"(\d+)\s*(?:dias?|days?)", numeric_text)
    daily = re.search(
        r"(\d+)\s*(?:vezes?|times|runs|checks|amostras?)\s*(?:por\s*dia|ao\s*dia|per\s*day|a\s*day|daily)",
        numeric_text,
    )
    samples = re.search(r"(\d+)\s*(?:vezes?|times|checks|amostras?|samples)", numeric_text)
    if samples:
        chosen.add("fixed_samples")
    if days and daily:
        count = max(count, int(days[1]) * int(daily[1]))
    elif samples:
        count = max(count, int(samples[1]))
    if (
        ctx.get("continuation") is not None
        or has(
            r"forever|indefinit|unbounded|sempre|continuamente|sem prazo|continuation|continue|continuacao|continuously"
        )
        or count > 20
    ):
        chosen.update(("continuation", "bounded_polling"))
    if has(r"paralel|parallel|independen|simultane|ao mesmo tempo|fan.out") or any(
        ctx.get(k) for k in ("parallel", "independent_tasks", "branches")
    ):
        chosen.add("parallel")
    if (
        has(r"minim|maxim|minimum|maximum|lowest|highest|acumul|previous|anterior|estado|state")
        or any(
            ctx.get(k) is not None
            for k in ("state", "previous_min", "previous_max", "previous_snapshot")
        )
        or bool(ctx.get("continuation"))
    ):
        chosen.add("state_tracking")
    content = has(r"noticia|news|feed|novidade|conteudo|content|status watch") or ctx.get(
        "monitor_type"
    ) in (
        "content",
        "news",
        "feed",
        "status",
    )
    if content and (
        "bounded_polling" in chosen or has(r"watch|new titles|snapshot|novos|novidade")
    ):
        chosen.update(("content_monitor", "bounded_polling", "state_tracking"))
    if has(r"callback|webhook|assincron|asynchron") or any(
        ctx.get(k) for k in ("callback_url", "webhook_url", "callback")
    ):
        chosen.add("async_callback")
    if has(r"compensa|rollback|undo|reverter|desfazer") or any(
        ctx.get(k) for k in ("compensation", "undo_url")
    ):
        chosen.add("compensation")
    return [x for x in EXAMPLE_IDS if x in chosen]


def task(id_: str, capability: str, input_: dict, next_: str = "end") -> dict:
    return {
        "id": id_,
        "kind": "task",
        "capability_name": capability,
        "input": input_,
        "next": next_,
    }


def example(id_: str) -> dict:
    """Compact examples share the compiler's explicit initialization behavior."""
    from ritesmith.workflows.compact import compact_workflow
    from ritesmith.workflows.constructors import semantic_example
    from ritesmith.workflows.semantic import compile_plan

    if id_ == "continuation":
        # A short indefinite-chain example avoids expanding 20 passes in a prompt.
        return {
            "name": id_,
            "entrypoint": "fetch",
            "nodes": [
                task("fetch", "market.coin_price", {"symbol": "btc"}, "wait"),
                {"id": "wait", "kind": "sleep", "durationSeconds": 60, "next": "continue"},
                {
                    "id": "continue",
                    "kind": "task",
                    "plan": {
                        "intent": "Monitor Bitcoin indefinitely every minute",
                        "mode": "execute",
                        "context": {
                            "continuation": {
                                "state": {
                                    "previous": "{{ nodes.fetch.response.body.output.price }}"
                                },
                            }
                        },
                    },
                    "next": "end",
                },
            ],
        }
    return compact_workflow(
        compile_plan(semantic_example(id_), "__RS_BASE_URL__"), "__RS_BASE_URL__"
    )


ESSENTIAL_RULES = """Trama v2 is a graph: entrypoint, nodes, name, version=2.0.0.
Nodes: task performs an action; sleep uses positive durationSeconds; switch uses cases [{name,when,target}] and REQUIRED default; split uses >=2 branches and one join; join uses next.
Unique ids. All references resolve. next=end terminates. Data is payload.* or nodes.ID.response.body.output.FIELD, never execution.input or execution.loop_count. Conditions are JSON-logic with var, comparisons, and/or/!.
Only declared capability input keys. Tools go through RiteSmith, never directly to provider APIs. Array capability outputs use output.result.
Sleep is for time, never data wiring. Loops need max_iterations. Polling sleep points back to fetch. Finite loops stop after the requested count; track counts with stat.tick. Indefinite or >20 iteration monitors use chains of at most 20 with a /plans continuation carrying required state and the original intent. For finite chains, carry remaining_iterations or an absolute end time; subtract completed work and stop at zero, never restart the full requested duration. JSON references to missing nodes or fields are errors. Initialize the first pass explicitly; subsequent passes may only read completed results. Never assume an absent reference becomes null. Semantic repeat.initial_state supplies defaults; continuation.state overrides them.
Content monitors compare snapshots via llm.evaluate, skip empty current snapshots, persist snapshots via stat.tick, and notify only decision=notify; result COUNT is not evidence of change. A nonempty snapshot after an empty one is new content.
Independent tasks may split/join. Branches have only payload and their own nodes; end branches with next=end, NEVER point at the join. After join read nodes.JOIN.response.body.branches and allSucceeded, not individual branch nodes.
Async callbacks require timeoutMillis and successWhen. Add compensation only for a reversible effect. Preserve explicit schedule, trigger, duration, constraints and continuation; never guess different values.
"""


def workflow_system(
    goal: str,
    context: dict | None,
    base_url: str,
    *,
    compact: bool,
    filtered: bool,
    mermaid: bool = False,
    semantic: bool = False,
    patterns: bool = False,
) -> tuple[str, list[str]]:
    selected = (
        select_examples(goal, context)
        if filtered or "workflow_examples" in (context or {})
        else list(EXAMPLE_IDS)
    )
    representation = (
        "Internal task shorthand: {id,kind:'task',capability_name OR artifact_id,input,next}; "
        "Python supplies HTTP/auth. Use {id,kind:'task',plan:{intent,mode,context},next} to chain /plans. "
        "For advanced HTTP/async actions use action in native Trama format; compensation stays native. "
        "Other node kinds keep native fields. Return definition with name,entrypoint,nodes and optional max_iterations/failureHandling."
        if compact
        else f"Task action is sync POST {base_url.rstrip('/')}/trama/execute, headers Authorization=Bearer __TRAMA_TOKEN__ and Content-Type=application/json; body has capability_name OR artifact_id and input; successStatusCodes=[200]."
    )
    rendered = []
    if patterns and not mermaid:
        from ritesmith.workflows.constructors import PATTERN_RULES, pattern_example

        return (
            "You generate Trama workflows. Return JSON only.\n"
            + ESSENTIAL_RULES
            + PATTERN_RULES
            + "\nEXAMPLES:\n"
            + "\n".join(
                identifier + ":" + json.dumps(pattern_example(identifier), separators=(",", ":"))
                for identifier in selected
            ),
            selected,
        )
    if semantic and not mermaid:
        from ritesmith.workflows.constructors import semantic_example
        from ritesmith.workflows.semantic import SEMANTIC_RULES

        return (
            "You generate Trama workflows. Return JSON only.\n"
            + ESSENTIAL_RULES
            + SEMANTIC_RULES
            + "\nEXAMPLES:\n"
            + "\n".join(
                identifier
                + ":"
                + json.dumps(
                    {"format": "semantic_plan", "plan": semantic_example(identifier)},
                    separators=(",", ":"),
                )
                for identifier in selected
            ),
            selected,
        )
    if mermaid:
        from ritesmith.workflows.mermaid import MERMAID_RULES, render_mermaid

        representation = MERMAID_RULES
    for id_ in selected:
        definition = example(id_)
        if mermaid:
            rendered.append(id_ + ":\n" + render_mermaid(definition))
            continue
        if not compact:
            definition = compile_workflow(definition, base_url)
        rendered.append(
            id_ + ":" + json.dumps(definition, separators=(",", ":"), ensure_ascii=False)
        )
    return (
        "You generate Trama workflows. Return JSON only.\n"
        + ESSENTIAL_RULES
        + representation
        + "\nEXAMPLES:\n"
        + "\n".join(rendered),
        selected,
    )
