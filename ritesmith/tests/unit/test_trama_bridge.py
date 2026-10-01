"""POST /trama/execute: bearer auth, capability dispatch, artifact dispatch, bad requests."""


from ritesmith.config import Settings
from ritesmith.tests.factories import make_artifact
from ritesmith.tests.fakes.llm import ScriptedLLM

TOKEN = "trama-secret"


def _settings(**kw) -> Settings:
    return Settings(trama_token=TOKEN, **kw)


async def test_missing_token_config_is_503(make_client):
    async with make_client(llm=ScriptedLLM(), settings=Settings(trama_token=None)) as client:
        resp = await client.post(
            "/trama/execute",
            json={"capability_name": "time.now_utc", "input": {}},
            headers={"Authorization": "Bearer anything"},
        )
    assert resp.status_code == 503


async def test_wrong_token_is_401(make_client):
    async with make_client(llm=ScriptedLLM(), settings=_settings()) as client:
        resp = await client.post(
            "/trama/execute",
            json={"capability_name": "time.now_utc", "input": {}},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


async def test_capability_dispatch(make_client):
    async with make_client(llm=ScriptedLLM(), settings=_settings()) as client:
        resp = await client.post(
            "/trama/execute",
            json={"capability_name": "text.slugify", "input": {"s": "Hello World"}},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["output"] == {"result": "hello-world"}


async def test_unknown_capability_is_404(make_client):
    async with make_client(llm=ScriptedLLM(), settings=_settings()) as client:
        resp = await client.post(
            "/trama/execute",
            json={"capability_name": "no.such_fn", "input": {}},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 404


async def test_neither_capability_nor_artifact_is_422(make_client):
    async with make_client(llm=ScriptedLLM(), settings=_settings()) as client:
        resp = await client.post(
            "/trama/execute",
            json={"input": {}},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 422


async def test_artifact_dispatch_runs_the_script(make_client, db_session):
    artifact, _ = await make_artifact(
        db_session,
        artifact_type="lua_script",
        content="function run(input, context) return { doubled = input.n * 2 } end",
    )
    async with make_client(llm=ScriptedLLM(), settings=_settings()) as client:
        resp = await client.post(
            "/trama/execute",
            json={"artifact_id": artifact.artifact_id, "input": {"n": 21}},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["output"] == {"doubled": 42}
