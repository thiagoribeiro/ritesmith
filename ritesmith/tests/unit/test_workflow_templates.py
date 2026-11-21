"""Caller-selected recipes: closed params, valid graphs, and the behaviour they promise."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ritesmith.workflows.simulation import WorkflowSimulator
from ritesmith.workflows.templates import (
    TEMPLATES,
    ScheduledReminder,
    build_template,
    requested_template,
    scheduled_reminder_workflow,
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


def test_state_match_with_start_delay():
    params = {
        "read": {"capability": "presence.read", "target": "sensor_porta_frente"},
        "metric_path": "present",
        "op": "==",
        "value": True,
        "action": {"capability": "alarm.home", "target": "sirene"},
        "notify": "Movimento na porta da frente: sirene em modo casa.",
        "interval_seconds": 60,
        "window_seconds": 6 * 3600,
        "start_delay_seconds": 3 * 3600,
    }
    definition = built("threshold_action", params)
    assert definition["entrypoint"] == "start_wait"
    deadline = clock(definition)
    sim = WorkflowSimulator(
        definition,
        {
            "read": [
                {"success": True, "result": {"present": False}},
                {"success": True, "result": {"present": True}},
            ],
            "clock": [{"result": deadline - 100}],
            "act_entry": {"success": True, "result": {}},
            "notify": {"ok": True, "message_id": 1},
        },
    ).run()
    assert sim.calls == {"home.execute": 3, "runtime.timestamp": 1, "telegram.send": 1}
    assert sim.clock == 3 * 3600 + 60


def test_ordering_ops_need_numbers():
    with pytest.raises(ValidationError):
        requested_template(
            {"template": {"name": "threshold_action", "params": {**THRESHOLD, "value": True}}}
        )


def here(flag):
    return {"success": True, "result": {"present": flag}}


def presence_clock(definition, prefix):
    node = next(n for n in definition["nodes"] if n["id"] == f"{prefix}_deadline")
    return node["cases"][0]["when"][">"][1]


def test_arrival_waits_for_a_real_transition():
    # "me avisa quando a Jaque chegar": already home at start must not fire.
    definition = built(
        "presence_change",
        {"people": ["jaque"], "event": "any_home", "notify": "A Jaque chegou."},
    )
    deadline = presence_clock(definition, "before")
    sim = WorkflowSimulator(
        definition,
        {
            "before_0": [here(True), here(False)],
            "before_clock": [{"result": deadline - 10}],
            "after_0": [here(False), here(True)],
            "after_clock": [{"result": deadline - 5}],
            "notify": {"ok": True, "message_id": 1},
        },
    ).run()
    assert sim.calls["telegram.send"] == 1
    assert sim.calls["home.execute"] == 4  # home, away, away, home


def test_everyone_leaving_runs_each_action_once_after_the_delay():
    definition = built(
        "presence_change",
        {
            "people": ["thiago", "jaque"],
            "event": "all_away",
            "delay_seconds": 900,
            "actions": [
                {"capability": "switch.off", "target": "interruptor_sala"},
                {"capability": "switch.off", "target": "interruptor_cozinha"},
            ],
        },
    )
    sim = WorkflowSimulator(
        definition,
        {
            "before_0": here(True),
            "before_1": here(False),
            "after_0": here(False),
            "after_1": here(False),
            "act_0": {"success": True, "result": {}},
            "act_1": {"success": True, "result": {}},
        },
    ).run()
    # 2 reads before + 2 reads after + 2 actions; no telegram (no notify asked)
    assert sim.calls == {"home.execute": 6}
    assert sim.clock == 900


def test_without_transition_an_already_true_state_fires_at_once():
    definition = built(
        "presence_change",
        {
            "people": ["thiago"],
            "event": "all_away",
            "require_transition": False,
            "notify": "Casa vazia.",
        },
    )
    assert definition["entrypoint"] == "after_0"
    sim = WorkflowSimulator(definition, {"after_0": here(False), "notify": {"ok": True}}).run()
    assert sim.calls == {"home.execute": 1, "telegram.send": 1}


def test_presence_change_stops_at_the_deadline_without_acting():
    definition = built(
        "presence_change",
        {"people": ["jaque"], "event": "any_home", "require_transition": False, "notify": "x"},
    )
    deadline = presence_clock(definition, "after")
    sim = WorkflowSimulator(
        definition,
        {"after_0": here(False), "after_clock": [{"result": deadline + 1}]},
    ).run()
    assert sim.calls["telegram.send"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"people": []},
        {"people": ["a", "A"]},
        {"event": "someone_left"},
        {"actions": [], "notify": None},
    ],
)
def test_presence_change_params_are_closed(change):
    params = {"people": ["thiago"], "event": "all_away", "notify": "x", **change}
    with pytest.raises(ValidationError):
        requested_template({"template": {"name": "presence_change", "params": params}})


# ── scheduled_reminder ──────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 21, 14, 7, tzinfo=UTC)  # 11:07 in São Paulo


def reminder(params, now=NOW):
    spec = ScheduledReminder.model_validate(params)
    definition = scheduled_reminder_workflow(spec, BASE, now=now)
    assert WorkflowValidator(CAPS).validate(definition) == []
    return spec, definition


def test_every_six_months_from_a_local_date():
    # The 2026-11-21 request: "15/05/2027 às 08:00, then every 6 months, on Telegram".
    spec, definition = reminder(
        {
            "message": "Colocar crédito no celular do Hugo.",
            "first_at": "2027-05-15T08:00",
            "repeat": {"every": 6, "unit": "months"},
            "count": 3,
        }
    )
    dates, capped = spec.occurrences(NOW)
    assert [d.isoformat() for d in dates] == [
        "2027-05-15T08:00:00-03:00",
        "2027-11-15T08:00:00-03:00",
        "2028-05-15T08:00:00-03:00",
    ]
    assert not capped
    sleeps = [n["durationSeconds"] for n in definition["nodes"] if n["kind"] == "sleep"]
    assert sleeps[0] == round((dates[0] - NOW).total_seconds())
    assert sum(sleeps) == round((dates[-1] - NOW).total_seconds())
    sim = WorkflowSimulator(definition, {"telegram.send": {"ok": True, "message_id": 1}}).run()
    assert sim.calls == {"telegram.send": 3}
    assert sim.clock == sum(sleeps)


def test_month_end_clamps_and_returns_to_the_anchor_day():
    spec = ScheduledReminder.model_validate(
        {
            "message": "x",
            "first_at": "2027-01-31T09:00",
            "repeat": {"every": 1, "unit": "months"},
            "count": 3,
        }
    )
    days = [d.date().isoformat() for d in spec.occurrences(NOW)[0]]
    assert days == ["2027-01-31", "2027-02-28", "2027-03-31"]


def test_one_shot_and_until():
    spec, definition = reminder({"message": "Renovar CNH", "first_at": "2027-02-01T10:00"})
    assert [n["id"] for n in definition["nodes"]] == ["wait_0", "remind_0"]
    spec = ScheduledReminder.model_validate(
        {
            "message": "x",
            "first_at": "2027-01-01T08:00",
            "repeat": {"every": 1, "unit": "years"},
            "until": "2029-01-01",
        }
    )
    assert len(spec.occurrences(NOW)[0]) == 3  # until is inclusive


def test_open_series_is_capped_and_says_so():
    spec, definition = reminder(
        {"message": "x", "first_at": "2027-05-15T08:00", "repeat": {"every": 6, "unit": "months"}}
    )
    dates, capped = spec.occurrences(NOW)
    assert capped and len(dates) == 21  # 10 years, both ends included
    last = definition["nodes"][-1]["action"]["request"]["body"]["input"]["text"]
    assert "Último lembrete" in last


@pytest.mark.parametrize(
    "params, error",
    [
        ({"first_at": "2026-11-21T08:00"}, "not in the future"),
        ({"first_at": "2040-01-01T08:00"}, "years away"),
        ({"first_at": "2027-05-15T08:00", "count": 2}, "needs repeat"),
        ({"first_at": "2027-05-15T08:00", "timezone": "Mars/Base"}, "unknown timezone"),
    ],
)
def test_bad_schedules_are_refused(params, error):
    with pytest.raises(ValueError, match=error):
        reminder({"message": "x", **params})


def test_presence_reminder_with_schedule_fields_is_refused():
    # The misroute behind the 2026-11-21 incident: a dated reminder sent with
    # presence_reminder silently became "remind me on my next arrival".
    with pytest.raises(ValueError, match="scheduled_reminder"):
        requested_template(
            {
                "schedule_hours": 4380,
                "presence_reminder": {
                    "person_alias": "thiago",
                    "message": "x",
                    "channel": "telegram",
                },
            }
        )


def test_scheduled_reminder_through_the_template_contract():
    found = requested_template(
        {
            "template": {
                "name": "scheduled_reminder",
                "params": {"message": "x", "first_at": "2030-01-01T08:00"},
            }
        }
    )
    assert found[0] == "scheduled_reminder"
    assert WorkflowValidator(CAPS).validate(build_template(*found, BASE)) == []


def test_threshold_action_runs_every_action_in_order():
    # 2026-11-20: "desliga o ar e liga o ventilador no 3" fit a one-action recipe.
    params = {
        **THRESHOLD,
        "action": None,
        "actions": [
            {"capability": "ac.turn_off", "target": "hugo"},
            {
                "capability": "remote.press",
                "target": "ventilador_hugo",
                "input": {"button": "velocidade 3"},
            },
        ],
    }
    params.pop("action")
    definition = built("threshold_action", params)
    deadline = clock(definition)
    sim = WorkflowSimulator(
        definition,
        {
            "read": [ac(27), ac(24.5)],
            "clock": [{"result": deadline - 100}],
            "act_entry": {"success": True, "result": {}},
            "act_1": {"success": True, "result": {}},
            "notify": {"ok": True, "message_id": 1},
        },
    ).run()
    assert sim.calls == {"home.execute": 4, "runtime.timestamp": 1, "telegram.send": 1}
    order = [
        n["action"]["request"]["body"]["input"].get("capability")
        for n in definition["nodes"]
        if n["id"] in ("act_entry", "act_1")
    ]
    assert order == ["ac.turn_off", "remote.press"]
    with pytest.raises(ValueError, match="not both"):
        requested_template(
            {
                "template": {
                    "name": "threshold_action",
                    "params": {**params, "action": THRESHOLD["action"]},
                }
            }
        )
