"""`stop` ends a repeat: "watch X; when Y do A and B, tell me, and stop" as a generated plan.

Before it, a semantic repeat could only end by count/duration/deadline, so act-once
monitors had to be squeezed into recipes. On 2026-11-20 "desliga o ar e liga o ventilador"
became a one-action threshold_action that would never have turned the AC off.
"""

import pytest

from ritesmith.workflows.readback import describe
from ritesmith.workflows.semantic import compile_plan
from ritesmith.workflows.simulation import WorkflowSimulator
from ritesmith.workflows.validator import WorkflowValidator

BASE = "http://ritesmith:8081"
CAPS = {c: {} for c in ("home.execute", "telegram.send", "stat.chain_step")}


def home(op_id, capability, target, input_=None):
    return {
        "kind": "call",
        "id": op_id,
        "capability_name": "home.execute",
        "args": {"capability": capability, "target": target, "input": input_ or {}},
    }


def watch(**repeat):
    return {
        "version": "workflow-plan-v1",
        "name": "ac_caio",
        "steps": [
            {
                "kind": "repeat",
                "id": "watch",
                "interval_seconds": 300,
                **repeat,
                "steps": [
                    home("read", "ac.read", "caio"),
                    {
                        "kind": "condition",
                        "when": {
                            "<": [{"ref": "read", "path": "result.state.current_temperature"}, 26]
                        },
                        "steps": [
                            home("ac_off", "ac.turn_off", "caio"),
                            home(
                                "fan", "remote.press", "ventilador_caio", {"button": "velocidade 3"}
                            ),
                            {
                                "kind": "call",
                                "id": "notify",
                                "capability_name": "telegram.send",
                                "args": {"text": "Desliguei o ar e liguei o ventilador no 3."},
                            },
                            {"kind": "stop"},
                        ],
                    },
                ],
            }
        ],
    }


def ac(temp):
    return {"success": True, "result": {"state": {"current_temperature": temp}}}


@pytest.mark.parametrize("repeat", [{"duration_seconds": 28800}, {"count": 5}])
def test_acts_once_with_every_action_then_stops(repeat):
    definition = compile_plan(watch(**repeat), BASE)
    assert WorkflowValidator(CAPS).validate(definition) == []
    done = {"success": True, "result": {}}
    sim = WorkflowSimulator(
        definition,
        {
            "home.execute": [ac(27), ac(26.5), ac(25.5), done, done, ac(24), ac(24), ac(24)],
            "telegram.send": {"ok": True, "message_id": 1},
        },
    ).run()
    # 3 reads, then both actions once; nothing is read or sent after the stop.
    assert sim.calls["home.execute"] == 5 and sim.calls["telegram.send"] == 1
    assert sim.clock == 600


def test_never_crossing_runs_the_whole_window():
    sim = WorkflowSimulator(compile_plan(watch(count=5), BASE), {"home.execute": ac(27)}).run()
    assert sim.calls == {"home.execute": 5, "stat.chain_step": 5}


def test_readback_says_once_and_stops():
    text = describe(compile_plan(watch(duration_seconds=28800), BASE)).text
    assert "executa ac.turn_off em caio e depois executa remote.press" in text
    assert "— uma vez, e encerra (verifica a cada 5 min)" in text
    assert "até completar ≈ 8 h no total" in text


@pytest.mark.parametrize(
    "steps",
    [
        [{"kind": "stop"}],  # outside a repeat
        [
            {
                "kind": "repeat",
                "id": "r",
                "interval_seconds": 60,
                "count": 2,
                "steps": [
                    {
                        "kind": "parallel",
                        "id": "p",
                        "branches": [{"kind": "stop"}, home("x", "ac.read", "caio")],
                    }
                ],
            }
        ],
    ],
)
def test_stop_only_inside_a_repeat_and_never_in_a_branch(steps):
    with pytest.raises(ValueError, match="stop ends the enclosing repeat"):
        compile_plan({"version": "workflow-plan-v1", "name": "x", "steps": steps}, BASE)
