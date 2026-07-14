from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from literature_workbench.api.health import router as health_router
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database.open(resolved.database_path)
        run_migrations(database)
        app.state.database = database
        yield

    app = FastAPI(
        title="Literature Workbench",
        version=resolved.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.include_router(health_router)
    return app
