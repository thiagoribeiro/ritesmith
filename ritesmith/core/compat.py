"""Deterministic contract-compatibility filter for reuse (P0.4, stage 2).

This is the *security gate* of the three-stage reuse pipeline: it runs before the
LLM judge so the judge only ever sees candidates that are already safe to run in
place of the request. A candidate is compatible when:

  - it produces every field the request requires, with compatible types
    (output_satisfies);
  - it does not require inputs the request will not provide (input_satisfiable);
  - its runtime profile's host functions are a subset of the requested profile's
    (profile_subset — "effects ⊆");
  - its risk level is within the allowed ceiling;
  - its artifact type matches (lua_script/luau_script are interchangeable run
    targets; anything else must match exactly).

Missing schemas are handled conservatively: if the request imposes a contract the
candidate cannot be *proven* to satisfy, it is rejected (regenerate rather than
reuse the wrong thing). This matches the "strict contract compat" decision.
"""

from __future__ import annotations

from ritesmith.registry.search import SearchResult
from ritesmith.runtime.host_functions import list_names_for_profile

_RISK_ORDER = ["low", "medium", "high", "critical"]
_SCRIPT_TYPES = {"lua_script", "luau_script"}


def _norm_type(t: str | None) -> str | None:
    return "number" if t == "integer" else t


def _schema_type(s: object) -> str | None:
    return s.get("type") if isinstance(s, dict) else None


def _type_compatible(a: str | None, b: str | None) -> bool:
    # Unknown type on either side → treat as `any` (compatible).
    if a is None or b is None:
        return True
    return _norm_type(a) == _norm_type(b)


def _risk_within(candidate: str | None, allowed: str | None) -> bool:
    if allowed is None:
        return True
    cand = candidate or "low"
    if cand not in _RISK_ORDER or allowed not in _RISK_ORDER:
        return False
    return _RISK_ORDER.index(cand) <= _RISK_ORDER.index(allowed)


def output_satisfies(request_out: dict | None, candidate_out: dict | None) -> bool:
    """Candidate must produce every field the request requires, with compatible types."""
    if not request_out:
        return True
    required = request_out.get("required") or []
    if not required:
        return True
    if candidate_out is None:
        return False  # cannot prove the contract is met
    cand_props = candidate_out.get("properties") or {}
    req_props = request_out.get("properties") or {}
    for field in required:
        if field not in cand_props:
            return False
        if not _type_compatible(
            _schema_type(req_props.get(field)), _schema_type(cand_props.get(field))
        ):
            return False
    return True


def input_satisfiable(request_in: dict | None, candidate_in: dict | None) -> bool:
    """Candidate must not require inputs the request will not provide."""
    if candidate_in is None:
        return True
    cand_required = candidate_in.get("required") or []
    if not cand_required:
        return True
    if not request_in:
        return False  # candidate needs fields the request does not declare
    req_props = request_in.get("properties") or {}
    cand_props = candidate_in.get("properties") or {}
    for field in cand_required:
        if field not in req_props:
            return False
        if not _type_compatible(
            _schema_type(cand_props.get(field)), _schema_type(req_props.get(field))
        ):
            return False
    return True


def profile_subset(candidate_profile: str | None, requested_profile: str | None) -> bool:
    """Candidate's host functions must be a subset of the requested profile's."""
    if candidate_profile is None or requested_profile is None:
        return True
    return set(list_names_for_profile(candidate_profile)) <= set(
        list_names_for_profile(requested_profile)
    )


def is_compatible(
    result: SearchResult,
    *,
    request_input_schema: dict | None,
    request_output_schema: dict | None,
    requested_profile: str | None,
    allowed_risk: str | None,
    requested_type: str,
) -> bool:
    art = result.artifact
    ver = result.version
    if ver is None:
        return False

    at = art.artifact_type
    if not (requested_type in _SCRIPT_TYPES and at in _SCRIPT_TYPES) and at != requested_type:
        return False

    cand_profile = (ver.metadata_ or {}).get("runtime_profile")
    if not profile_subset(cand_profile, requested_profile):
        return False
    if not _risk_within(ver.risk_level, allowed_risk):
        return False
    if not output_satisfies(request_output_schema, ver.output_schema):
        return False
    return input_satisfiable(request_input_schema, ver.input_schema)
