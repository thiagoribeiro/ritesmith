"""Contracts every tool provider must honour (runs across all of PROVIDERS)."""

import inspect

import jsonschema
import pytest

from ritesmith.runtime.host_functions import PROFILES
from ritesmith.runtime.providers import PROVIDERS

ALL = [pytest.param(p, id=p.namespace) for p in PROVIDERS]


def _accepts_kwargs(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True  # builtins / C functions: can't introspect, don't enforce
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _param_names(fn) -> set[str]:
    try:
        return {
            p.name
            for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }
    except (TypeError, ValueError):
        return set()


@pytest.mark.parametrize("provider", ALL)
def test_namespace_and_profile_are_valid(provider):
    assert provider.namespace and provider.namespace.isidentifier()
    assert provider.profile in PROFILES


@pytest.mark.parametrize("provider", ALL)
def test_lua_function_names_are_well_formed(provider):
    for key, fn_def in provider.lua_functions().items():
        assert fn_def.name == key, f"HostFunctionDef.name != key for {key}"
        assert key.count(".") == 1, f"tool name must be namespace.fn: {key}"
        ns, fn = key.split(".")
        assert ns == provider.namespace, f"{key} not under namespace {provider.namespace}"
        assert fn.isidentifier()


def _schema_tools():
    params = []
    for provider in PROVIDERS:
        for key, fn_def in provider.lua_functions().items():
            params.append(pytest.param(fn_def, id=key))
    return params


def _uses_single_table_convention(fn) -> bool:
    """report.* tools are `def f(args)`: one table arg unpacked from Lua, not top-level kwargs."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    return len(params) == 1 and params[0].name == "args"


def _checked_props(fn_def) -> set[str] | None:
    """Declared input properties, or None when the schema does not bind to parameters."""
    schema = getattr(fn_def, "input_schema", None) or {}
    props = set((schema.get("properties") or {}).keys())
    if (
        not props
        or _accepts_kwargs(fn_def.callable)
        or _uses_single_table_convention(fn_def.callable)
    ):
        return None
    return props


@pytest.mark.parametrize("fn_def", _schema_tools())
def test_input_schema_properties_match_callable_params(fn_def):
    """Every declared input property must be a real parameter (the deploy-time invariant)."""
    props = _checked_props(fn_def)
    if props is None:
        return
    extra = props - _param_names(fn_def.callable)
    assert not extra, f"{fn_def.name}: input_schema declares non-parameters {extra}"


@pytest.mark.parametrize("fn_def", _schema_tools())
def test_required_callable_params_are_declared(fn_def):
    """Contracts are closed, so a parameter the schema omits can never be supplied."""
    props = _checked_props(fn_def)
    if props is None:
        return
    required = {
        p.name
        for p in inspect.signature(fn_def.callable).parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) and p.default is p.empty
    }
    missing = required - props
    assert not missing, f"{fn_def.name}: required parameters missing from input_schema {missing}"


# The bridge returns a non-dict result as {"result": value}; the declared output type must
# match what the callable returns so references validate against the real shape.
_RETURN_TYPES = {
    "list": "array",
    "str": "string",
    "float": "number",
    "int": "integer",
    "bool": "boolean",
}

# Namespaces whose functions do not declare output contracts yet (device/CASP payloads vary).
_UNDECLARED_OUTPUT_NAMESPACES = {"casp", "home"}


@pytest.mark.parametrize("fn_def", _schema_tools())
def test_output_schema_matches_return_type(fn_def):
    if fn_def.name.split(".")[0] in _UNDECLARED_OUTPUT_NAMESPACES:
        return
    schema = getattr(fn_def, "output_schema", None)
    assert schema, f"{fn_def.name}: missing output_schema"
    annotation = str(inspect.signature(fn_def.callable).return_annotation).strip("'\"")
    base = annotation.split("[")[0]
    if "|" in annotation or base not in {**_RETURN_TYPES, "dict": None}:
        return  # unions / unannotated: nothing to compare
    expected = _RETURN_TYPES.get(base, "object")
    declared = schema.get("type") or ("object" if schema.get("properties") else None)
    assert declared == expected, (
        f"{fn_def.name}: returns {annotation} but output_schema declares {declared!r}"
    )


@pytest.mark.parametrize("provider", ALL)
def test_mcp_tools_have_valid_json_schema(provider):
    for tool in provider.mcp_tools():
        assert tool.name and tool.description
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)


@pytest.mark.parametrize("provider", ALL)
def test_manifest_and_capabilities_shape(provider):
    manifest = provider.manifest()
    assert manifest["namespace"] == provider.namespace
    assert set(manifest) >= {"namespace", "profile", "risk_level", "available", "lua_functions"}
    for cap in provider.capabilities():
        assert cap["kind"] == "script_function"
        assert cap["name"] in provider.lua_functions()


def test_namespaces_are_unique():
    names = [p.namespace for p in PROVIDERS]
    assert len(names) == len(set(names))


# The Luau type checker must accept every provider tool signature (deploy sigcheck as a test).
try:
    from ritesmith.runtime import luau

    _LUAU = luau.luau_available()
except Exception:
    _LUAU = False


@pytest.mark.skipif(not _LUAU, reason="lunardyson not installed")
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_profile_builds_a_valid_luau_tool_table(profile):
    rt = luau.build_runtime(profile, 1000, 16, type_decls=luau.script_type_declarations(None, None))
    try:
        diags = rt.check("function run(input: Input, context: Context): Output return {} end")
    finally:
        rt.close()
    errors = [d for d in diags if d["severity"] == "error"]
    assert errors == [], errors
