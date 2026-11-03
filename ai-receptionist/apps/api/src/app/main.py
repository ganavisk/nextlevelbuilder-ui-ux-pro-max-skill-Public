"""Application factory.

Wires cross-cutting concerns (config, logging, error handlers, CORS) and
mounts module routers. Modules register here and nowhere else — no module
imports another module's internals.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import models as _models  # noqa: F401 — registers every ORM model on Base.metadata
from app.core.config import Settings, get_settings
from app.core.db import check_database, ensure_rls_enforced
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry
from app.modules.conversation.providers import get_embedding_provider, get_llm_provider
from app.modules.scheduling.providers import get_calendar_provider

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    if settings.app_env in ("staging", "production"):
        await ensure_rls_enforced()
    app.state.redis = aioredis.from_url(settings.redis_url)
    # Provider clients are constructed once and reused — not per-request.
    # Tests override these on app.state after TestClient startup (fakes must
    # replace the real providers post-lifespan, or this assignment wins).
    app.state.llm_provider = get_llm_provider(settings)
    app.state.embedding_provider = get_embedding_provider(settings)
    app.state.calendar_provider = get_calendar_provider(settings)
    logger.info("startup", env=settings.app_env)
    yield
    await app.state.redis.aclose()
    logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env != "development")
    init_sentry(settings, service="api")

    app = FastAPI(
        title="AI Receptionist API",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    @app.middleware("http")
    async def harden_responses(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/api/"):
            # Tenant data and login responses must never sit in a shared cache.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": app.version}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict[str, str]:
        """Readiness: dependencies are reachable."""
        await check_database()
        await app.state.redis.ping()
        return {"status": "ready", "database": "ok", "redis": "ok"}

    # Module routers mount here as they land:
    from app.modules.telephony.routes import router as telephony_router

    app.include_router(telephony_router, prefix="/webhooks/voice", tags=["webhooks"])

    # Dashboard API (Phase 7). Each router owns its own auth: login is public
    # and oauth_router's /callback is state-token-authenticated (see its
    # module docstring — not a gap, its trust model is just different from
    # everything else here); every other route requires a bearer token via
    # core.auth dependencies. There is no blanket auth applied at this level,
    # so a route that forgets its dependency is a bug in that router, not
    # something this file can paper over by guessing which prefixes need
    # protecting.
    from app.modules.analytics.routes import router as analytics_router
    from app.modules.crm.routes import router as crm_router
    from app.modules.escalation.routes import router as callbacks_router
    from app.modules.scheduling.routes import router as appointments_router
    from app.modules.telephony.dashboard_routes import router as calls_router
    from app.modules.tenants.oauth_routes import router as oauth_router
    from app.modules.tenants.routes import router as tenants_router
    from app.modules.testlab.routes import router as testlab_router

    for dashboard_router in (
        tenants_router,
        oauth_router,
        calls_router,
        appointments_router,
        callbacks_router,
        analytics_router,
        crm_router,
        testlab_router,
    ):
        app.include_router(dashboard_router, prefix="/api/v1", tags=["dashboard"])

    return app


app = create_app()
