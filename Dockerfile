FROM python:3.12-slim

WORKDIR /app

# Install build deps for lupa (Lua C extension) and asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ liblua5.4-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# LunarDyson (luau_script runtime) ships as a platform wheel with the native
# liblunardyson.so inside, published on its GitHub release for x86_64 and aarch64.
# A wheel dropped in deploy/wheels/ overrides the download (see deploy/wheels/README.md).
# Production sets RITESMITH_REQUIRE_LUAU=true, so an image without it refuses to start
# instead of falling back to the Lua (lupa) sandbox.
ARG LUNARDYSON_VERSION=0.1.2
COPY deploy/wheels/ /tmp/wheels/
RUN set -e; \
    if ls /tmp/wheels/lunardyson-*.whl >/dev/null 2>&1; then \
        pip install --no-cache-dir /tmp/wheels/lunardyson-*.whl; \
    else \
        arch="$(uname -m)"; \
        pip install --no-cache-dir \
          "https://github.com/thiagoribeiro/lunardyson/releases/download/v${LUNARDYSON_VERSION}/lunardyson-${LUNARDYSON_VERSION}-py3-none-linux_${arch}.whl"; \
    fi; \
    python -c "import lunardyson; print('lunardyson', lunardyson.version())"; \
    rm -rf /tmp/wheels

COPY alembic.ini .
COPY ritesmith/ ritesmith/

CMD ["uvicorn", "ritesmith.api.app:app", "--host", "0.0.0.0", "--port", "8081"]
