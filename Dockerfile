FROM python:3.12-slim

WORKDIR /app

# Install build deps for lupa (Lua C extension) and asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ liblua5.4-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# LunarDyson (luau_script runtime) ships as a prebuilt platform wheel with the native
# liblunardyson.so inside — see deploy/wheels/README.md. Without it, RiteSmith falls
# back to generating Lua (lupa) and logs a warning at startup.
COPY deploy/wheels/ /tmp/wheels/
RUN if ls /tmp/wheels/*.whl >/dev/null 2>&1; then \
        pip install --no-cache-dir /tmp/wheels/*.whl; \
    fi && rm -rf /tmp/wheels

COPY alembic.ini .
COPY ritesmith/ ritesmith/

CMD ["uvicorn", "ritesmith.api.app:app", "--host", "0.0.0.0", "--port", "8081"]
