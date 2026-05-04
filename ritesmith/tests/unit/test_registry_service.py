import hashlib

import pytest

from ritesmith.core.exceptions import InvalidTransitionError, NotFoundError
from ritesmith.registry.service import RegistryService


@pytest.mark.asyncio
async def test_create_artifact_defaults(db_session):
    svc = RegistryService(db_session)
    artifact, version = await svc.create_artifact(
        name="normalize_whitespace",
        artifact_type="lua_script",
        content="function run(input) return {result = input.text} end",
        description="Normalizes whitespace",
        tags=["text", "transform"],
    )

    assert artifact.artifact_id.startswith("art_")
    assert artifact.name == "normalize_whitespace"
    assert artifact.artifact_type == "lua_script"
    assert artifact.status == "draft"
    assert artifact.current_version == 1
    expected_hash = hashlib.sha256(b"function run(input) return {result = input.text} end").hexdigest()
    assert artifact.content_hash == expected_hash

    assert version.version == 1
    assert version.content == "function run(input) return {result = input.text} end"
    assert version.artifact_id == artifact.artifact_id


@pytest.mark.asyncio
async def test_get_artifact_with_version(db_session):
    svc = RegistryService(db_session)
    artifact, _ = await svc.create_artifact(
        name="test_art",
        artifact_type="lua_script",
        content="function run(i) return {} end",
    )

    pair = await svc.get_artifact_with_version(artifact.artifact_id)
    assert pair is not None
    found, ver = pair
    assert found.artifact_id == artifact.artifact_id
    assert ver.version == 1


@pytest.mark.asyncio
async def test_get_artifact_not_found(db_session):
    svc = RegistryService(db_session)
    result = await svc.get_artifact_with_version("art_nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_create_new_version(db_session):
    svc = RegistryService(db_session)
    artifact, v1 = await svc.create_artifact(
        name="my_script",
        artifact_type="lua_script",
        content="function run(i) return {v=1} end",
    )

    artifact2, v2 = await svc.create_new_version(
        artifact.artifact_id,
        "function run(i) return {v=2} end",
    )

    assert artifact2.current_version == 2
    assert v2.version == 2
    assert v2.content == "function run(i) return {v=2} end"

    versions = await svc.list_artifact_versions(artifact.artifact_id)
    assert len(versions) == 2
    assert versions[0].version == 1
    assert versions[1].version == 2


@pytest.mark.asyncio
async def test_create_new_version_not_found(db_session):
    svc = RegistryService(db_session)
    with pytest.raises(NotFoundError):
        await svc.create_new_version("art_ghost", "function run(i) return {} end")


@pytest.mark.asyncio
async def test_update_artifact_status_valid_transition(db_session):
    svc = RegistryService(db_session)
    artifact, _ = await svc.create_artifact(
        name="art",
        artifact_type="lua_script",
        content="function run(i) return {} end",
    )
    assert artifact.status == "draft"

    updated = await svc.update_artifact_status(artifact.artifact_id, "validated")
    assert updated.status == "validated"


@pytest.mark.asyncio
async def test_update_artifact_status_invalid_transition(db_session):
    svc = RegistryService(db_session)
    artifact, _ = await svc.create_artifact(
        name="art2",
        artifact_type="lua_script",
        content="function run(i) return {} end",
    )
    with pytest.raises(InvalidTransitionError) as exc_info:
        await svc.update_artifact_status(artifact.artifact_id, "active")
    assert "draft" in exc_info.value.message


@pytest.mark.asyncio
async def test_list_artifacts_filter_by_type(db_session):
    svc = RegistryService(db_session)
    await svc.create_artifact(name="a1", artifact_type="lua_script", content="function run(i) return {} end")
    await svc.create_artifact(name="a2", artifact_type="trama_workflow", content="{}")

    lua_arts = await svc.list_artifacts(artifact_type="lua_script")
    assert all(a.artifact_type == "lua_script" for a in lua_arts)

    wf_arts = await svc.list_artifacts(artifact_type="trama_workflow")
    assert all(a.artifact_type == "trama_workflow" for a in wf_arts)
