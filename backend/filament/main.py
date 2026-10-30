"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from filament import __version__
from filament.api import config, graph, memory, runs
from filament.config import get_settings
from filament.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Filament", version=__version__, lifespan=lifespan,
                  description="Agentic software delivery platform with shared team memory.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])
    for router in (graph.router, memory.router, runs.router, config.router):
        app.include_router(router, prefix="/api")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "offline": settings.offline,
                "model": settings.model_default}

    return app


app = create_app()
