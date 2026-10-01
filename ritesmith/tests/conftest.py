"""Shared test fixtures.

Isolation: every test runs inside an outer transaction on a dedicated
connection, and the session joins it with SAVEPOINTs, so the explicit
`commit()` calls in the code under test are rolled back when the test ends.
Module-level caches, the rate limiter and the settings cache are reset
around every test.
"""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ritesmith.api.app import create_app
from ritesmith.api.deps import get_llm_provider
from ritesmith.config import Settings, get_settings
from ritesmith.registry.models import Base
from ritesmith.storage.postgres import get_db

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://ritesmith:ritesmith@localhost:5432/ritesmith_test",
)

_TRIGGER_FN = text("""
    CREATE OR REPLACE FUNCTION artifacts_search_vector_update()
    RETURNS trigger AS $$
    BEGIN
        NEW.search_vector :=
            setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(array_to_string(NEW.tags, ' '), '')), 'C');
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
""")

_DROP_TRIGGER = text("DROP TRIGGER IF EXISTS artifacts_search_vector_trigger ON artifacts;")

_CREATE_TRIGGER = text("""
    CREATE TRIGGER artifacts_search_vector_trigger
    BEFORE INSERT OR UPDATE OF name, description, tags
    ON artifacts
    FOR EACH ROW
    EXECUTE FUNCTION artifacts_search_vector_update();
""")


# ---------------------------------------------------------------------------
# Global state reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Rate limiter off, module caches and the settings cache cleared."""
    from ritesmith.api.limiter import limiter
    from ritesmith.core import execution
    from ritesmith.runtime.casp import client as casp_client
    from ritesmith.runtime.providers import loomharbor, market

    caches = [
        casp_client._discovery_cache,
        market._last_fetch,
        loomharbor._ready_cache,
        execution._repair_in_flight,
    ]
    limiter.enabled = False
    limiter.reset()
    for cache in caches:
        cache.clear()
    get_settings.cache_clear()
    yield
    limiter.enabled = True
    for cache in caches:
        cache.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(_TRIGGER_FN)
        await conn.execute(_DROP_TRIGGER)
        await conn.execute(_CREATE_TRIGGER)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_connection(db_engine):
    """A connection holding the test's outer transaction (always rolled back)."""
    async with db_engine.connect() as conn:
        outer = await conn.begin()
        yield conn
        if outer.is_active:
            await outer.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(db_connection) -> AsyncIterator[AsyncSession]:
    """Per-test session; commits become savepoints of the outer transaction."""
    session = AsyncSession(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def session_factory(db_connection, monkeypatch) -> Callable[[], AsyncSession]:
    """Routes `storage.postgres.AsyncSessionLocal` (used by background tasks) into the test transaction.

    Background sessions share the test connection, so only use it where those
    tasks are awaited sequentially (not concurrently with other DB work).
    """

    def factory() -> AsyncSession:
        return AsyncSession(
            bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    monkeypatch.setattr("ritesmith.storage.postgres.AsyncSessionLocal", factory)
    return factory


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------


@pytest.fixture
def make_client(db_session):
    """Factory for API clients: `async with make_client(llm=..., settings=...) as c:`.

    `settings` overrides the get_settings dependency; `llm` overrides get_llm_provider.
    """

    @asynccontextmanager
    async def factory(llm=None, settings: Settings | None = None) -> AsyncIterator[AsyncClient]:
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        if llm is not None:
            app.dependency_overrides[get_llm_provider] = lambda: llm
        if settings is not None:
            app.dependency_overrides[get_settings] = lambda: settings
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c

    return factory


@pytest_asyncio.fixture(loop_scope="session")
async def client(make_client):
    async with make_client() as c:
        yield c
