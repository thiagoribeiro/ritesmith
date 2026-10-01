"""Meta-tests: the shared fixtures actually isolate tests from each other."""

from ritesmith.api.limiter import limiter
from ritesmith.core import execution
from ritesmith.tests.factories import unique

_NAME = unique("isolation")


async def test_a_commit_inside_a_test(client):
    resp = await client.post(
        "/artifacts",
        json={
            "name": _NAME,
            "artifact_type": "lua_script",
            "content": "function run() return {} end",
        },
    )
    assert resp.status_code == 201
    listing = await client.get("/artifacts")
    assert _NAME in {a["name"] for a in listing.json()["artifacts"]}
    execution._repair_in_flight.add("leaked")


async def test_b_does_not_see_the_previous_commit(client):
    listing = await client.get("/artifacts")
    assert _NAME not in {a["name"] for a in listing.json()["artifacts"]}


def test_c_module_caches_and_limiter_are_reset():
    assert "leaked" not in execution._repair_in_flight
    assert limiter.enabled is False
