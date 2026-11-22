"""Provider plugins (entry points) and deployment prompt hints."""

import json

import pytest

from ritesmith.llm import agent_loop, hints
from ritesmith.runtime import providers as registry
from ritesmith.runtime.providers.base import HostFunctionDef, ToolProvider


class FakeHome(ToolProvider):
    namespace = "home"
    profile = "side_effects"
    virtual_device_types = frozenset({"person"})

    def is_available(self) -> bool:
        return True

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {}

    def mcp_tools(self):
        return []

    def prompt_hints(self):
        return {"ambiguity_example": "Qual luz? varanda, garagem", "person_examples": "carol"}


class Extra(FakeHome):
    namespace = "extra"


class EP:
    def __init__(self, name, obj, value="pkg.mod:Obj"):
        self.name, self._obj, self.value = name, obj, value

    def load(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def build_with(monkeypatch, eps, disabled=""):
    monkeypatch.setattr(
        registry, "entry_points", lambda group: eps if group == "ritesmith.providers" else []
    )
    monkeypatch.setenv("RITESMITH_DISABLED_PROVIDERS", disabled)
    registry.PLUGIN_SOURCES.clear()
    return registry._build()


def test_plugin_replaces_builtin_namespace_and_adds_new(monkeypatch):
    built = build_with(
        monkeypatch, [EP("home", FakeHome, "private.home:FakeHome"), EP("extra", Extra())]
    )
    homes = [p for p in built if p.namespace == "home"]
    assert len(homes) == 1 and isinstance(homes[0], FakeHome)
    assert any(p.namespace == "extra" for p in built)
    assert registry.PLUGIN_SOURCES["home"] == "private.home:FakeHome"
    # order of the built-ins is kept: the plugin takes the built-in's slot
    assert [p.namespace for p in built][: len(registry._BUILTIN)] == [
        p.namespace for p in registry._BUILTIN
    ]


def test_broken_plugin_is_skipped(monkeypatch, caplog):
    built = build_with(monkeypatch, [EP("bad", ImportError("nope")), EP("notprovider", object)])
    assert [p.namespace for p in built] == [p.namespace for p in registry._BUILTIN]
    assert "failed to load" in caplog.text


def test_disabled_providers(monkeypatch):
    built = build_with(monkeypatch, [], disabled="web, market")
    names = {p.namespace for p in built}
    assert "web" not in names and "market" not in names and "stat" in names


def test_hints_defaults_are_neutral(monkeypatch):
    monkeypatch.setattr(
        registry, "PROVIDERS", [p for p in registry.PROVIDERS if p.namespace != "home"]
    )
    h = hints.hints()
    assert h == hints.DEFAULTS
    # defaults only name the example house (home_example.py)
    assert "alice" in h["person_examples"] and "quarto" in h["ambiguity_example"]


def test_hints_merge_order(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "PROVIDERS", [FakeHome()])
    assert hints.hints()["ambiguity_example"] == "Qual luz? varanda, garagem"
    f = tmp_path / "hints.json"
    f.write_text(json.dumps({"person_examples": "dave, erin", "ignored": 3}))
    monkeypatch.setenv("RITESMITH_PROMPT_HINTS", str(f))
    hints.hints.cache_clear()
    h = hints.hints()
    assert h["person_examples"] == "dave, erin"  # file wins
    assert h["ambiguity_example"] == "Qual luz? varanda, garagem"  # provider over default


def test_bad_hints_file_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("RITESMITH_PROMPT_HINTS", str(tmp_path / "missing.json"))
    assert hints.hints()["person_examples"]


def test_prompts_have_no_unfilled_markers(monkeypatch):
    monkeypatch.setattr(registry, "PROVIDERS", [FakeHome()])
    system = agent_loop._static_system()
    tools = json.dumps(agent_loop._response_tools(), ensure_ascii=False)
    assert "__HINT_" not in system and "__HINT_" not in tools
    assert "Qual luz? varanda, garagem" in system
    assert "carol" in tools


def test_list_devices_uses_provider_normalization(monkeypatch):
    class Shaped(FakeHome):
        def normalize_device(self, raw):
            return {
                "target": raw["alias"],
                "type": raw["kind"],
                "capabilities": [],
                "state": {"on": True},
            }

    monkeypatch.setattr(registry, "PROVIDERS", [Shaped()])
    from ritesmith.runtime import host_functions

    devices = HostFunctionDef(
        "home.devices",
        "side_effects",
        lambda: {
            "devices": [{"alias": "luz_x", "kind": "light"}, {"alias": "carol", "kind": "person"}]
        },
    )
    monkeypatch.setattr(
        host_functions, "get_capability", lambda name: devices if name == "home.devices" else None
    )
    gen = agent_loop.AgentGeneration.__new__(agent_loop.AgentGeneration)
    rows = gen.list_devices()["devices"]
    assert rows[0]["target"] == "luz_x" and rows[0]["state_example"] == {"on": True}
    assert rows[1]["online"] == "virtual"


@pytest.mark.parametrize("provider", registry.PROVIDERS, ids=lambda p: p.namespace)
def test_every_provider_has_the_optional_hooks(provider):
    assert isinstance(provider.prompt_hints(), dict)
    assert isinstance(provider.virtual_device_types, frozenset)
    provider.reset_caches()


def test_plugin_api_does_not_build_the_registry():
    """A plugin imports its base class first; that must not load plugins (it would load
    itself half-imported). Checked in a fresh interpreter."""
    import subprocess
    import sys

    code = "import sys, ritesmith.plugin_api; print('ritesmith.runtime.providers' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
