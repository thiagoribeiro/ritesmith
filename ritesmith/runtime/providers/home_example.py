"""home.* — an in-memory example house implementing the home capability contract.

Real deployments install their own `home` provider as a plugin (entry point
``ritesmith.providers``, see docs/home-capability-contract.md); this one exists so
the contract has a runnable reference, and it is only active with
``RITESMITH_HOME_PROVIDER=example`` so it can never shadow a real house.
"""

from __future__ import annotations

import copy
import os
import threading

from ritesmith.plugin_api import HostFunctionDef, MCPToolDef, ToolProvider

_INITIAL: dict[str, dict] = {
    "alice": {
        "name": "Alice",
        "type": "person",
        "room": None,
        "capabilities": ["presence.read"],
        "state": {"present": True},
    },
    "bob": {
        "name": "Bob",
        "type": "person",
        "room": None,
        "capabilities": ["presence.read"],
        "state": {"present": False},
    },
    "luz_sala": {
        "name": "Luz da sala",
        "type": "smart-switch",
        "room": "sala",
        "capabilities": ["switch.read", "switch.on", "switch.off"],
        "state": {"on": False},
    },
    "ventilador_quarto": {
        "name": "Ventilador do quarto",
        "type": "fan",
        "room": "quarto",
        "capabilities": ["fan.read", "fan.set_speed", "fan.off"],
        "state": {"on": False, "speed": 0},
    },
    "ar_quarto": {
        "name": "Ar do quarto",
        "type": "ac-unit",
        "room": "quarto",
        "capabilities": ["ac.read", "ac.set", "ac.turn_off"],
        "state": {"on": False, "target_c": 24, "temperature_c": 27.5},
    },
}


class ExampleHome:
    """The house's state; thread-safe, reset with reset()."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.devices = copy.deepcopy(_INITIAL)

    def list(self) -> dict:
        with self._lock:
            return {
                "devices": [
                    {"target": t, "online": True, **copy.deepcopy(d)}
                    for t, d in self.devices.items()
                ]
            }

    def execute(self, capability: str, target: str, input: dict | None = None) -> dict:
        input = input or {}
        with self._lock:
            device = self.devices.get(target)
            if device is None:
                return {"success": False, "error": "unknown_target", "message": target}
            if capability not in device["capabilities"]:
                return {"success": False, "error": "unsupported", "message": capability}
            state = device["state"]
            verb = capability.split(".", 1)[1]
            if verb in ("on",):
                state["on"] = True
            elif verb in ("off", "turn_off"):
                state["on"] = False
                if "speed" in state:
                    state["speed"] = 0
            elif verb == "set_speed":
                state["speed"] = int(input.get("speed", 1))
                state["on"] = state["speed"] > 0
            elif verb == "set":
                state.update({k: v for k, v in input.items() if k in ("on", "target_c")})
                state["on"] = input.get("on", True)
            return {"success": True, "result": {"state": copy.deepcopy(state)}}


HOUSE = ExampleHome()


class ExampleHomeProvider(ToolProvider):
    namespace = "home"
    profile = "side_effects"
    risk_level = "medium"
    side_effects = "physical_world"
    virtual_device_types = frozenset({"person"})

    def is_available(self) -> bool:
        return os.environ.get("RITESMITH_HOME_PROVIDER", "").strip().lower() == "example"

    def reset_caches(self) -> None:
        HOUSE.reset()

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        def devices() -> dict:
            return HOUSE.list()

        def execute(capability: str, target: str, input: dict | None = None) -> dict:
            return HOUSE.execute(capability, target, input)

        def state(target: str) -> dict:
            row = next((d for d in HOUSE.list()["devices"] if d["target"] == target), None)
            return {"target": target, "state": row["state"]} if row else {"error": "unknown_target"}

        def capabilities() -> dict:
            caps = sorted({c for d in HOUSE.list()["devices"] for c in d["capabilities"]})
            return {"capabilities": caps}

        p = self.profile
        return {
            "home.devices": HostFunctionDef(
                "home.devices",
                p,
                devices,
                "List devices: target, name, type, room, capabilities, online, state.",
            ),
            "home.execute": HostFunctionDef(
                "home.execute",
                p,
                execute,
                "Run a device capability: {capability, target, input} → {success, result}.",
            ),
            "home.state": HostFunctionDef("home.state", p, state, "Current state of one device."),
            "home.capabilities": HostFunctionDef(
                "home.capabilities", p, capabilities, "Every device capability in the house."
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        return []
