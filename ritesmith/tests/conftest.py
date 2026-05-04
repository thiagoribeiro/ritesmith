import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ritesmith.api.app import create_app
from ritesmith.registry.models import Base
from ritesmith.storage.postgres import get_db

TEST_DB_URL = "postgresql+asyncpg://nanochat:nanochat@localhost:5432/ritesmith_test"

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
async def db_session(db_engine):
    """Sessão por teste — rollback ao final para isolamento."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def client(db_session: AsyncSession):
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
