"""Three-stage reuse: deterministic contract compat (core.compat) + check_reuse."""

from types import SimpleNamespace

import pytest

from ritesmith.config import get_settings
from ritesmith.core import compat
from ritesmith.core.reuse import check_reuse
from ritesmith.llm.base import LLMCallStats, LLMProvider
from ritesmith.registry.search import SearchResult
from ritesmith.tests.factories import make_artifact, unique

NUM_OUT = {"type": "object", "properties": {"result": {"type": "number"}}, "required": ["result"]}
STR_OUT = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
IN_VALUE = {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"]}


# ---------------------------------------------------------------------------
# Deterministic compatibility (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_out", "candidate_out", "ok"),
    [
        (None, None, True),  # request imposes no contract
        (NUM_OUT, NUM_OUT, True),
        (NUM_OUT, None, False),  # cannot prove a required contract is met
        (NUM_OUT, STR_OUT, False),  # type mismatch on required field
        (
            NUM_OUT,
            {"type": "object", "properties": {"other": {"type": "number"}}},
            False,  # missing required field
        ),
        (
            {
                "type": "object",
                "properties": {"result": {"type": "integer"}},
                "required": ["result"],
            },
            NUM_OUT,
            True,  # integer ⊆ number
        ),
    ],
)
def test_output_satisfies(request_out, candidate_out, ok):
    assert compat.output_satisfies(request_out, candidate_out) is ok


@pytest.mark.parametrize(
    ("request_in", "candidate_in", "ok"),
    [
        (IN_VALUE, None, True),  # candidate needs no input
        (IN_VALUE, IN_VALUE, True),
        (None, IN_VALUE, False),  # candidate needs fields the request won't provide
        (
            {"type": "object", "properties": {"other": {"type": "number"}}},
            IN_VALUE,
            False,  # required input not provided
        ),
    ],
)
def test_input_satisfiable(request_in, candidate_in, ok):
    assert compat.input_satisfiable(request_in, candidate_in) is ok


def test_profile_subset_is_by_host_function_set():
    # transform_only ⊆ readonly_network (network adds functions), not vice versa.
    assert compat.profile_subset("transform_only", "readonly_network")
    assert not compat.profile_subset("readonly_network", "transform_only")


def _result(artifact_type="lua_script", profile="transform_only", risk="low", out=None, inp=None):
    return SearchResult(
        artifact=SimpleNamespace(artifact_id="art_x", artifact_type=artifact_type),
        version=SimpleNamespace(
            metadata_={"runtime_profile": profile},
            risk_level=risk,
            output_schema=out,
            input_schema=inp,
        ),
        score=1.0,
    )


def test_is_compatible_cross_language_scripts_are_interchangeable():
    r = _result(artifact_type="lua_script", out=NUM_OUT)
    assert compat.is_compatible(
        r,
        request_input_schema=None,
        request_output_schema=NUM_OUT,
        requested_profile="transform_only",
        allowed_risk=None,
        requested_type="luau_script",
    )


def test_is_compatible_rejects_higher_profile_and_risk():
    hi = _result(profile="side_effects", risk="high", out=NUM_OUT)
    assert not compat.is_compatible(
        hi,
        request_input_schema=None,
        request_output_schema=NUM_OUT,
        requested_profile="transform_only",
        allowed_risk="low",
        requested_type="lua_script",
    )


# ---------------------------------------------------------------------------
# check_reuse end-to-end (recall → compat → judge)
# ---------------------------------------------------------------------------


class _JudgeLLM(LLMProvider):
    def __init__(self, choice):
        self._choice = choice
        self.calls = 0
        self.seen: list[dict] = []

    async def judge_reuse(self, intent, candidates):
        self.calls += 1
        self.seen = candidates
        return self._choice, LLMCallStats(model="mock")

    async def generate_lua(self, **kwargs):
        raise AssertionError("generation must not run on a reuse hit")

    async def repair_lua(self, **kwargs):
        raise AssertionError

    async def analyze_intent(self, **kwargs):
        raise AssertionError


async def test_reuse_returns_compatible_candidate_chosen_by_judge(db_session):
    token = unique("reuseintent")
    art, _ = await make_artifact(
        db_session,
        description=f"compute {token} doubled",
        usage_description=f"use when you need {token}",
        output_schema=NUM_OUT,
        input_schema=IN_VALUE,
    )
    await db_session.flush()
    llm = _JudgeLLM(choice=0)

    resp = await check_reuse(
        db_session,
        token,
        ["luau_script", "lua_script"],
        "prefer_reuse",
        input_schema=IN_VALUE,
        output_schema=NUM_OUT,
        runtime_profile="transform_only",
        llm=llm,
    )
    assert resp is not None
    assert resp.reused is True
    assert resp.source_artifact_id == art.artifact_id
    assert llm.calls == 1


async def test_reuse_rejects_contract_incompatible_candidate_before_judge(db_session):
    # Recalled by FTS, but its output is a string where the request requires a number:
    # the deterministic filter drops it, the judge is never consulted.
    token = unique("reuseintent")
    await make_artifact(
        db_session,
        description=f"compute {token} as text",
        output_schema=STR_OUT,
        input_schema=IN_VALUE,
    )
    await db_session.flush()
    llm = _JudgeLLM(choice=0)

    resp = await check_reuse(
        db_session,
        token,
        ["luau_script", "lua_script"],
        "prefer_reuse",
        input_schema=IN_VALUE,
        output_schema=NUM_OUT,
        runtime_profile="transform_only",
        llm=llm,
    )
    assert resp is None
    assert llm.calls == 0


async def test_reuse_judge_can_decline_compatible_candidate(db_session):
    token = unique("reuseintent")
    await make_artifact(
        db_session,
        description=f"compute {token} doubled",
        output_schema=NUM_OUT,
        input_schema=IN_VALUE,
    )
    await db_session.flush()
    llm = _JudgeLLM(choice=None)

    resp = await check_reuse(
        db_session,
        token,
        ["luau_script", "lua_script"],
        "prefer_reuse",
        input_schema=IN_VALUE,
        output_schema=NUM_OUT,
        runtime_profile="transform_only",
        llm=llm,
    )
    assert resp is None
    assert llm.calls == 1


async def test_reuse_deterministic_mode_skips_judge(db_session):
    token = unique("reuseintent")
    art, _ = await make_artifact(
        db_session,
        description=f"compute {token} doubled",
        output_schema=NUM_OUT,
        input_schema=IN_VALUE,
    )
    await db_session.flush()
    llm = _JudgeLLM(choice=0)
    settings = get_settings().model_copy(update={"reuse_llm_judge": False})

    resp = await check_reuse(
        db_session,
        token,
        ["luau_script", "lua_script"],
        "prefer_reuse",
        input_schema=IN_VALUE,
        output_schema=NUM_OUT,
        runtime_profile="transform_only",
        llm=llm,
        settings=settings,
    )
    assert resp is not None
    assert resp.source_artifact_id == art.artifact_id
    assert llm.calls == 0


async def test_reuse_force_new_returns_none(db_session):
    resp = await check_reuse(db_session, "anything", ["lua_script"], "force_new")
    assert resp is None
