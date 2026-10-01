"""Server-issued approval tokens: only a token for the exact artifact version releases an execution."""

import pytest

from ritesmith.config import Settings
from ritesmith.core.approval import issue_approval_token, verify_approval_token

SCRIPT = "function run(input, context) return { ok = true } end"


def test_token_is_bound_to_artifact_and_version():
    settings = Settings(approval_secret="s3cret")
    token = issue_approval_token(settings, "art_1", 2)
    assert verify_approval_token(settings, "art_1", 2, token)
    assert not verify_approval_token(settings, "art_1", 3, token)
    assert not verify_approval_token(settings, "art_2", 2, token)
    assert not verify_approval_token(Settings(approval_secret="other"), "art_1", 2, token)


@pytest.mark.parametrize("token", [None, "", "approved", "x" * 64])
def test_made_up_tokens_are_rejected(token):
    assert not verify_approval_token(Settings(approval_secret="s3cret"), "art_1", 1, token)


async def _high_risk_artifact(client) -> str:
    resp = await client.post(
        "/artifacts",
        json={
            "name": "needs_approval",
            "artifact_type": "lua_script",
            "content": SCRIPT,
            "risk_level": "high",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["artifact_id"]


async def test_approved_execution_runs(client):
    artifact_id = await _high_risk_artifact(client)
    approval = await client.post(f"/artifacts/{artifact_id}/versions/1/approve")
    assert approval.status_code == 200, approval.text
    token = approval.json()["approval_token"]

    resp = await client.post(
        "/executions", json={"artifact_id": artifact_id, "input": {}, "approval_token": token}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "succeeded"


@pytest.mark.parametrize("token", ["approved", "anything-non-empty"])
async def test_arbitrary_token_no_longer_bypasses_approval(client, token):
    artifact_id = await _high_risk_artifact(client)
    resp = await client.post(
        "/executions", json={"artifact_id": artifact_id, "input": {}, "approval_token": token}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "waiting"


async def test_token_for_another_artifact_is_rejected(client):
    first = await _high_risk_artifact(client)
    second = await _high_risk_artifact(client)
    token = (await client.post(f"/artifacts/{first}/versions/1/approve")).json()["approval_token"]
    resp = await client.post(
        "/executions", json={"artifact_id": second, "input": {}, "approval_token": token}
    )
    assert resp.json()["status"] == "waiting"


async def test_approving_a_missing_version_is_404(client):
    artifact_id = await _high_risk_artifact(client)
    resp = await client.post(f"/artifacts/{artifact_id}/versions/99/approve")
    assert resp.status_code == 404
