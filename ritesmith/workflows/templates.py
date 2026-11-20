"""Verified workflow recipes selected by the caller, built without a model call.

A caller that already knows the shape of the request (a threshold watch, a price swing)
sends ``context.template = {"name": ..., "params": {...}}``. The params are validated
against a closed model and the recipe is emitted as final Trama nodes, so generation is
deterministic, sub-second and free. Anything that does not fit a recipe still goes to
the model.

Every polling recipe stops on its own: either after its one action, or at a wall-clock
deadline computed when the plan is built (``runtime.timestamp`` is read each pass).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ritesmith.core.presence_workflow import PresenceReminder, presence_arrival_workflow

_OPS = {"<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "===", "!=": "!=="}
_MAX_WINDOW = 14 * 86400


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HomeCall(_Closed):
    """One ``home.execute`` call: LoomHarbor capability on a device alias/id."""

    capability: str = Field(min_length=3, max_length=80, pattern=r"^[a-z_]+\.[A-Za-z_]+$")
    target: str = Field(min_length=1, max_length=200)
    input: dict = Field(default_factory=dict)


class ThresholdAction(_Closed):
    """Poll a device reading; when it crosses ``value``, act once, notify once, stop."""

    read: HomeCall
    # Path under the home.execute ``result`` object, e.g. "state.current_temperature".
    metric_path: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.]+$")
    op: Literal["<", "<=", ">", ">=", "==", "!="]
    # Numbers for thresholds; a bool/str for state matches (motion detected, door open).
    value: float | bool | str
    action: HomeCall | None = None
    notify: str | None = Field(default=None, min_length=1, max_length=1000)
    timeout_notify: str | None = Field(default=None, min_length=1, max_length=1000)
    interval_seconds: int = Field(default=300, ge=30, le=86400)
    window_seconds: int = Field(default=12 * 3600, ge=60, le=_MAX_WINDOW)
    # Wait before the first read ("between midnight and 6am" starting now at 21:00).
    start_delay_seconds: int = Field(default=0, ge=0, le=86400)

    @model_validator(mode="after")
    def does_something(self):
        if self.action is None and self.notify is None:
            raise ValueError("threshold_action needs an action, a notify text, or both")
        if self.op in ("<", "<=", ">", ">=") and isinstance(self.value, (bool, str)):
            raise ValueError(f"op {self.op} needs a numeric value")
        return self


class PriceVariation(_Closed):
    """Capture a base price now; notify once when it moves more than ``percent``."""

    kind: Literal["crypto", "fiat"]
    symbol: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9-]+$")
    currency: str = Field(default="BRL", min_length=3, max_length=10, pattern=r"^[A-Za-z]+$")
    percent: float = Field(gt=0, le=100)
    direction: Literal["up", "down", "both"] = "both"
    interval_seconds: int = Field(default=3600, ge=60, le=86400)
    window_seconds: int = Field(default=86400, ge=60, le=_MAX_WINDOW)
    label: str | None = Field(default=None, max_length=80)


@dataclass(frozen=True)
class Template:
    spec: type[BaseModel]
    build: Callable[[BaseModel, str], dict]
    capabilities: tuple[str, ...]


def _task(base_url: str, node_id: str, capability: str, input_: dict, next_: str) -> dict:
    return {
        "id": node_id,
        "kind": "task",
        "next": next_,
        "action": {
            "mode": "sync",
            "request": {
                "url": base_url.rstrip("/") + "/trama/execute",
                "verb": "POST",
                "headers": {
                    "Authorization": "Bearer __TRAMA_TOKEN__",
                    "Content-Type": "application/json",
                },
                "body": {"capability_name": capability, "input": input_},
            },
            "successStatusCodes": [200],
        },
    }


def _out(node_id: str, path: str = "") -> str:
    root = f"nodes.{node_id}.response.body.output"
    return f"{root}.{path}" if path else root


def _home(base_url, node_id, call: HomeCall, next_):
    body = {"capability": call.capability, "target": call.target, "input": call.input}
    return _task(base_url, node_id, "home.execute", body, next_)


def _deadline_nodes(base_url, deadline: float, on_timeout: str, loop: str) -> list[dict]:
    """clock → past deadline? → on_timeout, else sleep handled by caller via ``loop``."""
    return [
        _task(base_url, "clock", "runtime.timestamp", {}, "check_deadline"),
        {
            "id": "check_deadline",
            "kind": "switch",
            "cases": [
                {
                    "name": "expired",
                    "when": {">": [{"var": _out("clock", "result")}, deadline]},
                    "target": on_timeout,
                }
            ],
            "default": loop,
        },
    ]


def threshold_action_workflow(spec: ThresholdAction, base_url: str) -> dict:
    deadline = round(time.time() + spec.start_delay_seconds + spec.window_seconds)
    metric = {"var": _out("read", "result." + spec.metric_path)}
    crossed = {
        "and": [
            {"===": [{"var": _out("read", "success")}, True]},
            {"!=": [metric, None]},
            {_OPS[spec.op]: [metric, spec.value]},
        ]
    }
    nodes = [
        _home(base_url, "read", spec.read, "check"),
        {
            "id": "check",
            "kind": "switch",
            "cases": [{"name": "crossed", "when": crossed, "target": "act_entry"}],
            "default": "clock",
        },
        *_deadline_nodes(base_url, deadline, "timeout_entry", "wait"),
        {"id": "wait", "kind": "sleep", "durationSeconds": spec.interval_seconds, "next": "read"},
    ]
    after_action = "notify" if spec.notify else "end"
    if spec.action:
        nodes.append(_home(base_url, "act_entry", spec.action, after_action))
    if spec.notify:
        text = spec.notify.replace(
            "{value}", "{{ " + _out("read", "result." + spec.metric_path) + " }}"
        )
        nodes.append(
            _task(
                base_url,
                "notify" if spec.action else "act_entry",
                "telegram.send",
                {"text": text},
                "end",
            )
        )
    if spec.timeout_notify:
        nodes.append(
            _task(base_url, "timeout_entry", "telegram.send", {"text": spec.timeout_notify}, "end")
        )
    else:
        # Nothing to say on timeout: a zero-cost terminal step keeps the graph explicit.
        nodes.append(_task(base_url, "timeout_entry", "runtime.timestamp", {}, "end"))
    entry = "read"
    if spec.start_delay_seconds:
        nodes.insert(
            0,
            {
                "id": "start_wait",
                "kind": "sleep",
                "durationSeconds": spec.start_delay_seconds,
                "next": "read",
            },
        )
        entry = "start_wait"
    return {
        "name": "threshold_action",
        "version": "1.0.0",
        # Validator metadata for the polling back-edge; the deadline is what stops it.
        "max_iterations": 20,
        "entrypoint": entry,
        "nodes": nodes,
    }


def price_variation_workflow(spec: PriceVariation, base_url: str) -> dict:
    deadline = round(time.time() + spec.window_seconds)
    if spec.kind == "crypto":
        capability = "market.coin_price"
        args = {"symbol": spec.symbol.lower(), "currency": spec.currency.lower()}
        field = "price"
    else:
        capability = "market.exchange_rate"
        args = {"from_currency": spec.symbol.lower(), "to_currency": spec.currency.lower()}
        field = "rate"
    base = {"var": _out("base", field)}
    now = {"var": _out("fetch", field)}
    factor = spec.percent / 100
    up = {">": [now, {"*": [base, 1 + factor]}]}
    down = {"<": [now, {"*": [base, 1 - factor]}]}
    moved = {"up": up, "down": down, "both": {"or": [up, down]}}[spec.direction]
    label = spec.label or spec.symbol.upper()
    text = (
        f"{label}: variou mais de {spec.percent:g}% — agora {{{{ {_out('fetch', field)} }}}} "
        f"{spec.currency.upper()} (base {{{{ {_out('base', field)} }}}})."
    )
    nodes = [
        _task(base_url, "base", capability, args, "wait"),
        {"id": "wait", "kind": "sleep", "durationSeconds": spec.interval_seconds, "next": "fetch"},
        _task(base_url, "fetch", capability, args, "check"),
        {
            "id": "check",
            "kind": "switch",
            "cases": [{"name": "moved", "when": moved, "target": "notify"}],
            "default": "clock",
        },
        *_deadline_nodes(base_url, deadline, "end", "wait"),
        _task(base_url, "notify", "telegram.send", {"text": text}, "end"),
    ]
    return {
        "name": "price_variation",
        "version": "1.0.0",
        "max_iterations": 20,
        "entrypoint": "base",
        "nodes": nodes,
    }


TEMPLATES: dict[str, Template] = {
    "presence_reminder": Template(
        PresenceReminder, presence_arrival_workflow, ("home.execute", "telegram.send")
    ),
    "threshold_action": Template(
        ThresholdAction,
        threshold_action_workflow,
        ("home.execute", "telegram.send", "runtime.timestamp"),
    ),
    "price_variation": Template(
        PriceVariation,
        price_variation_workflow,
        ("telegram.send", "runtime.timestamp"),  # + the market capability for its kind
    ),
}


def requested_template(context: dict | None) -> tuple[str, BaseModel] | None:
    """The validated recipe a request asks for, or None. Invalid params raise."""
    context = context or {}
    if "presence_reminder" in context:  # original contract, kept for r2d2
        return "presence_reminder", PresenceReminder.model_validate(context["presence_reminder"])
    choice = context.get("template")
    if choice is None:
        return None
    if not isinstance(choice, dict) or set(choice) - {"name", "params"}:
        raise ValueError("context.template must be {name, params}")
    name = choice.get("name")
    if name not in TEMPLATES:
        raise ValueError(f"Unknown template {name!r}; available: {sorted(TEMPLATES)}")
    return name, TEMPLATES[name].spec.model_validate(choice.get("params") or {})


def build_template(name: str, spec: BaseModel, base_url: str) -> dict:
    return TEMPLATES[name].build(spec, base_url)
