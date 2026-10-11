"""One frozen, twelve-request comparison; never run against production storage.

The CLI runs production HTTP paths in-process on r2d2. Every paid nested call
shares a durable ledger. Existing benchmark stubs are replaced by fixture-only
production validation sessions, with an additional hidden functional oracle.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

from benchmarks.generation_latency.run import assess, payload
from benchmarks.generation_latency.tasks import BY_ID
from benchmarks.generation_latency.version import software_manifest
from ritesmith.llm.response_contracts import SCHEMA_VERSION, packed, response_model
from ritesmith.llm.spend_budget import SpendBudget, current_spend_budget
from ritesmith.llm.structured_output import strict_schema
from ritesmith.observability.costs import PRICES, estimated_cost
from ritesmith.workflows.constructors import CONSTRUCTOR_VERSION

CELLS = (
    ("wf_week", "generate", "A", False),
    ("wf_parallel_prices", "specialized", "C", False),
    ("t1_celsius_to_fahrenheit", "specialized", "B", False),
    ("t3_notify_inactive", "generate", "A", True),
    ("wf_week", "generate", "B", False),
    ("wf_parallel_prices", "specialized", "B", False),
    ("t1_celsius_to_fahrenheit", "specialized", "A", False),
    ("t3_notify_inactive", "generate", "B", True),
    ("wf_week", "generate", "C", False),
    ("wf_parallel_prices", "specialized", "A", False),
    ("plan_t1_celsius_to_fahrenheit", "plans", "A", False),
    ("plan_t1_celsius_to_fahrenheit", "plans", "C", False),
)


def notification_fixtures():
    customers = [
        {"id": id_, "name": name, "last_seen_days": days}
        for id_, name, days in (
            ("c1", "Ana", 45),
            ("c2", "Bruno", 3),
            ("c3", "Caio", 30),
            ("c4", "Duda", 90),
            ("c5", "Eva", 29),
        )
    ]
    result = [
        {
            "tool": "crm.list",
            "args": {"segment": "dormant"},
            "output": {"ok": True, "customers": customers},
        },
        {
            "tool": "crm.list",
            "args": {"segment": "ghost"},
            "output": {
                "ok": False,
                "error": "unknown_segment",
                "message": "segment does not exist",
            },
        },
    ]
    for customer in customers:
        id_ = customer["id"]
        result.append(
            {
                "tool": "message.send",
                "args": {"user": id_, "text": f"Hi {customer['name']}, we miss you!"},
                "output": {"ok": False, "error": "blocked", "message": "user blocked messages"}
                if id_ == "c4"
                else {"ok": True, "message_id": "m-" + id_},
            }
        )
    extra = [
        {"id": f"c{index}", "name": f"Customer{index}", "last_seen_days": 45}
        for index in range(6, 13)
    ]
    result.append(
        {
            "tool": "crm.list",
            "args": {"segment": "many"},
            "output": {"ok": True, "customers": [*customers, *extra]},
        }
    )
    for customer in extra:
        result.append(
            {
                "tool": "message.send",
                "args": {"user": customer["id"], "text": f"Hi {customer['name']}, we miss you!"},
                "output": {"ok": True, "message_id": "m-" + customer["id"]},
            }
        )
    return result


def hidden_cases(task):
    cases = []
    for case in task.get("test_cases", []):
        expected = case["expected"]
        if task["id"] == "t3_notify_inactive" and case["input"]["segment"] == "ghost":
            expected = {"error": "unknown_segment", "message": "segment does not exist"}
        cases.append(
            {
                "input": case["input"],
                "expected_output": expected,
                "expected_calls": case.get("expected_calls", {}),
                "source": "client",
                "tool_fixtures": notification_fixtures()
                if task["id"] == "t3_notify_inactive"
                else [],
            }
        )
    if task["id"] == "t3_notify_inactive":
        cases.append(
            {
                "input": {"segment": "many", "inactive_days": 0},
                "expected_output": {
                    "notified": [f"c{index}" for index in range(1, 11) if index != 4],
                    "failed": ["c4"],
                    "count": 9,
                },
                "expected_calls": {"crm.list": 1, "message.send": 10},
                "source": "client",
                "tool_fixtures": notification_fixtures(),
            }
        )
    return cases


def settings_update(variant):
    return {
        "llm_model": "gpt-5-mini",
        "llm_script_model": "gpt-5-mini",
        "llm_workflow_model": "gpt-5.4-mini",
        "llm_model_fast": "gpt-4.1-nano",
        "llm_reasoning_effort": "low",
        "llm_script_effort": "low",
        "llm_workflow_effort": "low",
        "generation_specialized_prompts": True,
        "generation_compact_workflows": True,
        "generation_mermaid_workflows": False,
        "generation_semantic_workflows": True,
        "generation_luau_assembly": True,
        "generation_unified_proposal": True,
        "generation_typed_prompts": True,
        "generation_parallel_tests": True,
        "generation_avoid_speculative_workflow_tests": True,
        "generation_bounded_recovery": True,
        "generation_deadline_seconds": 30,
        "generation_recovery_attempts": 1,
        "generation_max_attempts": 2,
        "generation_structured_outputs": variant != "A",
        "generation_pattern_parameters": variant == "C",
        "generation_structured_output_operations": None,
        "generation_pattern_parameter_operations": None,
        "require_strict_typecheck": True,
        "require_luau": True,
        "require_tests_min_risk": "medium",
    }


def install_fixture_validation(stage):
    from ritesmith.core.validation import ValidationPipeline
    from ritesmith.runtime import luau, validation_session
    from ritesmith.runtime.providers.base import HostFunctionDef
    from ritesmith.workflows.validator import WorkflowValidator

    original_tools = luau.luau_tools_for_profile
    original_signature = luau.tool_signature

    def tools(profile):
        current = stage.scope.get()
        if current is None:
            return original_tools(profile)
        task = current.task.get("script_task", current.task)

        def never_live(**kwargs):
            raise AssertionError("Evaluation cannot execute live tools")

        schemas = {
            "crm.list": {
                "type": "object",
                "properties": {"segment": {"type": "string"}},
                "required": ["segment"],
                "additionalProperties": False,
            },
            "message.send": {
                "type": "object",
                "properties": {"user": {"type": "string"}, "text": {"type": "string"}},
                "required": ["user", "text"],
                "additionalProperties": False,
            },
        }
        outputs = {
            "crm.list": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "customers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                                "last_seen_days": {"type": "number"},
                            },
                            "required": ["id", "name", "last_seen_days"],
                        },
                    },
                    "error": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["ok"],
            },
            "message.send": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "message_id": {"type": "string"},
                    "error": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["ok"],
            },
        }
        return {
            tool["name"]: HostFunctionDef(
                tool["name"],
                profile,
                never_live,
                tool["description"],
                schemas.get(tool["name"]),
                outputs.get(tool["name"]),
            )
            for tool in task.get("tools", [])
        }

    def signature(name, definition):
        current = stage.scope.get()
        task = current.task.get("script_task", current.task) if current else {}
        custom = next(
            (tool["signature"] for tool in task.get("tools", []) if tool["name"] == name), None
        )
        return (custom, None) if custom else original_signature(name, definition)

    async def validation(self, *, content, artifact_type, **kwargs):
        current = stage.scope.get()
        if current and artifact_type == "luau_script":
            task = current.task.get("script_task", current.task)
            if "test_cases" not in task:
                from ritesmith.schemas.artifact import ValidationResult

                return ValidationResult(valid=False, errors=["Unexpected script dependency"])
            kwargs["test_cases"] = [*hidden_cases(task), *(kwargs.get("test_cases") or [])]
        return await stage._original_run(
            self, content=content, artifact_type=artifact_type, **kwargs
        )

    luau.luau_tools_for_profile = tools
    validation_session.luau_tools_for_profile = tools
    validation_session.script_type_declarations = stage.script_types
    luau.tool_signature = signature
    validation_session.tool_signature = signature
    ValidationPipeline.run = validation
    original_workflow_validation = WorkflowValidator.validate

    def workflow_validation(self, definition):
        errors = original_workflow_validation(self, definition)
        current = stage.scope.get()
        if errors or current is None:
            return errors
        try:
            controlled_workflow_check(current.task, definition)
        except (ValueError, TypeError, KeyError, AssertionError) as exc:
            errors.append("Controlled workflow execution: " + str(exc))
        return errors

    WorkflowValidator.validate = workflow_validation


def controlled_workflow_check(task, definition):
    """Functional checks run inside validation timing, without external effects."""
    from ritesmith.workflows.semantic import compile_plan
    from ritesmith.workflows.simulation import WorkflowSimulator

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            self.observed.append((capability, args, self.result.clock))
            if capability in ("market.coin_price", "market.bitcoin_price"):
                symbol = args.get("symbol", "btc").lower()
                assert symbol in ("btc", "eth"), "Unexpected quote symbol"
                price = 111111 if symbol == "btc" else 2222
                if (
                    task["id"] == "wf_week"
                    and sum(name.startswith("market.") for name, _, _ in self.observed) % 2
                ):
                    price = 99999
                return {
                    "symbol": symbol,
                    "price": price,
                    "currency": "USD",
                }
            if capability in ("telegram.send", "telegram.send_markdown"):
                assert args.get("text"), "Empty notification"
                assert "None" not in packed(args), "Notification reads unavailable data"
                return {"ok": True, "message_id": "fixture"}
            if task.get("script_task") and capability.startswith("art_"):
                assert args.get("celsius") == 0, "Dependent script input not bound to payload"
                return {"fahrenheit": 32}
            return super().fixture(node_id, capability, args)

    graph = definition
    payload_ = {"celsius": 0} if task.get("script_task") else {}
    observed = []
    completed = False
    now = 0
    for _ in range(3):
        simulator = Simulator(graph, payload=payload_, now=now)
        simulator.observed = observed
        result = simulator.run()
        completed |= result.completed
        now = result.clock
        if not result.continuations:
            break
        assert len(result.continuations) == 1, "Unexpected continuation fanout"
        context = result.continuations[0]["context"]
        cached = context.get("workflow_continuation")
        assert cached and cached.get("plan"), "Continuation lacks reusable compiled plan"
        state = context["continuation"]
        assert state.get("remaining_iterations") is not None, "Finite monitor became continuous"
        graph = compile_plan(
            cached["plan"],
            "http://typed-validation",
            continuation={**state, "_resume_repeat": cached["completed_repeat"]},
            intent=cached["intent"],
            contract_version=cached["contract_version"],
        )
    else:
        raise ValueError("Finite workflow did not finish within three chunks")
    quotes = [(name, args, clock) for name, args, clock in observed if name.startswith("market.")]
    notifications = [
        (name, args, clock) for name, args, clock in observed if name.startswith("telegram.")
    ]
    if task["id"] == "wf_week":
        assert len(quotes) == 28, f"Expected 28 samples; got {len(quotes)}"
        assert len(notifications) == 14, "Only threshold-positive samples must notify"
        assert [clock for _, _, clock in notifications] == [
            index * 21600 for index in range(1, 28, 2)
        ], "Notification threshold or schedule is incorrect"
        assert [clock for _, _, clock in quotes] == [index * 21600 for index in range(28)], (
            "Wrong sampling schedule"
        )
    elif task["id"] == "wf_parallel_prices":
        assert len(quotes) == 2 and {args["symbol"].lower() for _, args, _ in quotes} == {
            "btc",
            "eth",
        }, "Missing independent quote"
        assert len(notifications) == 1, "Expected one aggregate notification"
        text = packed(notifications[0][1])
        assert "111111" in text and "2222" in text, "Aggregate notification lacks branch results"
    elif task.get("script_task"):
        assert completed, "Dependent plan lacks completion"
        assert len(notifications) == 1 and "32" in packed(notifications[0][1]), (
            "Dependent script result not propagated"
        )


def freeze_manifest():
    manifest = software_manifest()
    manifest.update(
        protocol="typed-decisions-v1",
        schema_version=SCHEMA_VERSION,
        constructor_version=CONSTRUCTOR_VERSION,
        cells=CELLS,
        prices=PRICES,
        pricing_sources=[
            f"https://developers.openai.com/api/docs/models/{model}"
            for model in ("gpt-5-mini", "gpt-5.4-mini", "gpt-4.1-nano")
        ],
        settings={variant: settings_update(variant) for variant in "ABC"},
        fixture_sha256=hashlib.sha256(packed(notification_fixtures()).encode()).hexdigest(),
        response_schemas={
            f"{method}:{variant}": strict_schema(response_model(method, variant == "C"))
            for method in (
                "proposal_script",
                "proposal_workflow",
                "luau_gen",
                "workflow_gen",
                "test_gen",
                "workflow_repair",
                "luau_repair",
            )
            for variant in "BC"
        },
    )
    return manifest


def paired_report(rows):
    pairs = []
    for task, entrypoint in dict.fromkeys((row["task"], row["entrypoint"]) for row in rows):
        variants = {
            row["variant"]: row
            for row in rows
            if row["task"] == task and row["entrypoint"] == entrypoint
        }
        for first, second in (("A", "B"), ("B", "C"), ("A", "C")):
            if first not in variants or second not in variants:
                continue
            before, after = variants[first], variants[second]
            pair = {
                "task": task,
                "entrypoint": entrypoint,
                "comparison": first + "→" + second,
                "before_valid": before["valid"],
                "after_valid": after["valid"],
                "latency_delta_s": after["duration_s"] - before["duration_s"],
                "cost_delta_usd": None
                if before["estimated_usd"] is None or after["estimated_usd"] is None
                else after["estimated_usd"] - before["estimated_usd"],
            }
            for field in (
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cached_tokens",
            ):
                pair[field + "_delta"] = sum(call.get(field, 0) for call in after["calls"]) - sum(
                    call.get(field, 0) for call in before["calls"]
                )
            # Failures cannot provide a matching cost-per-valid baseline.
            if (
                before["valid"]
                and after["valid"]
                and before["estimated_usd"]
                and after["estimated_usd"] is not None
            ):
                pair["cost_reduction_fraction"] = (
                    1 - after["estimated_usd"] / before["estimated_usd"]
                )
            pairs.append(pair)
    return pairs


async def run(output):
    from sqlalchemy.engine import make_url

    if (
        make_url(os.environ.get("RITESMITH_DATABASE_URL", "postgresql://localhost/absent")).database
        != "ritesmith_typed_validation"
    ):
        raise RuntimeError("A dedicated typed_validation database is required")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("No OpenAI credential configured")
    # Import the staging-only substitutions after the guards; never production app.
    from benchmarks.generation_latency import app as stage
    from ritesmith.api.deps import get_llm_provider
    from ritesmith.config import get_settings

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise RuntimeError("Frozen evaluation already exists; refusing a second campaign")
    manifest = freeze_manifest()
    verification = output / "verification.json"
    if not verification.exists():
        raise RuntimeError("Offline verification evidence is required before paid evaluation")
    verified = json.loads(verification.read_text())
    if not verified.get("passed") or verified.get("source_sha256") != manifest["source_sha256"]:
        raise RuntimeError("Offline verification does not match the frozen source")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ledger = SpendBudget(output / "spending.json")
    install_fixture_validation(stage)
    variant = "A"
    stage.app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update=settings_update(variant)
    )
    providers = {}

    def provider():
        if variant not in providers:
            providers[variant] = stage.BenchProvider(
                get_settings().model_copy(update=settings_update(variant)),
                client=stage.app.state.llm_provider.client,
            )
        return providers[variant]

    stage.app.dependency_overrides[get_llm_provider] = provider
    rows = []
    spending_token = current_spend_budget.set(ledger)
    try:
        async with (
            stage.app.router.lifespan_context(stage.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=stage.app, raise_app_exceptions=False),
                base_url="http://typed-validation",
            ) as client,
        ):
            for index, (task_id, entrypoint, variant, missing) in enumerate(CELLS):
                if ledger.state["blocked"]:
                    break
                cell = f"{index}:{task_id}:{variant}"
                ledger.request(cell)
                task = BY_ID[task_id]
                path, body = payload(task, entrypoint, missing)
                if task_id == "t3_notify_inactive":
                    body["context"]["test_fixtures"] = notification_fixtures()
                    body["constraints"]["runtime_profile"] = "notification"
                started = time.perf_counter()
                response = await client.post(
                    path,
                    json=body,
                    headers={
                        "x-benchmark-task": task_id,
                        "x-benchmark-phase": "full",
                        "x-benchmark-model": "gpt-5.4-mini",
                    },
                )
                seconds = time.perf_counter() - started
                try:
                    result = response.json()
                except ValueError:
                    result = {"error": response.text}
                calls = json.loads(response.headers.get("x-benchmark-calls", "[]"))
                errors = (
                    assess(task, result, entrypoint)
                    if response.status_code < 400
                    else [f"HTTP {response.status_code}"]
                )
                row = {
                    "cell": cell,
                    "task": task_id,
                    "entrypoint": entrypoint,
                    "variant": variant,
                    "client_tests": not missing,
                    "concurrency": 1,
                    "status": response.status_code,
                    "duration_s": seconds,
                    "valid": not errors,
                    "valid_within_10s": not errors and seconds <= 10,
                    "errors": errors,
                    "calls": calls,
                    "stages": json.loads(response.headers.get("x-benchmark-stages", "[]")),
                    "estimated_usd": estimated_cost(calls),
                    "response": result,
                    "source_sha256": manifest["source_sha256"],
                }
                if not calls:
                    row["estimated_usd"] = None
                if not calls or row["estimated_usd"] is None:
                    ledger.stop("missing_request_usage_or_trace")
                rows.append(row)
                with (output / "responses.jsonl").open("a") as file:
                    file.write(packed(row) + "\n")
                print(
                    packed(
                        {
                            key: row[key]
                            for key in (
                                "cell",
                                "valid",
                                "duration_s",
                                "estimated_usd",
                                "errors",
                            )
                        }
                    ),
                    flush=True,
                )
    finally:
        current_spend_budget.reset(spending_token)
        total = (
            None
            if any(call["status"] != "settled" for call in ledger.state["calls"])
            else sum(float(call["actual_usd"]) for call in ledger.state["calls"])
        )
        report = {
            "protocol": "typed-decisions-v1",
            "requests": len(rows),
            "planned_requests": 12,
            "unexecuted_cells": CELLS[len(rows) :],
            "blocked": ledger.state["blocked"],
            "estimated_usd": total,
            "provider_calls": len(ledger.state["calls"]),
            "maximum_reserved_or_settled_usd": sum(
                float(call.get("actual_usd", call["reserved_usd"]))
                for call in ledger.state["calls"]
            ),
            "pairs": paired_report(rows),
            "general_slo_qualified": False,
            "production_promoted": False,
            "limitations": [
                "One observation per cell; no statistical SLO qualification.",
                "Controlled local execution does not certify the complete Trama engine.",
                "Cached tokens and reasoning tokens are recorded separately.",
            ],
        }
        (output / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.output))
