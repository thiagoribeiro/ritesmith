"""Progressive, stop-on-failure verification. Never deploy or contact live tools.

Both stages are frozen together. An unsuccessful offline qualification records
all cells as unexecuted; it cannot be bypassed with a command-line option.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path

import httpx

from benchmarks.generation_latency import typed_decisions as previous
from benchmarks.generation_latency.run import assess, payload
from benchmarks.generation_latency.tasks import BY_ID
from benchmarks.generation_latency.version import software_manifest
from ritesmith.llm.response_contracts import packed
from ritesmith.llm.spend_budget import SpendBudget, current_spend_budget
from ritesmith.observability.costs import PRICE_VERSION, PRICES

PROTOCOL = "reliability-v2"
STAGE_1 = (
    ("t1_celsius_to_fahrenheit", "specialized", False),
    ("t3_notify_inactive", "generate", True),
    ("wf_week", "generate", False),
)
STAGE_2 = (
    ("t1_celsius_to_fahrenheit", "specialized", False),
    ("wf_reminder", "specialized", False),
    ("plan_t1_celsius_to_fahrenheit", "plans", False),
    ("t3_notify_inactive", "generate", True),
    ("wf_threshold", "specialized", False),
    ("t1_percent_change", "specialized", False),
    ("wf_parallel_prices", "specialized", False),
    ("wf_news", "specialized", False),
    ("t1_group_totals", "specialized", False),
    ("wf_week", "generate", False),
    ("plan_t1_slugify", "plans", False),
    ("wf_four_samples", "specialized", False),
    ("t2_customer_summary", "specialized", False),
    ("wf_chain", "generate", False),
    ("t3_notify_inactive", "generate", True),
    ("wf_minimum", "specialized", False),
    ("t4_balanced_brackets", "specialized", False),
    ("wf_held_callback", "specialized", False),
    ("wf_parallel_prices", "specialized", False),
    ("plan_t1_celsius_to_fahrenheit", "plans", False),
    ("wf_held_compensation", "specialized", False),
    ("t1_celsius_to_fahrenheit", "specialized", False),
    ("wf_news", "specialized", False),
    ("wf_week", "generate", False),
)
SETTINGS = previous.settings_update("A")
SETTINGS["generation_workflow_typed_json"] = True


def notification_fixtures():
    fixtures = deepcopy(previous.notification_fixtures())
    listing = next(f for f in fixtures if f["args"] == {"segment": "many"})
    for index in (13, 14):
        customer = {"id": f"c{index}", "name": f"Customer{index}", "last_seen_days": 45}
        listing["output"]["customers"].append(customer)
        fixtures.append(
            {
                "tool": "message.send",
                "args": {"user": customer["id"], "text": f"Hi {customer['name']}, we miss you!"},
                "output": {"ok": True, "message_id": "m-" + customer["id"]},
            }
        )
    return fixtures


def fixtures_for(task):
    """Record the original pure stubs with reference inputs, never external tools."""
    if task["id"] == "t3_notify_inactive":
        return notification_fixtures()
    if not task.get("tools"):
        return []
    from lunardyson import Runtime

    fixtures, stubs = [], []
    runtime = Runtime(memory_mb=32, cpu_time_ms=1000)
    try:
        runtime.declare_types("type Context = {[string]: any}\n" + task["types"])
        for tool in task["tools"]:
            stub = Runtime(memory_mb=16, cpu_time_ms=1000)
            stubs.append(stub)
            source = (
                "local stub = "
                + tool["stub"]
                + "\nfunction run(input, context) return stub(input) end"
            )

            def handler(args, stub=stub, source=source, name=tool["name"]):
                response = stub.execute(source, args, {})
                if not response.ok:
                    raise ValueError(response.message)
                fixture = {"tool": name, "args": args, "output": response.output}
                if fixture not in fixtures:
                    fixtures.append(fixture)
                return response.output

            runtime.tool(tool["name"], handler, signature=tool["signature"], effect="pure")
        for case in task["test_cases"]:
            result = runtime.execute(task["reference"], case["input"], {})
            if not result.ok:
                raise ValueError("Reference fixture recording failed: " + result.message)
    finally:
        runtime.close()
        for stub in stubs:
            stub.close()
    return fixtures


def hidden_cases(task):
    cases = []
    fixtures = fixtures_for(task)
    for case in task.get("test_cases", []):
        expected = case["expected"]
        cases.append(
            {
                "input": case["input"],
                "assertions": [
                    {"path": key, "op": "equals", "value": value} for key, value in expected.items()
                ],
                "expected_calls": case.get("expected_calls", {}),
                "tool_fixtures": fixtures,
                "source": "client",
            }
        )
    if task["id"] == "t3_notify_inactive":
        cases.append(
            {
                "input": {"segment": "many", "inactive_days": 30},
                "expected_output": {
                    "notified": ["c1", "c3", *[f"c{i}" for i in range(6, 13)]],
                    "failed": ["c4"],
                    "count": 9,
                },
                "expected_calls": {"crm.list": 1, "message.send": 10},
                "tool_fixtures": fixtures,
                "source": "fixture",
            }
        )
    return cases


def request_spec(task, entrypoint, missing):
    path, body = payload(task, entrypoint, missing)
    script = task.get("script_task", task)
    if script.get("test_cases"):
        if not missing:
            body["context"]["test_cases"] = hidden_cases(script)
        fixtures = fixtures_for(script)
        if fixtures:
            body["context"]["test_fixtures"] = fixtures
    if task["id"] == "t3_notify_inactive":
        body["constraints"]["runtime_profile"] = "notification"
    return path, body


def manifest():
    identity = software_manifest()
    root = Path(__file__).resolve().parents[2]
    # Include the separate MCP entry point and transport probe in this freeze.
    for file in [
        *sorted((root / "mcp-server").glob("*.py")),
        root / "pyproject.toml",
        root / "benchmarks/generation_latency/TramaTemplateOracle.java",
        root / "benchmarks/generation_latency/TramaTypedJsonOracle.java",
    ]:
        identity["files"][str(file.relative_to(root))] = hashlib.sha256(
            file.read_bytes()
        ).hexdigest()
    identity["source_sha256"] = hashlib.sha256(
        json.dumps(identity["files"], sort_keys=True).encode()
    ).hexdigest()
    stages = []
    for number, cap, cells in ((1, "0.10", STAGE_1), (2, "1.00", STAGE_2)):
        frozen = []
        for index, (task_id, entrypoint, missing) in enumerate(cells):
            task = BY_ID[task_id]
            path, body = request_spec(task, entrypoint, missing)
            frozen.append(
                {
                    "cell": f"stage{number}:{index}:{task_id}",
                    "task": task_id,
                    "entrypoint": entrypoint,
                    "missing_tests": missing,
                    "path": path,
                    "body": body,
                    "expectations": hidden_cases(task.get("script_task", task)),
                    "workflow_expectations": task["goal"]
                    if task["kind"] == "trama_workflow"
                    else None,
                }
            )
        stages.append(
            {"stage": number, "limit_usd": cap, "max_requests": len(cells), "cells": frozen}
        )
    identity.update(
        protocol=PROTOCOL,
        settings=SETTINGS,
        stages=stages,
        price_version=PRICE_VERSION,
        prices=PRICES,
        concurrency=1,
        reuse=False,
        deployment_authorized=False,
    )
    return identity


BOUND_SCRIPTS = {}
TRAMA_TRANSPORT = None


def workflow_check(task, definition):
    from ritesmith.config import Settings
    from ritesmith.runtime.validation_session import LuauValidationSession
    from ritesmith.schemas.test_spec import TestSpec
    from ritesmith.workflows.semantic import compile_plan
    from ritesmith.workflows.simulation import WorkflowSimulator
    from ritesmith.workflows.transport import typed_json_workflow

    if TRAMA_TRANSPORT is None:
        raise RuntimeError("Functional qualification requires the real Trama transport oracle")

    observed = []
    script_task = task.get("script_task")
    seed = script_task["test_cases"][0]["input"] if script_task else {"id": "fixture-order"}
    quotes = 0

    class Simulator(WorkflowSimulator):
        def fixture(self, node_id, capability, args):
            nonlocal quotes
            observed.append((capability, deepcopy(args), self.result.clock))
            if capability.startswith("market."):
                quotes += 1
                symbol = args.get("symbol", "btc").lower()
                assert symbol in ("btc", "eth"), "Unexpected quote symbol"
                price = 111111 if symbol == "btc" else 2222
                if task["id"] in ("wf_week", "wf_threshold"):
                    price = 99999 if quotes % 2 else 111111
                elif task["id"] in ("wf_four_samples", "wf_minimum", "wf_chain"):
                    price = (
                        4000 + quotes if task["id"] == "wf_chain" and quotes > 20 else 3000 - quotes
                    )
                return {"symbol": symbol, "price": price, "currency": "USD"}
            if capability.startswith("telegram."):
                assert isinstance(args.get("text"), str) and args["text"], (
                    "Notification must be text"
                )
                assert "None" not in args["text"], "Unavailable result in notification"
                return {"ok": True, "message_id": "fixture"}
            if capability == "web.search":
                count = sum(name == capability for name, _, _ in observed)
                return {"result": "" if count == 1 else "New solar title"}
            if capability == "llm.evaluate":
                changed = bool(args.get("current")) and args.get("previous") != args.get("current")
                return {"decision": "notify" if changed else "skip", "message": "New solar title"}
            if capability.startswith("art_"):
                assert capability in BOUND_SCRIPTS, "Unknown uncertified script binding"
                content = BOUND_SCRIPTS[capability]
                session = LuauValidationSession(
                    Settings(),
                    "transform_only",
                    script_task["input_schema"],
                    script_task["output_schema"],
                )
                try:
                    result, fixture_error, errors = session.execute(content, TestSpec(input=args))
                    assert result.ok and not fixture_error and not errors, (
                        "Bound script execution failed"
                    )
                    return result.output
                finally:
                    session.close()
            return (
                {"status": "APPROVED"}
                if task["id"] == "wf_held_callback"
                else {"id": "fixture-order"}
            )

    graph, now, chunks, completed = definition, 0, [], False
    for index in range(3):
        simulation = Simulator(
            graph, payload=seed, now=now, native_templates=True, transport=TRAMA_TRANSPORT
        )
        result = simulation.run()
        completed |= result.completed
        now = result.clock
        if not result.continuations:
            break
        assert len(result.continuations) == 1, "Unexpected continuation fanout"
        context = result.continuations[0]["context"]
        cached, state = context["workflow_continuation"], context["continuation"]
        assert isinstance(state.get("state"), dict), "Continuation state must remain an object"
        chunks.append(state)
        if task["id"] == "wf_chain" and index == 1:
            break
        assert state.get("remaining_iterations") is not None or task["id"] == "wf_chain", (
            "Finite monitor became continuous"
        )
        # Mirror /plans auto-execution, which carries both the nested and legacy state.
        seed = {**state, "continuation": state}
        graph = typed_json_workflow(
            compile_plan(
                cached["plan"],
                "http://candidate",
                continuation={**state, "_resume_repeat": cached["completed_repeat"]},
                intent=cached["intent"],
                contract_version=cached["contract_version"],
            )
        )
    else:
        raise ValueError("Finite workflow exceeded three chunks")
    market = [(name, args, clock) for name, args, clock in observed if name.startswith("market.")]
    messages = [(args, clock) for name, args, clock in observed if name.startswith("telegram.")]
    if task["id"] == "wf_week":
        assert len(market) == 28 and chunks[0]["remaining_iterations"] == 8
        assert [clock for _, _, clock in market] == [i * 21600 for i in range(28)]
        assert [clock for _, clock in messages] == [i * 21600 for i in range(1, 28, 2)]
    elif task["id"] == "wf_parallel_prices":
        assert len(market) == 2 and {args["symbol"].lower() for _, args, _ in market} == {
            "btc",
            "eth",
        }
        assert len(messages) == 1 and all(
            str(value) in messages[0][0]["text"] for value in (111111, 2222)
        )
    elif task["id"] == "wf_reminder":
        assert len(messages) == 1 and messages[0][1] == 300
    elif task["id"] == "wf_threshold":
        assert len(market) == 3 and [clock for _, clock in messages] == [60]
    elif task["id"] in ("wf_four_samples", "wf_minimum"):
        count = 4 if task["id"] == "wf_four_samples" else 3
        assert [clock for _, _, clock in market] == [i * 60 for i in range(count)]
        assert len(messages) == 1 and messages[0][1] == (count - 1) * 60
        values = range(3000 - count, 3000) if count == 4 else [2997]
        assert all(str(value) in messages[0][0]["text"] for value in values)
    elif task["id"] == "wf_chain":
        assert len(market) == 40 and len(chunks) == 2
        assert chunks[0]["state"].get("minimum") == 2980
        assert chunks[1]["state"].get("minimum") == 2980, "Global minimum lost across continuation"
        assert [clock for _, _, clock in market] == [i * 60 for i in range(40)]
    elif task["id"] == "wf_news":
        assert len(messages) == 1 and messages[0][1] == 3600
    elif script_task:
        assert completed and len(messages) == 1
        expected = script_task["test_cases"][0]["expected"]
        assert all(str(value) in messages[0][0]["text"] for value in expected.values())
        script_calls = [clock for name, _, clock in observed if name.startswith("art_")]
        assert script_calls == [300 if task["id"].endswith("slugify") else 0]


def write_report(output, frozen, rows, reason):
    stages = []
    for spec in frozen["stages"]:
        executed = [row for row in rows if row["stage"] == spec["stage"]]
        ledger_path = output / f"stage{spec['stage']}-spending.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"calls": []}
        calls = ledger["calls"]
        known = all(call["status"] == "settled" for call in calls)
        stages.append(
            {
                "stage": spec["stage"],
                "executed": len(executed),
                "planned": spec["max_requests"],
                "correct_within_10s": sum(row["accepted"] for row in executed),
                "calls": len(calls),
                "cost_usd": sum(float(c["actual_usd"]) for c in calls) if known else None,
                "unexecuted": [
                    c["cell"]
                    for c in spec["cells"]
                    if c["cell"] not in {r["cell"] for r in executed}
                ],
            }
        )
    report = {
        "protocol": PROTOCOL,
        "source_sha256": frozen["source_sha256"],
        "stages": stages,
        "stopping_reason": reason,
        "recommendation": "retain_restored_production"
        if reason
        else "review_candidate_without_deployment",
        "production_changed": False,
        "general_slo_qualified": False,
        "rows": rows,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    lines = [
        "# Reliability recovery verification",
        "",
        f"Source: `{frozen['source_sha256']}`.",
        "",
        "Production was not deployed or modified by this verification.",
        "",
        "| Stage | Executed / planned | Correct and ≤10s | Known token cost |",
        "|---|---:|---:|---:|",
    ]
    for stage in stages:
        cost = "unknown" if stage["cost_usd"] is None else f"${stage['cost_usd']:.6f}"
        lines.append(
            f"| {stage['stage']} | {stage['executed']}/{stage['planned']} | {stage['correct_within_10s']}/{stage['executed']} | {cost} |"
        )
    lines += [
        "",
        f"Stopping reason: {reason or 'Both stages completed; review required.'}",
        "",
        "Unexecuted cells are not failed deliveries and provide no accuracy evidence.",
        "These observations do not qualify the 95%/10-second target or prove 50% savings.",
        "",
        f"Recommendation: `{report['recommendation']}`.",
        "",
        "Full request data, diagnostics, usage and original provider responses are in the adjacent JSON files.",
    ]
    (output / "RESULTS.md").write_text("\n".join(lines) + "\n")


async def run(output):
    global TRAMA_TRANSPORT
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise RuntimeError("Existing frozen campaign: resubmission is forbidden")
    frozen = manifest()
    verification = json.loads((output / "verification.json").read_text())
    if verification["source_sha256"] != frozen["source_sha256"]:
        raise RuntimeError("Offline verification does not match source")
    frozen["native_engine"] = verification.get("native_engine")
    (output / "manifest.json").write_text(json.dumps(frozen, indent=2))
    if not verification.get("passed"):
        write_report(
            output,
            frozen,
            [],
            "offline_qualification_failed: " + verification.get("reason", "see verification.json"),
        )
        return
    if not verification.get("candidate_image") or not verification.get("trama_transport_verified"):
        raise RuntimeError(
            "Frozen candidate image and actual Trama transport qualification required"
        )
    from benchmarks.generation_latency.native_transport import TramaTransportOracle, artifact_hashes

    if artifact_hashes() != verification.get("trama_oracle_files"):
        raise RuntimeError("Native Trama oracle does not match the verified frozen artifacts")

    TRAMA_TRANSPORT = TramaTransportOracle()
    assert TRAMA_TRANSPORT.render({"ready": "{{payload.ready}}"}, {"payload": {"ready": 1}}) == {
        "ready": 1
    }
    from sqlalchemy.engine import make_url

    if make_url(os.environ["RITESMITH_DATABASE_URL"]).database != "ritesmith_reliability":
        raise RuntimeError("Dedicated reliability storage required")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("No credential configured")
    from benchmarks.generation_latency import app as stage
    from ritesmith.api.deps import get_llm_provider
    from ritesmith.config import get_settings
    from ritesmith.core.generation import GenerationService

    previous.hidden_cases = hidden_cases
    previous.controlled_workflow_check = workflow_check
    previous.install_fixture_validation(stage)
    original_generate = GenerationService.generate_lua

    async def capture_script(self, *args, **kwargs):
        response = await original_generate(self, *args, **kwargs)
        if response.artifact.metadata.get("certification") == "strict":
            BOUND_SCRIPTS[response.artifact.artifact_id] = response.artifact.content
        return response

    GenerationService.generate_lua = capture_script
    settings = get_settings().model_copy(update=SETTINGS)
    stage.app.dependency_overrides[get_settings] = lambda: settings
    provider = None

    def llm():
        nonlocal provider
        if provider is None:
            provider = stage.BenchProvider(settings, client=stage.app.state.llm_provider.client)
        return provider

    stage.app.dependency_overrides[get_llm_provider] = llm
    rows, reason = [], None
    try:
        async with (
            stage.app.router.lifespan_context(stage.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=stage.app, raise_app_exceptions=False),
                base_url="http://candidate",
            ) as client,
        ):
            for spec in frozen["stages"]:
                if spec["stage"] == 2 and (
                    len(rows) != 3 or not all(row["accepted"] for row in rows)
                ):
                    reason = "stage1_gate_not_met"
                    break
                ledger = SpendBudget(
                    output / f"stage{spec['stage']}-spending.json",
                    limit_usd=spec["limit_usd"],
                    max_requests=spec["max_requests"],
                )
                token = current_spend_budget.set(ledger)
                try:
                    for cell in spec["cells"]:
                        ledger.request(cell["cell"])
                        started = time.perf_counter()
                        response = await client.post(
                            cell["path"],
                            json=cell["body"],
                            headers={
                                "x-benchmark-task": cell["task"],
                                "x-benchmark-phase": "full",
                                "x-benchmark-model": "gpt-5.4-mini",
                            },
                        )
                        duration = time.perf_counter() - started
                        data = response.json()
                        calls = [
                            call for call in ledger.state["calls"] if call["cell"] == cell["cell"]
                        ]
                        errors = (
                            assess(BY_ID[cell["task"]], data, cell["entrypoint"])
                            if response.status_code < 400
                            else [f"HTTP {response.status_code}", packed(data)]
                        )
                        known = bool(calls) and all(call["status"] == "settled" for call in calls)
                        row = {
                            **cell,
                            "stage": spec["stage"],
                            "status": response.status_code,
                            "response": data,
                            "functional_valid": not errors,
                            "errors": errors,
                            "duration_s": duration,
                            "accepted": not errors and duration <= 10 and known,
                            "usage_known": known,
                            "calls": calls,
                            "stages": json.loads(response.headers.get("x-benchmark-stages", "[]")),
                        }
                        rows.append(row)
                        with (output / "responses.jsonl").open("a") as file:
                            file.write(packed(row) + "\n")
                        print(
                            packed(
                                {
                                    key: row[key]
                                    for key in ("cell", "accepted", "duration_s", "errors")
                                }
                            ),
                            flush=True,
                        )
                        if not row["accepted"] or ledger.state["blocked"]:
                            reason = ledger.state["blocked"] or (
                                "unknown_usage"
                                if not known
                                else "incorrect_delivery"
                                if errors
                                else "delivery_exceeded_10_seconds"
                            )
                            ledger.stop(reason)
                            break
                finally:
                    current_spend_budget.reset(token)
                if reason:
                    break
    except BaseException as exc:
        reason = "campaign_aborted: " + type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        write_report(output, frozen, rows, reason)
        TRAMA_TRANSPORT.close()
        TRAMA_TRANSPORT = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args().output))
