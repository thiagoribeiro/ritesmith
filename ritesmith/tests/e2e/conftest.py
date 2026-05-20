"""E2E test fixtures — real OpenAI LLM + test database.

Tests in this package require:
  - OPENAI_API_KEY set in environment or .env file
  - PostgreSQL accessible at TEST_DATABASE_URL (default: ritesmith:ritesmith@localhost/ritesmith_test)

Run only E2E tests:
  pytest -m e2e
Skip E2E tests (default unit/integration run):
  pytest -m "not e2e"
"""
import os

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ritesmith.api.app import create_app
from ritesmith.registry.models import Base
from ritesmith.storage.postgres import get_db

# Load .env so OPENAI_API_KEY is available even if not exported in the shell
load_dotenv()

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
    ON artifacts FOR EACH ROW
    EXECUTE FUNCTION artifacts_search_vector_update();
""")


def _api_key_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


# Applied to every test collected from this package
pytestmark = pytest.mark.e2e


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def e2e_db_engine():
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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def e2e_db_session(e2e_db_engine):
    """Session-scoped DB session — commits persist across tests (intentional for reuse tests)."""
    factory = async_sessionmaker(e2e_db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def e2e_client(e2e_db_session):
    """HTTP client backed by real OpenAI provider + test DB session."""
    if not _api_key_available():
        pytest.skip("OPENAI_API_KEY not set — skipping E2E tests")

    app = create_app()

    async def override_db():
        yield e2e_db_session

    # Override only the DB — LLM provider uses real OpenAI
    app.dependency_overrides[get_db] = override_db

    # Generous timeout: real LLM calls can take up to 60 s
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=120.0,
    ) as client:
        yield client
