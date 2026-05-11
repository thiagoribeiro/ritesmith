import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from ritesmith.api.limiter import limiter
from ritesmith.api.routes import admin, artifacts, capabilities, executions, generations, health, memory, plans, policies, providers, trama, validation
from ritesmith.core.exceptions import RiteSmithError
from ritesmith.observability.metrics import http_request_duration, http_requests_total
from ritesmith.storage.postgres import get_db


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from ritesmith.core.provider_registration import register_all_providers
    import logging
    log = logging.getLogger(__name__)
    try:
        async for db in get_db():
            await register_all_providers(db)
            break
    except Exception as e:
        log.warning("Provider registration failed at startup: %s", e)

    # CASP migration check — warn if persisted Lua artifacts still use home.*
    try:
        from ritesmith.registry.search import fts_search
        async for db in get_db():
            results = await fts_search(db, "home.", artifact_types=["lua_script"], limit=10)
            home_count = sum(
                1 for r in results
                if r.version and r.version.content and "home." in r.version.content
            )
            if home_count:
                log.warning(
                    "CASP MIGRATION: %d artifact(s) still use home.* — migrate to casp.*. "
                    "Check /admin/casp/migration-status for details.",
                    home_count,
                )
            break
    except Exception as e:
        log.debug("CASP migration check skipped: %s", e)
    yield
    # Graceful shutdown: drain Lua executor and release DB pool
    log.info("shutdown: draining Lua sandbox executor")
    from ritesmith.runtime.sandbox import _EXECUTOR
    _EXECUTOR.shutdown(wait=True, cancel_futures=False)
    log.info("shutdown: disposing DB connection pool")
    from ritesmith.storage.postgres import engine
    await engine.dispose()


class _RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


class _MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        method = request.method
        endpoint = request.url.path
        status = str(response.status_code)
        http_request_duration.labels(method=method, endpoint=endpoint).observe(elapsed)
        http_requests_total.labels(method=method, endpoint=endpoint, status_code=status).inc()
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title="RiteSmith API",
        version="0.1.0",
        description="AI-assisted capability and workflow generation control plane",
        lifespan=_lifespan,
    )

    @app.exception_handler(RiteSmithError)
    async def ritesmith_exception_handler(request: Request, exc: RiteSmithError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.error_code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(_MetricsMiddleware)
    app.add_middleware(_RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(artifacts.router)
    app.include_router(capabilities.router)
    app.include_router(memory.router)
    app.include_router(validation.router)
    app.include_router(generations.router)
    app.include_router(plans.router)
    app.include_router(executions.router)
    app.include_router(policies.router)
    app.include_router(providers.router)
    app.include_router(trama.router)
    app.include_router(admin.router)

    app.mount("/metrics", make_asgi_app())

    return app


app = create_app()
