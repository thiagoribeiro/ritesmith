"""The example house: a runnable reference of the home capability contract."""

import pytest

from ritesmith.runtime.providers import provider_for
from ritesmith.runtime.providers.home_example import HOUSE, ExampleHomeProvider


@pytest.fixture
def fns(monkeypatch):
    monkeypatch.setenv("RITESMITH_HOME_PROVIDER", "example")
    HOUSE.reset()
    return {k: v.callable for k, v in ExampleHomeProvider().lua_functions().items()}


def test_only_active_when_asked(monkeypatch):
    monkeypatch.delenv("RITESMITH_HOME_PROVIDER", raising=False)
    assert ExampleHomeProvider().is_available() is False
    monkeypatch.setenv("RITESMITH_HOME_PROVIDER", "example")
    assert ExampleHomeProvider().is_available() is True


def test_is_the_builtin_home_provider():
    assert isinstance(provider_for("home"), ExampleHomeProvider)


def test_devices_follow_the_contract_shape(fns):
    rows = fns["home.devices"]()["devices"]
    targets = {r["target"] for r in rows}
    assert {"alice", "bob", "luz_sala", "ventilador_quarto", "ar_quarto"} <= targets
    for r in rows:
        assert set(r) >= {"target", "name", "type", "room", "capabilities", "online", "state"}


def test_execute_changes_and_reads_state(fns):
    out = fns["home.execute"](capability="switch.on", target="luz_sala", input={})
    assert out == {"success": True, "result": {"state": {"on": True}}}
    assert fns["home.state"](target="luz_sala")["state"]["on"] is True
    fan = fns["home.execute"](
        capability="fan.set_speed", target="ventilador_quarto", input={"speed": 3}
    )
    assert fan["result"]["state"] == {"on": True, "speed": 3}
    assert fns["home.execute"](capability="presence.read", target="alice")["result"]["state"][
        "present"
    ]


def test_execute_errors(fns):
    assert fns["home.execute"](capability="switch.on", target="nope")["error"] == "unknown_target"
    assert fns["home.execute"](capability="ac.set", target="luz_sala")["error"] == "unsupported"


def test_reset_caches_restores_the_house(fns):
    fns["home.execute"](capability="switch.on", target="luz_sala")
    ExampleHomeProvider().reset_caches()
    assert fns["home.state"](target="luz_sala")["state"]["on"] is False
