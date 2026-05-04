import pytest


@pytest.mark.asyncio
async def test_create_artifact_returns_201(client):
    response = await client.post("/artifacts", json={
        "name": "get_btc_price",
        "artifact_type": "lua_script",
        "content": "function run(input) return {price = 50000} end",
        "description": "Fetches BTC price",
        "tags": ["crypto", "market"],
    })
    assert response.status_code == 201
    body = response.json()
    assert body["artifact_id"].startswith("art_")
    assert body["name"] == "get_btc_price"
    assert body["artifact_type"] == "lua_script"
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["content"] == "function run(input) return {price = 50000} end"
    assert body["content_hash"] is not None


@pytest.mark.asyncio
async def test_get_artifact_by_id(client):
    create_resp = await client.post("/artifacts", json={
        "name": "my_artifact",
        "artifact_type": "lua_script",
        "content": "function run(i) return {} end",
    })
    artifact_id = create_resp.json()["artifact_id"]

    get_resp = await client.get(f"/artifacts/{artifact_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["artifact_id"] == artifact_id


@pytest.mark.asyncio
async def test_get_artifact_not_found(client):
    response = await client.get("/artifacts/art_doesnotexist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"


@pytest.mark.asyncio
async def test_list_artifact_versions(client):
    create_resp = await client.post("/artifacts", json={
        "name": "versioned_art",
        "artifact_type": "lua_script",
        "content": "function run(i) return {v=1} end",
    })
    artifact_id = create_resp.json()["artifact_id"]

    versions_resp = await client.get(f"/artifacts/{artifact_id}/versions")
    assert versions_resp.status_code == 200
    versions = versions_resp.json()
    assert len(versions) == 1
    assert versions[0]["version"] == 1


@pytest.mark.asyncio
async def test_list_artifacts_filter_by_type(client):
    await client.post("/artifacts", json={
        "name": "lua_filter_test",
        "artifact_type": "lua_script",
        "content": "function run(i) return {} end",
    })
    await client.post("/artifacts", json={
        "name": "wf_filter_test",
        "artifact_type": "trama_workflow",
        "content": "{}",
    })

    response = await client.get("/artifacts?artifact_type=lua_script")
    assert response.status_code == 200
    body = response.json()
    assert all(a["artifact_type"] == "lua_script" for a in body["artifacts"])


@pytest.mark.asyncio
async def test_get_capabilities_empty(client):
    response = await client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["capabilities"], list)


@pytest.mark.asyncio
async def test_memory_search_embeddings_returns_501(client):
    response = await client.post("/memory/search", json={
        "query": "normalize text",
        "search_mode": "embeddings",
    })
    assert response.status_code == 501


@pytest.mark.asyncio
async def test_memory_search_full_text(client):
    # Cria artifact com termo específico para busca
    await client.post("/artifacts", json={
        "name": "bitcoin_price_monitor",
        "artifact_type": "lua_script",
        "content": "function run(input) return {price = 50000} end",
        "description": "Monitors bitcoin cryptocurrency price drops",
        "tags": ["crypto", "bitcoin", "monitoring"],
    })

    response = await client.post("/memory/search", json={
        "query": "bitcoin price",
        "search_mode": "full_text",
        "limit": 5,
    })
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["results"], list)
    assert len(body["results"]) >= 1
    first = body["results"][0]
    assert first["score"] > 0
    assert first["artifact_id"].startswith("art_")


@pytest.mark.asyncio
async def test_memory_search_no_results(client):
    response = await client.post("/memory/search", json={
        "query": "zzzyyyxxx_nonexistent_term_999",
        "search_mode": "full_text",
    })
    assert response.status_code == 200
    assert response.json()["results"] == []
