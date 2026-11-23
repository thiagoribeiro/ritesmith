"""Tool-using generation loop: the model checks its own work before submitting.

The single-shot proposal asks the model for a complete artifact in one JSON answer and
validates it afterwards; any mistake costs a cold repair call. Here the model works the
way a coding agent does: it looks up the contracts and devices it needs, builds a plan,
runs the same validators and the offline simulator the pipeline uses, reads the errors
as tool results and fixes them in the same conversation, then submits. ``submit`` only
accepts what already passes validation.

Prompt layout is cache-friendly: the system prompt and tool definitions are identical
for every request, and everything request-specific comes last.

Returns the same ``(GenerationProposal, LLMCallStats)`` as ``OpenAIProvider.propose``
so planning, validation and persistence are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import cache
from typing import Any

from ritesmith.core.exceptions import LLMError
from ritesmith.core.generation_budget import current_budget
from ritesmith.llm import hints
from ritesmith.llm.base import (
    GenerationProposal,
    IntentAnalysis,
    LLMCallStats,
    LuaGenerationResponse,
    WorkflowGenerationResponse,
)
from ritesmith.observability.generation import current_trace
from ritesmith.workflows.constructors import semantic_example
from ritesmith.workflows.semantic import SEMANTIC_RULES

log = logging.getLogger(__name__)

AGENT_VERSION = "agent-loop-v3"
_RESULT_LIMIT = 6000  # characters of a tool result shown back to the model

AGENT_SYSTEM = """\
You build automations for RiteSmith. Output is EITHER a Trama workflow (a semantic plan, for
anything with waits, schedules, polling, several steps over time or parallel calls) OR one
Luau script (pure computation, or a single immediate read/act that returns a result).
Prefer the simplest artifact that does the job.

Work like an engineer with tools, not like an autocomplete:
1. find_capabilities / get_contracts for every capability you will call — never guess input
   keys or output fields; contracts are closed.
2. list_devices when the request names a device, room or person: use the exact alias/id and
   a capability that device actually has (e.g. an IR fan is remote.press, not switch.off).
3. First list every requirement in the request: trigger, times/dates, repetition, each
   action (device + what), each message, when it stops. Recipes (use_template) are verified
   shortcuts — use one ONLY if every requirement maps onto its params. Never drop, merge or
   rephrase a requirement to make a recipe fit (two actions are two actions; a date is not
   an arrival). If anything is left over, write a plan instead: a plan can do everything a
   recipe does, e.g. "watch X; when Y do A and B, tell me, and stop" is a repeat with a
   condition ending in {kind:"stop"}. Shortcuts: threshold_action (device reading/state
   crosses a value → actions[] and/or one message, once), price_variation (price moves N%),
   presence_change (people arrive / everyone leaves → actions/message once), presence_reminder
   (a text on the next arrival), scheduled_reminder (a text at a date, optionally repeating).
4. Otherwise draft, then validate_workflow (or check_script). Fix every error it reports.
   For anything that loops, simulate_workflow with realistic responses and confirm it
   notifies the right number of times and terminates.
5. submit. submit re-validates; a rejected submit returns the errors to fix.

Deliver what was asked through its capability, never as returned text: a report, HTML page
or file → report.write_html (return its path); a message → telegram.send; a device change →
home.execute. Unknown metric names, API fields or units are discovered with try_capability
(e.g. list Prometheus metric names) — not a reason to refuse.
Ambiguous targets: if the request names a kind of device ("o ar", "a luz") and more than one
device of that kind matches without a room or name in the request, do NOT pick one — a
wrong physical action is worse than a question. submit(kind="refuse") with a reason that
lists the options (e.g. "__HINT_ambiguity_example__"). Act on all of
them only when the request says so ("todos", plural).
Devices: person/tracker/adapter entries are virtual; their "online" flag does not matter and
presence.read on a person works. Offline physical devices are skipped (say so in the
description); refuse only if every device the request depends on is unavailable.
Vague but common phrasing gets a sensible, stated default instead of a refusal: "à noite /
durante a noite" = now until 07:00 local; "hoje" = until 23:59 local; "de vez em quando" =
every 30 min; no duration on a monitor = 24 h. Put the assumption in submit.description.
Refuse only when the request is unsafe or impossible with the listed capabilities.
Behaviour rules: one notification per real event (never one per polling pass); every loop
has an explicit end; precompute numeric thresholds; never invent devices, chats or URLs; a
request that cannot be done safely with the available capabilities is reported via
submit(kind="refuse", reason=...).
context.user_request, when present, is the user's own words; the request text is a
paraphrase by another assistant and may be wrong — where they disagree, follow the user.
context.verifier_feedback, when present, means a previous build was checked against those
words and did not match: fix exactly what it says (requested vs built) and keep the rest.
Keep messages to the user in the request's language. Code comments in English.
"""

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "find_capabilities",
            "description": "Search the capability catalog. Returns names and one-line descriptions.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contracts",
            "description": "Closed input/output JSON schemas of the named capabilities.",
            "parameters": {
                "type": "object",
                "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                "required": ["names"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": (
                "Home devices known to home.execute: alias/id, type, room and capabilities. "
                "Optional filter matches name, alias, type or room."
            ),
            "parameters": {
                "type": "object",
                "properties": {"filter": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "try_capability",
            "description": (
                "Run a READ-ONLY capability now to learn real data before building: e.g. "
                "grafana.query_prometheus to find a metric name "
                "({query: 'count by (__name__)({__name__=~\".*loss.*\"})'}), web.fetch_json to "
                "see an API's shape, market.* for a current price. Only grafana.*, web.*, "
                "market.*, runtime.* and stat.* are allowed; results are truncated."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_template",
            "description": (
                "Build a verified recipe — only when EVERY requirement of the request maps onto "
                "its params; otherwise write a plan. threshold_action = 'when a device reading matches, act "
                "and/or notify ONCE, then end' (ends at the deadline otherwise). params: "
                "read{capability,target,input?}, metric_path (under home.execute result, e.g. "
                "state.current_temperature or present), op (<,<=,>,>= numeric; ==,!= for "
                "bool/text like motion or door state), value, actions[{capability,target,input}] (<=6, in order), "
                "notify? ('{value}' = reading), timeout_notify?, interval_seconds (30-86400), "
                "window_seconds (<=14 days), start_delay_seconds (wait before the first read, "
                "e.g. until midnight). "
                "price_variation params: kind (crypto|fiat), symbol, currency, percent, "
                "direction (up|down|both), interval_seconds, window_seconds, label?. "
                "presence_change = 'when people arrive/leave, act and/or notify ONCE, then end': "
                "params people[] (person targets from list_devices, e.g. __HINT_person_examples__), event "
                "(all_away = everyone listed is out; any_home = someone listed is home), "
                "require_transition (default true: first see the opposite state, so 'when X "
                "arrives' waits for a real arrival), delay_seconds (e.g. 900 = 15 min after), "
                "actions[{capability,target,input}] (<=6), notify?, interval_seconds, "
                "window_seconds. "
                "presence_reminder params: person_alias, message, channel='telegram' (ONLY "
                "'on my next arrival'; never for a date). "
                "scheduled_reminder = 'tell me at <date/time>, optionally every N "
                "days/weeks/months/years': params message, first_at (local ISO, e.g. "
                "2027-05-15T08:00), timezone (default America/Sao_Paulo), "
                "repeat{every,unit}?, count? or until? (YYYY-MM-DD). Calendar months; a "
                "durable Trama sleep per gap. "
                "On success the workflow is final: call submit(kind='template')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [
                            "threshold_action",
                            "price_variation",
                            "presence_change",
                            "presence_reminder",
                            "scheduled_reminder",
                        ],
                    },
                    "params": {"type": "object"},
                },
                "required": ["name", "params"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_workflow",
            "description": "Compile a semantic plan {version,name,steps} and run every pipeline validator.",
            "parameters": {
                "type": "object",
                "properties": {"plan": {"type": "object"}},
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_workflow",
            "description": (
                "Run the compiled plan offline with a virtual clock. responses maps an "
                "operation id or capability name to one output or a list consumed in order "
                "(the last item repeats once the list runs out; home.execute returns "
                "{success, result:{...}}; scalar outputs are {result: x}). runtime.timestamp/"
                "runtime.now follow the virtual clock automatically. Returns calls per "
                "capability, elapsed virtual seconds, and errors. Simulate once or twice at "
                "most; it is a check, not a requirement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "object"},
                    "responses": {"type": "object"},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_script",
            "description": (
                "Type-check a Luau script (one global run(input, context)) and run it against your "
                "cases: [{input, expected_output?, assertions?, tool_fixtures:[{tool,args,output}]}]. "
                "Fixtures must use the exact args your script passes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string"},
                    "cases": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["script", "cases"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": (
                "Finish. kind=workflow: plan, name, description. kind=script: script, name, "
                "description, cases (the ones check_script passed). kind=template: name, "
                "description (after use_template). kind=refuse: reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["workflow", "script", "template", "refuse"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "plan": {"type": "object"},
                    "script": {"type": "string"},
                    "cases": {"type": "array", "items": {"type": "object"}},
                    "reason": {"type": "string"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
    },
]


# Responses API shape; strict=False because plan/params/responses are open objects.
@cache
def _response_tools() -> list[dict]:
    """Tool definitions with deployment hints filled in (resolved once: prompt-cache stable)."""
    return [
        {
            "type": "function",
            "name": t["function"]["name"],
            "description": hints.fill(t["function"]["description"]),
            "parameters": json.loads(hints.fill(json.dumps(t["function"]["parameters"]))),
            "strict": False,
        }
        for t in _TOOLS
    ]


def _static_system() -> str:
    from ritesmith.llm import prompts

    examples = {p: semantic_example(p) for p in ("linear", "bounded_polling", "parallel")}
    return "\n".join(
        [
            hints.fill(AGENT_SYSTEM),
            "SEMANTIC PLAN RULES:" + SEMANTIC_RULES,
            "PLAN EXAMPLES: " + json.dumps(examples, separators=(",", ":")),
            "LUAU RULES:\n"
            + prompts.luau_generation_system(False).replace(
                "Respond with valid JSON matching the schema in the user message — nothing else.",
                "Scripts are passed to check_script/submit as a string.",
            ),
        ]
    )


class AgentGeneration:
    def __init__(
        self,
        provider,
        *,
        capabilities: list[dict],
        profile: str,
        language: str,
        input_schema: dict | None = None,
        output_schema: dict | None = None,
    ):
        self.provider = provider
        self.settings = provider.settings
        self.base_url = self.settings.public_url or "http://ritesmith:8081"
        self.registry = {c["capability_name"]: c for c in capabilities if c.get("capability_name")}
        self.profile = profile
        self.language = language
        self.template_definition: dict | None = None
        self.template_name: str | None = None
        self.transcript: list[dict] = []
        self.probes = 0
        # The script contract: check_script validates against it exactly as the
        # pipeline does after submit (empty-table normalization, Output type, enums).
        self.input_schema = input_schema
        self.output_schema = output_schema

    # ── tools ────────────────────────────────────────────────────────────────────
    def find_capabilities(self, query: str) -> dict:
        words = {w for w in query.lower().replace(".", " ").split() if len(w) > 2}
        scored = []
        for name, cap in self.registry.items():
            text = (name + " " + (cap.get("description") or "")).lower()
            score = sum(w in text for w in words) + (3 if query.lower() in name else 0)
            if score:
                scored.append((score, name, (cap.get("description") or "")[:140]))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return {
            "matches": [{"name": n, "description": d} for _, n, d in scored[:12]],
            "all_names": sorted(self.registry),
        }

    def get_contracts(self, names: list[str]) -> dict:
        out, unknown = {}, []
        for name in names[:15]:
            cap = self.registry.get(name)
            if cap is None:
                unknown.append(name)
                continue
            out[name] = {"in": cap.get("input_schema") or {}, "out": cap.get("output_schema") or {}}
        result: dict = {"contracts": out, "unknown": unknown}
        if any(
            not name.startswith(tuple(f"{ns}." for ns in _namespaces(self.registry)))
            for name in unknown
        ):
            result["hint"] = (
                "Device capabilities such as ac.read or remote.press are not catalog entries: "
                "call them through home.execute (see list_devices)."
            )
        return result

    def list_devices(self, filter: str = "") -> dict:
        from ritesmith.runtime.host_functions import get_capability
        from ritesmith.runtime.providers import provider_for
        from ritesmith.runtime.providers.base import invoke_capability, normalize_device

        home = provider_for("home")
        normalize = home.normalize_device if home is not None else normalize_device
        virtual = home.virtual_device_types if home is not None else frozenset()
        cap = get_capability("home.devices")
        if cap is None:
            return {"error": "home.devices unavailable"}
        raw = invoke_capability(cap.callable, {})
        devices = raw.get("devices", raw) if isinstance(raw, dict) else raw
        execute = get_capability("home.execute")
        wanted = filter.lower().strip()
        rows = []
        for d in devices if isinstance(devices, list) else []:
            row = normalize(d)
            state = row.pop("state", None)
            if row.get("type") in virtual:
                # virtual devices (people, trackers) report offline; their reads still work.
                row["online"] = "virtual"
            blob = json.dumps(row, ensure_ascii=False).lower()
            if wanted and not all(w in blob for w in wanted.split()):
                continue
            # The current state shows the exact fields a read returns under result.state.
            if isinstance(state, dict) and state:
                row["state_example"] = dict(list(state.items())[:12])
            if execute is not None and "remote.buttons" in (row.get("capabilities") or []):
                try:  # read-only: the button names remote.press accepts
                    out = invoke_capability(
                        execute.callable,
                        {"capability": "remote.buttons", "target": row["target"], "input": {}},
                    )
                    buttons = ((out or {}).get("result") or {}).get("buttons")
                    if isinstance(buttons, dict):
                        row["buttons"] = sorted(buttons)
                except Exception as exc:  # a lookup failure must not end the tool call
                    row["buttons_error"] = str(exc)[:120]
            rows.append(row)
        return {
            "devices": rows[:40],
            "how_to_call": (
                "home.execute {capability: <one of capabilities>, target: <target>, input: {...}} "
                "returns {success, result}; reads put the device state at result.state "
                "(fields as in state_example). IR remotes: remote.press input {button: <buttons>}."
            ),
        }

    def try_capability(self, name: str, args: dict | None = None) -> dict:
        from ritesmith.runtime.host_functions import get_capability
        from ritesmith.runtime.providers.base import invoke_capability

        if name.split(".", 1)[0] not in _PROBE_NAMESPACES or name not in self.registry:
            return {
                "ok": False,
                "error": f"{name} is not a read-only capability; allowed namespaces: "
                + ", ".join(sorted(_PROBE_NAMESPACES)),
            }
        self.probes += 1
        if self.probes > _MAX_PROBES:
            return {
                "ok": False,
                "error": f"probe budget ({_MAX_PROBES}) used; build with what you know",
            }
        cap = get_capability(name)
        if cap is None:
            return {"ok": False, "error": f"{name} unavailable"}
        return {"ok": True, "output": invoke_capability(cap.callable, args or {})}

    def use_template(self, name: str, params: dict) -> dict:
        from ritesmith.workflows.templates import build_template, requested_template

        context = (
            {"presence_reminder": params}
            if name == "presence_reminder"
            else {"template": {"name": name, "params": params}}
        )
        try:
            found = requested_template(context)
        except ValueError as exc:  # includes pydantic ValidationError
            return {"ok": False, "errors": str(exc)[:1500]}
        definition = build_template(found[0], found[1], self.base_url)
        errors = self._graph_errors(definition)
        if errors:
            return {"ok": False, "errors": errors}
        self.template_definition, self.template_name = definition, name
        return {"ok": True, "nodes": len(definition["nodes"]), "next": "submit(kind='template')"}

    def _compile(self, plan: dict) -> dict:
        if "plan" in plan and "steps" not in plan:  # tolerate the {format, plan} envelope
            plan = plan["plan"]
        return self.provider._compile_definition(
            {"format": "semantic_plan", "plan": plan}, self.base_url
        )

    def _graph_errors(self, definition: dict) -> list[str]:
        from ritesmith.workflows.semantic_validation import validate_semantics
        from ritesmith.workflows.transport import typed_json_workflow
        from ritesmith.workflows.validator import WorkflowValidator, native_shape_errors

        errors = native_shape_errors(definition)
        if errors:
            return errors
        try:
            typed = typed_json_workflow(definition)
        except ValueError as exc:
            return [str(exc)]
        errors = WorkflowValidator(self.registry).validate(typed)
        if not errors:
            errors = validate_semantics(
                typed, self.registry, check_graph=self.settings.generation_semantic_workflows
            )
        return errors

    def validate_workflow(self, plan: dict) -> dict:
        try:
            compiled = self._compile(plan)
        except Exception as exc:  # pydantic / compiler errors are the useful feedback
            return {"ok": False, "errors": [str(exc)[:2000]]}
        errors = self._graph_errors(compiled)
        return {"ok": not errors, "errors": errors[:20], "nodes": len(compiled.get("nodes", []))}

    def compile_workflow(self, plan: dict) -> dict:
        """validate_workflow plus the compiled definition, for submit."""
        check = self.validate_workflow(plan)
        if check["ok"]:
            check["definition"] = self._compile(plan)
        return check

    def simulate_workflow(self, plan: dict, responses: dict | None = None) -> dict:
        try:
            compiled = self._compile(plan)
            result = _AgentSimulator(compiled, responses or {}, max_steps=2000).run()
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:1500]}
        return {
            "ok": True,
            "calls": dict(result.calls),
            "virtual_seconds": result.clock,
            "steps": result.steps,
        }

    def check_script(self, script: str, cases: list[dict]) -> dict:
        from ritesmith.runtime.validation_session import LuauValidationSession, preflight

        static = self._static_script_errors(script)
        if static:
            # Same gate the pipeline applies after submit; failing it there costs the run.
            return {"ok": False, "static_errors": static}
        session = LuauValidationSession(
            self.settings, self.profile, self.input_schema, self.output_schema
        )
        try:
            diagnostics = session.check(script)
            errors = [d.get("message", str(d)) for mode in diagnostics.values() for d in mode]
            if errors:
                return {"ok": False, "type_errors": errors[:15]}
            try:
                specs = preflight(cases, session.tools, self.input_schema, self.output_schema)
            except Exception as exc:
                return {"ok": False, "case_errors": [f"invalid cases: {str(exc)[:600]}"]}
            failures = []
            for index, spec in enumerate(specs):
                result, fixture_error, errs = session.execute(script, spec)
                if fixture_error or errs or not getattr(result, "ok", True):
                    failures.append(
                        {
                            "case": index,
                            "fixture_error": fixture_error,
                            "errors": errs[:5],
                            # What the comparison saw: {} already normalized to [].
                            "output": session.last_output,
                        }
                    )
            return {"ok": not failures, "cases": len(specs), "failures": failures[:5]}
        finally:
            session.close()

    def _static_script_errors(self, script: str) -> list[str]:
        from ritesmith.core.validation import (
            _LUAU_EXTRA_FORBIDDEN,
            _LUAU_SIZE_FACTOR,
            ValidationPipeline,
        )

        pipeline = ValidationPipeline(self.settings)
        checks = [
            *pipeline._check_luau_tools_in_profile(script, self.profile),
            *pipeline._check_forbidden_tokens(script, extra=_LUAU_EXTRA_FORBIDDEN),
            *pipeline._check_size(script, None, factor=_LUAU_SIZE_FACTOR),
        ]
        return [c.message or c.name for c in checks if c.status == "failed"]

    # ── loop ─────────────────────────────────────────────────────────────────────
    async def _dispatch(self, name: str, args: dict) -> Any:
        if name in _ISOLATED:
            return await self._isolated(name, args)
        if name == "try_capability":
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self.try_capability, **args),
                    timeout=self.settings.generation_agent_tool_timeout_seconds,
                )
            except TimeoutError:
                return {"ok": False, "error": "probe timed out"}
            except TypeError as exc:
                return {"error": f"bad arguments for try_capability: {exc}"}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:600]}"}
        handler = {
            "find_capabilities": self.find_capabilities,
            "get_contracts": self.get_contracts,
            "list_devices": self.list_devices,
            "use_template": self.use_template,
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return handler(**args)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # a tool failure is feedback, not a crash
            log.warning("agent tool %s failed: %s", name, exc)
            return {"error": f"{type(exc).__name__}: {str(exc)[:800]}"}

    async def _isolated(self, name: str, args: dict) -> Any:
        """Run a model-shaped workload (compile/validate/simulate/Luau) out of process.

        2026-11-20: one agent generation drove the service to exhaust the host's RAM
        and froze the Pi; these tools run code whose cost the model controls (plan
        size, loop counts, a generated script), synchronously, inside the event loop.
        Here they get their own process with a data-segment limit and a timeout, and
        a runaway becomes a tool error the model can act on.
        """
        if not self.settings.generation_agent_isolate_tools:
            return _run_tool(self, name, args)
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    _tool_pool(self.settings.generation_agent_tool_memory_mb),
                    _isolated_tool,
                    self.settings,
                    self.registry,
                    self.profile,
                    self.language,
                    self.input_schema,
                    self.output_schema,
                    name,
                    args,
                ),
                timeout=self.settings.generation_agent_tool_timeout_seconds,
            )
        except (TimeoutError, BrokenProcessPool, MemoryError) as exc:
            _reset_tool_pool()
            log.warning("agent tool %s aborted: %s", name, type(exc).__name__)
            return {
                "ok": False,
                "error": (
                    f"{name} exceeded its time/memory budget "
                    f"({self.settings.generation_agent_tool_timeout_seconds:g}s, "
                    f"{self.settings.generation_agent_tool_memory_mb} MB). Simplify: fewer "
                    "iterations, smaller simulated responses, no unbounded loops."
                ),
            }

    async def _submit(self, args: dict, goal: str):
        kind = args.get("kind")
        name = (args.get("name") or "automation").strip()[:80] or "automation"
        description = (args.get("description") or goal)[:500]
        if kind == "refuse":
            raise LLMError(
                "Generation declined: " + (args.get("reason") or "not possible"),
                details={"retryable": False, "category": "refused"},
            )
        if kind == "template":
            if self.template_definition is None:
                return None, {"ok": False, "errors": ["call use_template successfully first"]}
            wf = WorkflowGenerationResponse(
                definition=self.template_definition,
                name=self.template_definition["name"],
                description=description,
                required_capabilities=_capabilities_of(self.template_definition),
            )
            return _proposal(workflow=wf, summary=description), None
        if kind == "workflow":
            plan = args.get("plan") or {}
            check = await self._dispatch("compile_workflow", {"plan": plan})
            if not check.get("ok"):
                return None, check
            compiled = check["definition"]
            wf = WorkflowGenerationResponse(
                definition=compiled,
                name=name,
                description=description,
                required_capabilities=_capabilities_of(compiled),
            )
            return _proposal(workflow=wf, summary=description), None
        if kind == "script":
            script, cases = args.get("script") or "", args.get("cases") or []
            if not cases:
                return None, {"ok": False, "errors": ["submit the cases check_script passed"]}
            check = await self._dispatch("check_script", {"script": script, "cases": cases})
            if not check.get("ok"):
                return None, check
            sc = LuaGenerationResponse(
                script=script,
                name=name,
                description=description,
                runtime_profile=self.profile,
            )
            proposal = _proposal(script=sc, summary=description, language=self.language)
            proposal._test_cases = cases
            proposal._tests_attempted = True
            return proposal, None
        return None, {"ok": False, "errors": [f"unknown kind {kind!r}"]}

    async def run(self, *, goal, constraints, context, input_schema, output_schema):
        self.input_schema = input_schema
        self.output_schema = output_schema
        from ritesmith.runtime.luau import describe_tools_for_prompt

        model, effort = self.provider.operation_config(self.provider.model, "proposal_workflow")
        request = {
            "now_local": _now_local(self.settings.local_timezone),
            "goal": goal,
            "constraints": constraints,
            "context": context or {},
            "script_contract": {
                "input_schema": input_schema,
                "output_schema": output_schema,
                "runtime_profile": self.profile,
                "tools": describe_tools_for_prompt(self.profile),
            },
        }
        instructions = _static_system()
        pending: list[dict] = [
            {"role": "user", "content": "REQUEST " + json.dumps(request, ensure_ascii=False)}
        ]
        previous_id = None
        total = LLMCallStats(model=model, method="agent", prompt_version=AGENT_VERSION)
        started = time.perf_counter()
        nudged = False
        last_rejection = None
        for turn in range(1, self.settings.generation_agent_max_turns + 1):
            budget = current_budget.get()
            if budget is not None and budget.remaining < 5:
                break
            # Responses API: reasoning + function tools together (chat.completions rejects
            # that for gpt-5.5), and previous_response_id keeps the history and reasoning
            # server-side. Instructions are not inherited, so they are re-sent; being
            # identical for every request, they are what the prompt cache serves.
            kwargs: dict = {
                "model": model,
                "instructions": instructions,
                "input": pending,
                "tools": _response_tools(),
                "parallel_tool_calls": True,
                "max_output_tokens": self.settings.generation_agent_turn_tokens,
                "store": True,
            }
            if previous_id:
                kwargs["previous_response_id"] = previous_id
            if effort is not None:
                kwargs["reasoning"] = {"effort": effort}
            if budget is not None:
                kwargs["timeout"] = max(
                    1.0, min(self.settings.llm_timeout_seconds, budget.remaining)
                )
            t0 = time.perf_counter()
            response = await self.provider.client.responses.create(**kwargs)
            self._account(total, response, model, effort, time.perf_counter() - t0, turn)
            previous_id = response.id
            calls = [item for item in response.output if item.type == "function_call"]
            pending = []
            if not calls:
                if nudged:
                    break
                nudged = True
                pending.append(
                    {
                        "role": "user",
                        "content": "Use the tools, then call submit. Do not answer in text.",
                    }
                )
                continue
            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except ValueError as exc:
                    result: Any = {"error": f"arguments are not valid JSON: {exc}"}
                    args = None
                if args is not None and call.name == "submit":
                    proposal, rejection = await self._submit(args, goal)
                    self._log(turn, "submit", args, rejection or {"ok": True})
                    if proposal is not None:
                        total.duration_s = time.perf_counter() - started
                        total.api_attempts = turn
                        proposal.analysis.summary = proposal.analysis.summary or goal
                        return proposal, total
                    last_rejection = rejection
                    result = {"submit_rejected": rejection}
                elif args is not None:
                    result = await self._dispatch(call.name, args)
                    self._log(turn, call.name, args, result)
                pending.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, ensure_ascii=False, default=str)[
                            :_RESULT_LIMIT
                        ],
                    }
                )
        total.duration_s = time.perf_counter() - started
        raise LLMError(
            "Agent generation ended without an accepted submit",
            details={
                "retryable": False,
                "category": "agent",
                "candidate": json.dumps(last_rejection, default=str)[:4000]
                if last_rejection
                else None,
                "stats": total.model_dump(),
                "api_attempts": total.api_attempts,
            },
        )

    def _account(self, total, response, model, effort, duration, turn):
        usage = response.usage
        prompt = getattr(usage, "input_tokens", 0) or 0
        completion = getattr(usage, "output_tokens", 0) or 0
        reasoning = (
            getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        )
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        total.prompt_tokens += prompt
        total.completion_tokens += completion
        total.total_tokens += prompt + completion
        total.reasoning_tokens += reasoning
        total.cached_tokens += cached
        total.resolved_model = response.model
        total.usage_known = usage is not None and (turn == 1 or total.usage_known)
        trace = current_trace.get()
        if trace is not None:
            trace.calls.append(
                {
                    "model": model,
                    "resolved_model": response.model,
                    "reasoning_effort": effort,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                    "reasoning_tokens": reasoning,
                    "cached_tokens": cached,
                    "duration_s": duration,
                    "method": f"agent_turn_{turn}",
                    "prompt_version": AGENT_VERSION,
                    "usage_known": usage is not None,
                    "api_attempts": 1,
                    "fallback": False,
                }
            )
        log.info(
            "agent turn=%d model=%s duration=%.3fs prompt=%s completion=%s reasoning=%s cached=%s",
            turn,
            model,
            duration,
            prompt,
            completion,
            reasoning,
            cached,
        )

    def _log(self, turn, tool, args, result):
        self.transcript.append(
            {
                "turn": turn,
                "tool": tool,
                "args": json.dumps(args, ensure_ascii=False, default=str)[:3000],
                "result": json.dumps(result, ensure_ascii=False, default=str)[:3000],
            }
        )


# Read-only by provider declaration (side_effects none/external_read); llm.* is
# excluded because it costs model calls.
_PROBE_NAMESPACES = frozenset({"grafana", "web", "market", "runtime", "stat"})
_MAX_PROBES = 6
_ISOLATED = ("validate_workflow", "simulate_workflow", "check_script", "compile_workflow")
_POOL: ProcessPoolExecutor | None = None
_POOL_LIMIT_MB: int | None = None


def _limit_memory(megabytes: int) -> None:
    import resource

    # Soft limit only: nothing in the worker (validators, sandboxed Luau) can raise it.
    _, hard = resource.getrlimit(resource.RLIMIT_DATA)
    soft = megabytes * 1024 * 1024
    if hard != resource.RLIM_INFINITY:
        soft = min(soft, hard)  # a soft limit above the hard one raises (macOS)
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (soft, hard))
    except (ValueError, OSError) as exc:
        # Some platforms refuse RLIMIT_DATA outright; run uncapped rather than fail every tool.
        log.warning("agent tool worker: RLIMIT_DATA not applied (%s)", exc)


def _tool_pool(megabytes: int) -> ProcessPoolExecutor:
    global _POOL, _POOL_LIMIT_MB
    if _POOL is None or _POOL_LIMIT_MB != megabytes:
        _reset_tool_pool()
        _POOL = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_limit_memory,
            initargs=(megabytes,),
        )
        _POOL_LIMIT_MB = megabytes
    return _POOL


def _reset_tool_pool() -> None:
    global _POOL
    pool, _POOL = _POOL, None
    if pool is None:
        return
    for process in list(getattr(pool, "_processes", {}).values()):
        process.kill()  # a timed-out worker is still running; don't wait for it
    pool.shutdown(wait=False, cancel_futures=True)


def _run_tool(gen: AgentGeneration, name: str, args: dict) -> Any:
    try:
        return getattr(gen, name)(**args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except MemoryError:
        raise
    except Exception as exc:  # a tool failure is feedback, not a crash
        return {"error": f"{type(exc).__name__}: {str(exc)[:800]}"}


def _isolated_tool(settings, registry, profile, language, input_schema, output_schema, name, args):
    """Worker-process entry: rebuild a provider-less generator and run one tool."""
    from ritesmith.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(settings, client=object())
    gen = AgentGeneration(
        provider,
        capabilities=list(registry.values()),
        profile=profile,
        language=language,
        input_schema=input_schema,
        output_schema=output_schema,
    )
    return _run_tool(gen, name, args)


class _AgentSimulator:
    """WorkflowSimulator with a clock and forgiving response lists for the model's checks."""

    def __new__(cls, definition, fixtures, **kwargs):
        from ritesmith.workflows.simulation import WorkflowSimulator

        class Simulator(WorkflowSimulator):
            started = time.time()

            def fixture(self, node_id, capability, args):
                name = node_id if node_id in self.fixtures else capability
                if name not in self.fixtures and capability == "runtime.timestamp":
                    return {"result": self.started + self.result.clock}
                if name not in self.fixtures and capability == "runtime.now":
                    from datetime import UTC, datetime

                    stamp = datetime.fromtimestamp(self.started + self.result.clock, UTC)
                    return {"result": stamp.isoformat()}
                values = self.fixtures.get(name)
                if (
                    isinstance(values, list)
                    and values
                    and self.fixture_offsets[name] >= len(values)
                ):
                    return values[-1]
                return super().fixture(node_id, capability, args)

        return Simulator(definition, fixtures, **kwargs)


def _namespaces(registry: dict) -> set[str]:
    return {name.split(".", 1)[0] for name in registry}


def _now_local(zone: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d %H:%M (%A) ") + f"{zone}, unix {int(now.timestamp())}"


def _capabilities_of(definition: dict) -> list[str]:
    names = set()
    for node in definition.get("nodes", []):
        body = (node.get("action") or {}).get("request", {}).get("body", {})
        if isinstance(body, dict) and body.get("capability_name"):
            names.add(body["capability_name"])
        if node.get("capability_name"):
            names.add(node["capability_name"])
    return sorted(names)


def _proposal(*, workflow=None, script=None, summary="", language="luau") -> GenerationProposal:
    types = []
    if script is not None:
        types.append("lua_script" if language == "lua" else "luau_script")
    if workflow is not None:
        types.append("trama_workflow")
    analysis = IntentAnalysis(
        requires_lua=script is not None,
        requires_workflow=workflow is not None,
        summary=summary[:300],
        artifact_types=types,
        suggested_name=(workflow or script).name,
    )
    return GenerationProposal(analysis=analysis, script=script, workflow=workflow)
