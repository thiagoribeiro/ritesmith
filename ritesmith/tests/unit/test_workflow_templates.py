"""Caller-selected recipes: closed params, valid graphs, and the behaviour they promise."""

import pytest
from pydantic import ValidationError

from ritesmith.workflows.simulation import WorkflowSimulator
from ritesmith.workflows.templates import (
    TEMPLATES,
    build_template,
    requested_template,
)
from ritesmith.workflows.validator import WorkflowValidator

BASE = "http://ritesmith:8081"
CAPS = {
    name: {}
    for name in (
        "home.execute",
        "telegram.send",
        "runtime.timestamp",
        "market.coin_price",
        "market.exchange_rate",
    )
}

THRESHOLD = {
    "read": {"capability": "ac.read", "target": "hugo"},
    "metric_path": "state.current_temperature",
    "op": "<",
    "value": 25,
    "action": {
        "capability": "remote.press",
        "target": "ventilador_hugo",
        "input": {"button": "desligar"},
    },
    "notify": "Quarto do Hugo a {value} °C: desliguei o ventilador.",
    "interval_seconds": 600,
    "window_seconds": 3600,
}


def ac(temp, ok=True):
    return {"success": ok, "result": {"state": {"current_temperature": temp}} if ok else {}}


def built(name, params):
    found = requested_template({"template": {"name": name, "params": params}})
    assert found is not None and found[0] == name
    definition = build_template(found[0], found[1], BASE)
    assert WorkflowValidator(CAPS).validate(definition) == []
    return definition


def clock(definition):
    # The deadline baked into the graph; the fake clock walks up to and past it.
    for node in definition["nodes"]:
        if node["id"] == "check_deadline":
            return node["cases"][0]["when"][">"][1]
    raise AssertionError("no deadline")


def test_threshold_acts_once_notifies_once_and_stops():
    definition = built("threshold_action", THRESHOLD)
    deadline = clock(definition)
    sim = WorkflowSimulator(
        definition,
        {
            "read": [ac(27), ac(26.5), ac(24.5)],
            "clock": [{"result": deadline - 100}, {"result": deadline - 50}],
            "act_entry": {"success": True, "result": {}},
            "notify": {"ok": True, "message_id": 1},
        },
    ).run()
    assert sim.calls["home.execute"] == 4  # 3 reads + 1 action
    assert sim.calls["telegram.send"] == 1
    assert sim.clock == 1200  # two 10-minute waits before the crossing


def test_threshold_message_carries_the_reading():
    definition = built("threshold_action", THRESHOLD)
    notify = next(n for n in definition["nodes"] if n["id"] == "notify")
    text = notify["action"]["request"]["body"]["input"]["text"]
    assert "{{ nodes.read.response.body.output.result.state.current_temperature }}" in text


def test_threshold_failed_reads_never_trigger_and_deadline_stops_it():
    definition = built("threshold_action", {**THRESHOLD, "timeout_notify": "Noite sem ação."})
    deadline = clock(definition)
    sim = WorkflowSimulator(
        definition,
        {
            "read": [ac(None, ok=False), ac(26), ac(26)],
            "clock": [
                {"result": deadline - 10},
                {"result": deadline - 5},
                {"result": deadline + 1},
            ],
            "timeout_entry": {"ok": True, "message_id": 2},
        },
    ).run()
    assert sim.calls["home.execute"] == 3  # no action: never crossed
    assert sim.calls["telegram.send"] == 1  # only the timeout note


def test_threshold_notify_only():
    params = {k: v for k, v in THRESHOLD.items() if k != "action"}
    definition = built("threshold_action", params)
    sim = WorkflowSimulator(
        definition,
        {"read": [ac(20)], "act_entry": {"ok": True, "message_id": 3}},
    ).run()
    assert sim.calls == {"home.execute": 1, "telegram.send": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"op": "between"},
        {"interval_seconds": 5},
        {"window_seconds": 30 * 86400},
        {"metric_path": "state; drop"},
        {"read": {"capability": "rm -rf", "target": "x"}},
        {"extra": 1},
    ],
)
def test_threshold_params_are_closed(change):
    with pytest.raises(ValidationError):
        requested_template(
            {"template": {"name": "threshold_action", "params": {**THRESHOLD, **change}}}
        )


def test_threshold_needs_an_effect():
    params = {k: v for k, v in THRESHOLD.items() if k not in ("action", "notify")}
    with pytest.raises(ValidationError):
        requested_template({"template": {"name": "threshold_action", "params": params}})


PRICE = {
    "kind": "crypto",
    "symbol": "BTC",
    "currency": "BRL",
    "percent": 5,
    "interval_seconds": 3600,
    "window_seconds": 86400,
}


def price(value):
    return {"price": value, "symbol": "btc", "currency": "brl"}


def test_price_variation_uses_contract_args_and_notifies_once_on_swing():
    definition = built("price_variation", PRICE)
    base = next(n for n in definition["nodes"] if n["id"] == "base")
    assert base["action"]["request"]["body"] == {
        "capability_name": "market.coin_price",
        "input": {"symbol": "btc", "currency": "brl"},
    }
    deadline = clock(definition)
    sim = WorkflowSimulator(
        definition,
        {
            "base": price(400000),
            "fetch": [price(410000), price(395000), price(379000)],
            "clock": [{"result": deadline - 3}, {"result": deadline - 2}],
            "notify": {"ok": True, "message_id": 1},
        },
    ).run()
    assert sim.calls["market.coin_price"] == 4
    assert sim.calls["telegram.send"] == 1


def test_price_variation_quiet_day_ends_at_deadline_without_message():
    definition = built("price_variation", {**PRICE, "direction": "up"})
    deadline = clock(definition)
    sim = WorkflowSimulator(
        definition,
        {
            "base": price(400000),
            "fetch": [price(380000), price(410000)],  # -5% is ignored for direction=up
            "clock": [{"result": deadline - 1}, {"result": deadline + 1}],
        },
    ).run()
    assert sim.calls["telegram.send"] == 0


def test_fiat_variation_reads_exchange_rate():
    definition = built("price_variation", {**PRICE, "kind": "fiat", "symbol": "USD"})
    base = next(n for n in definition["nodes"] if n["id"] == "base")
    assert base["action"]["request"]["body"]["input"] == {
        "from_currency": "usd",
        "to_currency": "brl",
    }


def test_presence_reminder_keeps_its_original_contract():
    found = requested_template(
        {"presence_reminder": {"person_alias": "thiago", "message": "lixo", "channel": "telegram"}}
    )
    assert found[0] == "presence_reminder"
    assert WorkflowValidator(CAPS).validate(build_template(*found, BASE)) == []


def test_unknown_template_is_rejected_with_the_list():
    with pytest.raises(ValueError, match="price_variation"):
        requested_template({"template": {"name": "nope", "params": {}}})
    assert requested_template({"schedule_hours": 1}) is None
    assert set(TEMPLATES) >= {"presence_reminder", "threshold_action", "price_variation"}
