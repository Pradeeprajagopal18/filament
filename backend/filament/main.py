"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from filament import __version__
from filament.api import access, code, config, delivery, gateway, graph, memory, repos, runs
from filament.api.guard import guard
from filament.config import get_settings
from filament.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    import asyncio

    from filament.repos.service import scheduler_loop

    init_db()
    task = asyncio.create_task(scheduler_loop()) if get_settings().repo_scheduler_enabled \
        else None
    yield
    if task:
        task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.auth_mode not in {"none", "oidc"}:
        raise RuntimeError("FILAMENT_AUTH_MODE must be 'none' or 'oidc'")
    if settings.auth_mode == "oidc" and settings.session_secret in {"", "dev-only-change-me"}:
        raise RuntimeError("Set FILAMENT_SESSION_SECRET to a long random value when "
                           "FILAMENT_AUTH_MODE=oidc (it signs session cookies)")
    app = FastAPI(title="Filament", version=__version__, lifespan=lifespan,
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
