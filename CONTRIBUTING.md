# Contributing to RiteSmith

## Development setup

**Requirements:** Python 3.12+, PostgreSQL 16+

```bash
# Clone and install
git clone https://github.com/your-org/ritesmith.git
cd ritesmith
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configure environment
cp deploy/.env.example .env
# Edit .env with your API keys and database URL

# Run migrations
alembic upgrade head

# Start the server
uvicorn ritesmith.api.app:app --reload
```

## Running tests

```bash
# Unit + integration tests (requires a running PostgreSQL)
export TEST_DATABASE_URL="postgresql+asyncpg://ritesmith:ritesmith@localhost:5432/ritesmith_test"
pytest -m "not e2e"

# With coverage
pytest -m "not e2e" --cov=ritesmith --cov-report=term-missing

# End-to-end tests (requires OpenAI API key)
pytest -m e2e
```

## Linting

```bash
ruff check .
ruff format --check .
```

## Branch strategy

- `main` — stable, always deployable
- feature branches → PR → main
- Branch names: `feat/short-description`, `fix/short-description`

## PR checklist

- [ ] `ruff check .` passes with no errors
- [ ] Tests pass (`pytest -m "not e2e"`)
- [ ] No secrets or personal paths in any tracked file
- [ ] New env vars added to `deploy/.env.example`
