FROM python:3.12-slim

WORKDIR /app

# Install build deps for lupa (Lua C extension) and asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ liblua5.4-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY alembic.ini .
COPY ritesmith/ ritesmith/

CMD ["uvicorn", "ritesmith.api.app:app", "--host", "0.0.0.0", "--port", "8081"]
