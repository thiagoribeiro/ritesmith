# The `home` capability contract

Device control reaches RiteSmith through one provider namespace, `home`. RiteSmith
ships only an example house ([`home_example.py`](../ritesmith/runtime/providers/home_example.py),
active with `RITESMITH_HOME_PROVIDER=example`). A real deployment installs its own
provider as a **plugin**, and the plugin replaces the example.

## Installing a provider

A plugin is a Python package that declares an entry point in the `ritesmith.providers`
group:

```toml
[project.entry-points."ritesmith.providers"]
home = "my_house.provider:MyHomeProvider"
```

- The provider subclasses `ritesmith.plugin_api.ToolProvider`. Import it from
  `ritesmith.plugin_api`, not from `ritesmith.runtime.providers`: that package builds the
  registry, so importing from it would load the plugin before it has finished importing.
- A plugin with the same namespace as a built-in replaces the built-in.
- `GET /providers` shows which plugin supplied each namespace.
- A plugin that fails to import is logged and skipped.
- `RITESMITH_DISABLED_PROVIDERS` drops namespaces without a rebuild.

A common way to ship it is an image built on top of the public one:

```dockerfile
FROM ritesmith:<tag>
COPY . /opt/my-house
RUN pip install --no-cache-dir --no-deps /opt/my-house
```

## Capabilities

The agent, the templates (`threshold_action`, `presence_change`, `presence_reminder`)
and the readback rely on two capabilities:

| Capability | Arguments | Returns |
|---|---|---|
| `home.devices` | none | `{devices: [...]}`. Each device goes through the provider's `normalize_device()` into `{target, name, type, room, capabilities, online, state}`. |
| `home.execute` | `capability`, `target`, `input` | `{success, result: {state}}`. Reads put the device state in `result.state`. |

Optional capabilities: `home.state`, `home.capabilities`, and provider-specific
extras such as switches, alarms, sensors or GPS.

**Device capabilities** are the strings in a device's `capabilities` list.
- They are called through `home.execute`; they are not catalog entries.
- They follow the pattern `<kind>.<verb>`. Examples:
  - `switch.read`, `switch.on`, `switch.off`;
  - `fan.set_speed`;
  - `ac.read`, `ac.set`, `ac.turn_off`;
  - `presence.read`, which returns `state.present`;
  - `remote.buttons` and `remote.press`, for IR remotes.
- Templates rely only on these strings and on `state` fields. They make no assumption
  about the device API behind them.

**Virtual devices** are people, trackers and anything else that is not a physical
device.
- List their types in the provider's `virtual_device_types`.
- Their `online` flag is ignored, and reads still work.

## Prompt hints

The generation prompts use examples of devices and people, for instance in a refusal
that lists ambiguous matches. The defaults name the example house. A provider can
return its own examples from `prompt_hints()`, and the `RITESMITH_PROMPT_HINTS` JSON
file overrides both. Keys:

- `ambiguity_example`
- `person_examples`

Hints are resolved once per process, so the prompt stays byte-stable for prompt
caching.
