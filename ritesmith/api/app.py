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
from ritesmith.api.mcp_handler import mcp_app
from ritesmith.api.routes import (
    admin,
    artifacts,
    capabilities,
    executions,
    generations,
    health,
    memory,
    plans,
    policies,
    providers,
    search,
    trama,
    validation,
)
from ritesmith.core.exceptions import RiteSmithError
from ritesmith.observability.metrics import http_request_duration, http_requests_total
from ritesmith.storage.postgres import get_db


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import logging
    import os

    from ritesmith.config import get_settings
    from ritesmith.core.provider_registration import register_all_providers
    from ritesmith.schemas.policy import PolicyDecisionValue

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("ritesmith").setLevel(logging.INFO)
    log = logging.getLogger(__name__)

    from ritesmith.runtime.luau import effective_script_language, luau_available

    settings = get_settings()
    if settings.script_language == "luau" and not luau_available():
        if settings.require_luau:
            raise RuntimeError(
                "RITESMITH_SCRIPT_LANGUAGE=luau and RITESMITH_REQUIRE_LUAU=true, but the "
                "lunardyson package is not installed. Refusing to start and silently fall "
                "back to the lua (lupa) sandbox, which has different isolation guarantees. "
                "Install lunardyson or set RITESMITH_REQUIRE_LUAU=false."
            )
        log.warning(
            "script_language=luau requested but lunardyson is missing; generating lua",
        )
    else:
        log.info("new scripts are generated in %s", effective_script_language(settings))

    # Validate policy_default at startup to catch misconfiguration early
    try:
        PolicyDecisionValue(get_settings().policy_default)
    except ValueError:
        raise RuntimeError(
            f"Invalid RITESMITH_POLICY_DEFAULT={get_settings().policy_default!r}. "
            f"Must be one of: {[v.value for v in PolicyDecisionValue]}"
        )
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
                1
                for r in results
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
    from ritesmith.llm.openai_provider import OpenAIProvider

    # Registry/health/execution remain available without an LLM credential.
    app.state.llm_provider = OpenAIProvider(settings, lazy_client=not os.getenv("OPENAI_API_KEY"))
    try:
        yield
    finally:
        await app.state.llm_provider.close()
        # Drain executors and DB pool even if the application exits with an error.
        log.info("shutdown: draining Lua sandbox executor")
        from ritesmith.runtime.sandbox import _EXECUTOR

        _EXECUTOR.shutdown(wait=True, cancel_futures=False)
        from ritesmith.runtime.luau import shutdown_executor

        shutdown_executor()
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
        import json
        import logging

        from ritesmith.observability.generation import GenerationTrace, current_trace
        from ritesmith.observability.metrics import (
            generation_duration,
            generation_requests_total,
            generation_target_requests_total,
        )

        start = time.perf_counter()
        endpoint = request.url.path
        is_generation = request.method == "POST" and (
            endpoint.startswith("/generate") or endpoint == "/plans"
        )
        entrypoint = (
            f"mcp:{endpoint}" if request.headers.get("X-Ritesmith-Interface") == "mcp" else endpoint
        )
        trace = GenerationTrace() if is_generation else None
        if trace is not None:
            request.state.generation_trace = trace
        token = current_trace.set(trace) if trace is not None else None
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            elapsed = time.perf_counter() - start
            http_request_duration.labels(method=request.method, endpoint=endpoint).observe(elapsed)
            http_requests_total.labels(
                method=request.method, endpoint=endpoint, status_code=status
            ).inc()
            if trace is not None:
                valid = trace.accepted and bool(trace.artifact_types) and int(status) < 400
                from ritesmith.config import get_settings

                target = get_settings().generation_latency_target_seconds
                target_result = (
                    "valid_within_target"
                    if valid and elapsed <= target
                    else "valid_over_target"
                    if valid
                    else "invalid"
                )
                result = (
                    "valid_under_5s"
                    if valid and elapsed < 5
                    else "valid_over_5s"
                    if valid
                    else "invalid"
                )
                for kind in trace.artifact_types or {"unknown"}:
                    generation_duration.labels(entrypoint=entrypoint, artifact_type=kind).observe(
                        elapsed
                    )
                    generation_requests_total.labels(
                        entrypoint=entrypoint, artifact_type=kind, outcome=result
                    ).inc()
                    generation_target_requests_total.labels(
                        entrypoint=entrypoint,
                        artifact_type=kind,
                        target_seconds=f"{target:g}",
                        outcome=target_result,
                    ).inc()
                from ritesmith.observability.costs import PRICE_VERSION, estimated_cost
                from ritesmith.observability.metrics import (
                    generation_estimated_cost_total,
                    generation_unknown_cost_total,
                )

                cost = estimated_cost(trace.calls)
                group = (
                    "compound"
                    if len(trace.artifact_types) > 1
                    else next(iter(trace.artifact_types), "unknown")
                )
                labels = {
                    "entrypoint": entrypoint,
                    "artifact_type": group,
                    "client_tests": str(trace.client_tests).lower(),
                    "dependencies": str(trace.has_dependencies).lower(),
                }

                if cost is None:
                    generation_unknown_cost_total.labels(**labels).inc()
                else:
                    generation_estimated_cost_total.labels(**labels).inc(cost)
                logging.getLogger(__name__).info(
                    "generation response duration=%.3fs outcome=%s trace=%s",
                    elapsed,
                    result,
                    json.dumps(
                        {
                            "calls": trace.calls,
                            "stages": trace.stages,
                            "fallback": trace.fallback,
                            "diagnostics": trace.diagnostics,
                            "estimated_usd": cost,
                            "price_version": PRICE_VERSION,
                            "client_tests": trace.client_tests,
                            "dependencies": trace.has_dependencies,
                            "entrypoint": entrypoint,
                        }
                    ),
                )
            if token is not None:
                current_trace.reset(token)


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
    app.include_router(search.router)
    app.include_router(trama.router)
    app.include_router(admin.router)

    # MCP sub-app mounted outside the middleware stack to avoid BaseHTTPMiddleware
    # conflicts with SSE streaming (which uses raw ASGI send).
    app.mount("/mcp", mcp_app)

    app.mount("/metrics", make_asgi_app())

    return app


app = create_app()
