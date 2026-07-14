from fastapi import FastAPI

from literature_workbench.api.health import router as health_router
from literature_workbench.config import AppSettings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved = settings or AppSettings()
    app = FastAPI(
        title="Literature Workbench",
        version=resolved.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.include_router(health_router)
    return app
