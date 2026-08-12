"""CASP host functions — registered under 'trusted_internal' profile.

These are Python callables exposed to Lua scripts as casp.* functions.
They call LoomHarbor (or any CASP provider) via the internal httpx client
and bypass _BLOCKED_PATTERNS by design (structured internal calls only).
"""

from __future__ import annotations

from ritesmith.runtime.casp import client as _casp
from ritesmith.runtime.host_functions import _register


def _casp_query(resource_type: str, filters: dict, capability: str) -> dict:
    """Query CASP provider for resources matching type + filters."""
    return _casp.query(resource_type, filters, [capability] if capability else [])


def _casp_resolve(resource_type: str, capability: str, hint: str) -> dict:
    """Resolve a natural-language hint to a specific resource ID."""
    return _casp.resolve(resource_type, capability, hint)


def _casp_execute(resource_id: str, resource_type: str, capability: str, input_data: dict) -> dict:
    """Execute a capability on a specific resource."""
    return _casp.execute(resource_id, resource_type, capability, input_data or {})


_register("casp.query", "trusted_internal", _casp_query)
_register("casp.resolve", "trusted_internal", _casp_resolve)
_register("casp.execute", "trusted_internal", _casp_execute)
