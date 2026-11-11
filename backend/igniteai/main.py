"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from igniteai import __version__
from igniteai.api import access, code, config, delivery, gateway, graph, memory, repos, runs
from igniteai.api.guard import guard
from igniteai.config import get_settings
from igniteai.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    import asyncio

    from igniteai.repos.service import scheduler_loop

    init_db()
    from igniteai.auth import bootstrap
    from igniteai.db import SessionLocal

    with SessionLocal() as session:
        bootstrap(session)
    task = asyncio.create_task(scheduler_loop()) if get_settings().repo_scheduler_enabled \
        else None
    yield
    if task:
        task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.auth_mode not in {"none", "login", "oidc"}:
        raise RuntimeError("IGNITE_AUTH_MODE must be 'login' or 'none'")
    bad = set(settings.auth_methods) - {"local", "ldap", "oidc"}
    if bad:
        raise RuntimeError(f"Unknown IGNITE_AUTH_METHODS {sorted(bad)}: use local, ldap, oidc")
    if settings.session_secret is not None and len(settings.session_secret) < 32:
        raise RuntimeError("IGNITE_SESSION_SECRET must be at least 32 characters (or unset it "
                           "and Ignite generates one)")
    app = FastAPI(title="Ignite", version=__version__, lifespan=lifespan,
                  description="Agentic software delivery platform with shared team memory.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"], allow_credentials=True)
    for router in (graph.router, memory.router, runs.router, config.router, repos.router,
                   gateway.router, code.router, delivery.router, access.router):
        # Every route goes through the access guard (see api/guard.py).
        app.include_router(router, prefix="/api", dependencies=[Depends(guard)])

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "offline": settings.offline,
                "model": settings.model_default}

    return app


app = create_app()
