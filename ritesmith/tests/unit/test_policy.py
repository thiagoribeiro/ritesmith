"""PolicyEngine decision table, the allow_unapproved_low_risk switch and the manual-artifact risk floor."""

import pytest

from ritesmith.config import Settings
from ritesmith.core.policy import PolicyEngine, effective_risk
from ritesmith.schemas.policy import PolicyDecisionValue, PolicyEvaluationRequest

ALLOW = PolicyDecisionValue.allow
DENY = PolicyDecisionValue.deny
APPROVE = PolicyDecisionValue.require_approval

SCRIPT_TYPES = ["lua_script", "luau_script", "trama_workflow"]


def _engine(**overrides) -> PolicyEngine:
    return PolicyEngine(Settings(**overrides))


def _decide(engine: PolicyEngine, **req) -> PolicyDecisionValue:
    return engine.evaluate(PolicyEvaluationRequest(**req)).decision


@pytest.mark.parametrize("artifact_type", SCRIPT_TYPES)
@pytest.mark.parametrize(
    ("risk", "expected"),
    [("low", ALLOW), ("medium", APPROVE), ("high", APPROVE), ("critical", DENY)],
)
def test_execute_by_risk(artifact_type, risk, expected):
    assert (
        _decide(_engine(), operation="execute", artifact_type=artifact_type, risk_level=risk)
        == expected
    )


@pytest.mark.parametrize("risk", ["low", "medium", "high", "critical", None])
def test_shell_scripts_are_never_executed(risk):
    assert (
        _decide(_engine(), operation="execute", artifact_type="shell_script", risk_level=risk)
        == DENY
    )


def test_missing_risk_defaults_to_low():
    assert _decide(_engine(), operation="execute", artifact_type="luau_script") == ALLOW


@pytest.mark.parametrize("artifact_type", SCRIPT_TYPES)
def test_low_risk_requires_approval_when_unapproved_execution_is_disabled(artifact_type):
    engine = _engine(allow_unapproved_low_risk=False)
    assert (
        _decide(engine, operation="execute", artifact_type=artifact_type, risk_level="low")
        == APPROVE
    )


def test_workflow_delegation_requires_approval():
    assert _decide(_engine(), operation="delegate", artifact_type="trama_workflow") == APPROVE


@pytest.mark.parametrize("default", ["deny", "allow", "require_approval"])
@pytest.mark.parametrize("operation", ["generate", "persist", "discover"])
def test_unmatched_operations_fall_back_to_policy_default(default, operation):
    engine = _engine(policy_default=default)
    assert _decide(engine, operation=operation, artifact_type="luau_script") == PolicyDecisionValue(
        default
    )


# ---------------------------------------------------------------------------
# Risk floor for manually created artifacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "profile", "manual", "expected"),
    [
        ("low", "transform_only", True, "low"),
        ("low", "readonly_network", True, "low"),
        ("low", "notification", True, "medium"),
        ("low", "sensitive_personal", True, "medium"),
        ("low", "filesystem_write", True, "medium"),
        ("low", "reporting", True, "medium"),
        ("low", "side_effects", True, "high"),
        ("low", "trusted_internal", True, "high"),
        ("medium", "trusted_internal", True, "high"),
        ("critical", "notification", True, "critical"),  # the floor never lowers risk
        ("low", "trusted_internal", False, "low"),  # generated artifacts keep declared risk
        ("low", None, True, "low"),
    ],
)
def test_effective_risk(declared, profile, manual, expected):
    assert effective_risk(declared, profile, manual) == expected


def test_manual_device_control_script_cannot_self_declare_low_risk():
    req = {
        "operation": "execute",
        "artifact_type": "lua_script",
        "risk_level": "low",
        "runtime_profile": "trusted_internal",
    }
    assert _decide(_engine(), **req, manual=True) == APPROVE
    assert _decide(_engine(), **req, manual=False) == ALLOW


async def test_manual_artifact_through_api_waits_for_approval(client):
    created = await client.post(
        "/artifacts",
        json={
            "name": "manual_trusted_low",
            "artifact_type": "lua_script",
            "content": "function run(input, context) return { ok = true } end",
            "risk_level": "low",
            "metadata": {"runtime_profile": "trusted_internal", "source": "generated"},
        },
    )
    assert created.status_code == 201, created.text
    artifact = created.json()
    assert artifact["metadata"]["source"] == "manual"  # the client cannot claim another origin

    execution = await client.post(
        "/executions", json={"artifact_id": artifact["artifact_id"], "input": {}}
    )
    assert execution.status_code == 201, execution.text
    assert execution.json()["status"] == "waiting"
