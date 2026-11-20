"""Tool-using generation: the model sees validator errors and fixes them before submit."""

import json
from types import SimpleNamespace

import pytest

from ritesmith.config import Settings
from ritesmith.core.exceptions import LLMError
from ritesmith.llm.agent_loop import _TOOLS, AgentGeneration, _static_system
from ritesmith.llm.openai_provider import OpenAIProvider

CAPS = [
    {
        "capability_name": "telegram.send",
        "description": "Send a plain-text message",
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    },
    {
        "capability_name": "market.coin_price",
        "description": "Get the current price of any cryptocurrency",
        "input_schema": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}, "currency": {"type": "string"}},
        },
        "output_schema": {"type": "object", "properties": {"price": {"type": "number"}}},
    },
    {
        "capability_name": "home.execute",
        "description": "Execute any home automation capability",
        "input_schema": {
            "type": "object",
            "required": ["capability", "target"],
            "properties": {
                "capability": {"type": "string"},
                "target": {"type": "string"},
                "input": {"type": "object"},
            },
        },
        "output_schema": {},
    },
    {
        "capability_name": "runtime.timestamp",
        "description": "Unix epoch",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "number"},
    },
]

REMINDER = {
    "version": "workflow-plan-v1",
    "name": "reminder",
    "steps": [
        {"kind": "wait", "seconds": 300},
        {
            "kind": "call",
            "id": "notify",
            "capability_name": "telegram.send",
            "args": {"text": "roupa"},
        },
    ],
}


def call(name, args, cid):
    return SimpleNamespace(type="function_call", call_id=cid, name=name, arguments=json.dumps(args))


def reply(*calls, content=""):
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=100,
        output_tokens_details=SimpleNamespace(reasoning_tokens=40),
        input_tokens_details=SimpleNamespace(cached_tokens=800),
    )
    output = list(calls) or [SimpleNamespace(type="message", content=content)]
    reply.n = getattr(reply, "n", 0) + 1
    return SimpleNamespace(id=f"resp_{reply.n}", output=output, usage=usage, model="gpt-test")


class FakeResponses:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.replies.pop(0)


def outputs(request):
    return [
        json.loads(i["output"]) for i in request["input"] if i.get("type") == "function_call_output"
    ]


def agent(replies, **settings):
    responses = FakeResponses(replies)
    client = SimpleNamespace(responses=responses)
    provider = OpenAIProvider(Settings(llm_model="gpt-5.5", **settings), client=client)
    return AgentGeneration(
        provider, capabilities=CAPS, profile="notification", language="luau"
    ), responses


async def run(gen):
    return await gen.run(
        goal="em 5 minutos me lembra",
        constraints={},
        context=None,
        input_schema=None,
        output_schema=None,
    )


@pytest.mark.asyncio
async def test_validator_errors_reach_the_model_and_the_fix_is_submitted():
    broken = {**REMINDER, "steps": [{"kind": "wait", "seconds": 0}, REMINDER["steps"][1]]}
    gen, completions = agent(
        [
            reply(call("validate_workflow", {"plan": broken}, "a")),
            reply(call("validate_workflow", {"plan": REMINDER}, "b")),
            reply(call("submit", {"kind": "workflow", "plan": REMINDER, "name": "lembrete"}, "c")),
        ]
    )
    proposal, stats = await run(gen)
    # The second request carries the first validation result as a function_call_output.
    first = outputs(completions.requests[1])[0]
    assert first["ok"] is False and "greater than 0" in json.dumps(first)
    assert proposal.workflow is not None and proposal.script is None
    assert proposal.analysis.artifact_types == ["trama_workflow"]
    nodes = proposal.workflow.definition["nodes"]
    assert any(n["kind"] == "sleep" and n["durationSeconds"] == 300 for n in nodes)
    assert stats.api_attempts == 3 and stats.prompt_tokens == 3000 and stats.cached_tokens == 2400


@pytest.mark.asyncio
async def test_invalid_submit_is_rejected_back_to_the_model():
    bad = {
        **REMINDER,
        "steps": [
            {"kind": "call", "id": "x", "capability_name": "telegram.send", "args": {"msg": "a"}}
        ],
    }
    gen, completions = agent(
        [
            reply(call("submit", {"kind": "workflow", "plan": bad}, "a")),
            reply(call("submit", {"kind": "workflow", "plan": REMINDER}, "b")),
        ]
    )
    proposal, _ = await run(gen)
    rejection = outputs(completions.requests[1])[0]
    assert "submit_rejected" in rejection
    assert proposal.workflow is not None


@pytest.mark.asyncio
async def test_template_path_needs_no_plan():
    params = {"kind": "crypto", "symbol": "BTC", "currency": "BRL", "percent": 5}
    gen, _ = agent(
        [
            reply(call("use_template", {"name": "price_variation", "params": params}, "a")),
            reply(call("submit", {"kind": "template", "description": "BTC 5%"}, "b")),
        ]
    )
    proposal, _ = await run(gen)
    assert proposal.workflow.name == "price_variation"
    assert "market.coin_price" in proposal.workflow.required_capabilities


@pytest.mark.asyncio
async def test_bad_template_params_come_back_as_errors():
    gen, completions = agent(
        [
            reply(
                call("use_template", {"name": "price_variation", "params": {"kind": "crypto"}}, "a")
            ),
            reply(call("submit", {"kind": "refuse", "reason": "falta o percentual"}, "b")),
        ]
    )
    with pytest.raises(LLMError, match="declined"):
        await run(gen)
    result = outputs(completions.requests[1])[0]
    assert result["ok"] is False and "percent" in result["errors"]


@pytest.mark.asyncio
async def test_simulation_reports_calls_and_virtual_time():
    gen, _ = agent([])
    result = gen.simulate_workflow(REMINDER, {"telegram.send": {"ok": True}})
    assert result == {
        "ok": True,
        "calls": {"telegram.send": 1},
        "virtual_seconds": 300,
        "steps": result["steps"],
    }


@pytest.mark.asyncio
async def test_text_answers_are_nudged_once_then_fail():
    gen, completions = agent([reply(content="here is a plan"), reply(content="still text")])
    with pytest.raises(LLMError, match="without an accepted submit"):
        await run(gen)
    assert len(completions.requests) == 2


@pytest.mark.asyncio
async def test_turn_budget_is_enforced():
    looping = [reply(call("find_capabilities", {"query": "price"}, str(i))) for i in range(3)]
    gen, completions = agent(looping, generation_agent_max_turns=3)
    with pytest.raises(LLMError):
        await run(gen)
    assert len(completions.requests) == 3


def test_contracts_and_search_use_the_live_registry():
    gen, _ = agent([])
    found = gen.find_capabilities("crypto price")
    assert found["matches"][0]["name"] == "market.coin_price"
    contracts = gen.get_contracts(["market.coin_price", "nope"])
    assert contracts["unknown"] == ["nope"]
    assert contracts["contracts"]["market.coin_price"]["in"]["required"] == ["symbol"]


@pytest.mark.asyncio
async def test_every_turn_resends_the_same_instructions_and_tools():
    # Prompt caching needs a stable prefix: instructions + tools never depend on the request.
    gen, responses = agent(
        [
            reply(call("find_capabilities", {"query": "price"}, "a")),
            reply(call("submit", {"kind": "workflow", "plan": REMINDER}, "b")),
        ]
    )
    await run(gen)
    first, second = responses.requests
    assert first["instructions"] == second["instructions"] == _static_system()
    assert first["tools"] == second["tools"]
    assert all(t["strict"] is False for t in first["tools"])
    assert "previous_response_id" not in first and second["previous_response_id"].startswith(
        "resp_"
    )
    assert all("REQUEST" not in json.dumps(t) for t in _TOOLS)


# ── /plans in agent mode, end to end through the API and the database ───────────────


def _agent_provider(replies):
    responses = FakeResponses(replies)
    settings = Settings(llm_model="gpt-5.5", generation_mode="agent")
    return OpenAIProvider(settings, client=SimpleNamespace(responses=responses)), settings


# Capabilities a bare test environment always has (no Telegram token there).
CLOCK = {
    "version": "workflow-plan-v1",
    "name": "clock",
    "steps": [
        {"kind": "wait", "seconds": 300},
        {"kind": "call", "id": "now", "capability_name": "runtime.now", "args": {}},
    ],
}


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_plan_is_created_and_its_transcript_is_readable(make_client):
    llm, settings = _agent_provider(
        [
            reply(call("validate_workflow", {"plan": CLOCK}, "a")),
            reply(call("submit", {"kind": "workflow", "plan": CLOCK, "name": "relogio"}, "b")),
        ]
    )
    async with make_client(llm=llm, settings=settings) as client:
        resp = await client.post(
            "/plans",
            json={"intent": "em 5 minutos me lembra de tirar a roupa", "reuse_policy": "force_new"},
        )
        assert resp.status_code == 201, resp.text
        plan = resp.json()
        assert plan["artifacts"][0]["artifact_type"] == "trama_workflow"
        transcript = await client.get(f"/plans/{plan['plan_id']}/transcript")
    assert transcript.status_code == 200
    attempt = transcript.json()["attempts"][0]
    assert attempt["ok"] is True
    assert [t["tool"] for t in attempt["transcript"]] == ["validate_workflow", "submit"]


@pytest.mark.asyncio(loop_scope="session")
async def test_failed_agent_generation_keeps_its_transcript(make_client):
    llm, settings = _agent_provider([reply(content="texto"), reply(content="mais texto")])
    async with make_client(llm=llm, settings=settings) as client:
        resp = await client.post(
            "/plans", json={"intent": "faça algo impossível", "reuse_policy": "force_new"}
        )
        assert resp.status_code == 422, resp.text
        plan_id = resp.json()["details"]["plan_id"]
        transcript = await client.get(f"/plans/{plan_id}/transcript")
        missing = await client.get("/plans/plan_nope/transcript")
    assert transcript.status_code == 200
    assert transcript.json()["attempts"][0]["ok"] is False
    assert missing.status_code == 404


def test_check_script_applies_the_pipeline_size_limit_first():
    # agent-v1: four scripts passed check_script, then failed the pipeline's 90-line limit.
    gen, _ = agent([])
    long_script = "function run(input, context)\n" + "  local x = 1\n" * 200 + "  return {}\nend"
    result = gen.check_script(long_script, [{"input": {}}])
    assert result["ok"] is False
    assert any("linhas" in e for e in result["static_errors"])


@pytest.mark.asyncio
async def test_isolated_tool_over_budget_becomes_feedback_and_pool_recovers():
    # 2026-11-20: a generation froze the host; heavy tools now run in a capped child.
    gen, _ = agent([], generation_agent_tool_timeout_seconds=0.001)
    result = await gen._dispatch("validate_workflow", {"plan": REMINDER})
    assert result["ok"] is False and "budget" in result["error"]
    gen.settings = Settings(llm_model="gpt-5.5", generation_agent_tool_timeout_seconds=60)
    result = await gen._dispatch("validate_workflow", {"plan": REMINDER})
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_isolated_compile_returns_the_definition_for_submit():
    gen, _ = agent([])
    result = await gen._dispatch("compile_workflow", {"plan": REMINDER})
    assert result["ok"] and any(n["kind"] == "sleep" for n in result["definition"]["nodes"])


def test_worker_memory_limit_is_a_data_segment_cap():
    import resource

    from ritesmith.llm.agent_loop import _limit_memory

    soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    try:
        _limit_memory(4096)
        assert resource.getrlimit(resource.RLIMIT_DATA)[0] == 4096 * 1024 * 1024
    finally:
        resource.setrlimit(resource.RLIMIT_DATA, (soft, hard))


@pytest.mark.asyncio
async def test_try_capability_runs_read_only_and_refuses_effects():
    gen, _ = agent([])
    ok = await gen._dispatch("try_capability", {"name": "runtime.timestamp", "args": {}})
    assert ok["ok"] is True and isinstance(ok["output"], float)
    for name in ("home.execute", "telegram.send"):
        refused = await gen._dispatch("try_capability", {"name": name, "args": {}})
        assert refused["ok"] is False and "read-only" in refused["error"]


@pytest.mark.asyncio
async def test_probe_budget_is_bounded():
    from ritesmith.llm.agent_loop import _MAX_PROBES

    gen, _ = agent([])
    for _ in range(_MAX_PROBES):
        assert (await gen._dispatch("try_capability", {"name": "runtime.timestamp"}))["ok"]
    over = await gen._dispatch("try_capability", {"name": "runtime.timestamp"})
    assert over["ok"] is False and "budget" in over["error"]


def test_prompt_covers_deliverables_and_virtual_devices():
    system = _static_system()
    assert "report.write_html" in system and "try_capability" in system
    assert "virtual" in system
