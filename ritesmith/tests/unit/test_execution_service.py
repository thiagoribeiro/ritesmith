"""Unit tests for ExecutionService — direct service layer, no HTTP stack."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ritesmith.config import get_settings
from ritesmith.core.exceptions import NotFoundError
from ritesmith.core.execution import ExecutionService
from ritesmith.runtime.base import RuntimeResult
from ritesmith.schemas.execution import CreateExecutionRequest, ExecutionStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact_pair(
    artifact_id="art-test",
    artifact_type="lua_script",
    risk_level="low",
    content="function run(i, c) return {ok=true} end",
):
    artifact = MagicMock()
    artifact.artifact_id = artifact_id
    artifact.artifact_type = artifact_type

    version = MagicMock()
    version.version = 1
    version.risk_level = risk_level
    version.content = content
    version.output_schema = None

    return artifact, version


def _make_service(db, registry_pair=None, registry_none=False):
    settings = get_settings()
    svc = ExecutionService(db=db, settings=settings)

    if registry_none:
        svc.registry.get_artifact_with_version = AsyncMock(return_value=None)
    elif registry_pair:
        svc.registry.get_artifact_with_version = AsyncMock(return_value=registry_pair)

    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lua_success(db_session):
    """Lua script runs successfully → status succeeded, output populated."""
    artifact, version = _make_artifact_pair()
    svc = _make_service(db_session, registry_pair=(artifact, version))

    success_result = RuntimeResult(output={"result": 42}, error=None, duration_ms=12)
    svc.lua_runtime.execute = AsyncMock(return_value=success_result)

    req = CreateExecutionRequest(artifact_id="art-test", input={"x": 1})
    result = await svc.create_execution(req)

    assert result.status == ExecutionStatus.succeeded
    assert result.output == {"result": 42}


@pytest.mark.asyncio
async def test_policy_deny(db_session):
    """High-risk artifact with deny policy → status rejected, runtime never called."""
    artifact, version = _make_artifact_pair(risk_level="critical")
    svc = _make_service(db_session, registry_pair=(artifact, version))
    svc.lua_runtime.execute = AsyncMock()

    req = CreateExecutionRequest(artifact_id="art-test")

    with patch.object(svc.policy, "evaluate") as mock_eval:
        from ritesmith.schemas.policy import PolicyDecision, PolicyDecisionValue

        mock_eval.return_value = PolicyDecision(
            decision=PolicyDecisionValue.deny, reason="risk too high"
        )
        result = await svc.create_execution(req)

    assert result.status == ExecutionStatus.rejected
    svc.lua_runtime.execute.assert_not_called()


@pytest.mark.asyncio
async def test_require_approval_without_token(db_session):
    """require_approval decision without token → status waiting, runtime not called."""
    artifact, version = _make_artifact_pair(risk_level="high")
    svc = _make_service(db_session, registry_pair=(artifact, version))
    svc.lua_runtime.execute = AsyncMock()

    req = CreateExecutionRequest(artifact_id="art-test", approval_token=None)

    with patch.object(svc.policy, "evaluate") as mock_eval:
        from ritesmith.schemas.policy import PolicyDecision, PolicyDecisionValue

        mock_eval.return_value = PolicyDecision(
            decision=PolicyDecisionValue.require_approval, reason="needs approval"
        )
        result = await svc.create_execution(req)

    assert result.status == ExecutionStatus.waiting
    svc.lua_runtime.execute.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_timeout(db_session):
    """Runtime timeout → status failed, error message mentions timeout."""
    artifact, version = _make_artifact_pair()
    svc = _make_service(db_session, registry_pair=(artifact, version))

    timeout_result = RuntimeResult(
        output={}, error="execution timed out", timed_out=True, duration_ms=5000
    )
    svc.lua_runtime.execute = AsyncMock(return_value=timeout_result)

    req = CreateExecutionRequest(artifact_id="art-test")
    result = await svc.create_execution(req)

    assert result.status == ExecutionStatus.failed
    assert result.error is not None
    assert "timed out" in (result.error.get("message") or "").lower()


@pytest.mark.asyncio
async def test_idempotency_key_returns_existing(db_session):
    """Second call with same idempotency key returns original result without re-running."""
    artifact, version = _make_artifact_pair()
    svc = _make_service(db_session, registry_pair=(artifact, version))

    success_result = RuntimeResult(output={"idempotent": True}, duration_ms=5)
    svc.lua_runtime.execute = AsyncMock(return_value=success_result)

    req = CreateExecutionRequest(artifact_id="art-test", idempotency_key="idem-key-001")
    first = await svc.create_execution(req)
    assert first.status == ExecutionStatus.succeeded

    # Second call: registry lookup won't be reached; original record returned
    svc.lua_runtime.execute.reset_mock()
    second = await svc.create_execution(req)

    assert second.execution_id == first.execution_id
    svc.lua_runtime.execute.assert_not_called()


@pytest.mark.asyncio
async def test_artifact_not_found(db_session):
    """Missing artifact → NotFoundError raised."""
    svc = _make_service(db_session, registry_none=True)

    req = CreateExecutionRequest(artifact_id="nonexistent-art")
    with pytest.raises(NotFoundError):
        await svc.create_execution(req)


@pytest.mark.asyncio
async def test_unhandled_runtime_exception_marks_failed(db_session):
    """Unhandled exception from runtime → status failed, exception not leaked."""
    artifact, version = _make_artifact_pair()
    svc = _make_service(db_session, registry_pair=(artifact, version))

    svc.lua_runtime.execute = AsyncMock(side_effect=RuntimeError("sandbox exploded"))

    req = CreateExecutionRequest(artifact_id="art-test")
    result = await svc.create_execution(req)

    assert result.status == ExecutionStatus.failed
    assert "sandbox exploded" in (result.error or {}).get("message", "")
