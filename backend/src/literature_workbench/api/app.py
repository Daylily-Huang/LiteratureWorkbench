from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from literature_workbench.api.health import router as health_router
from literature_workbench.api.session import router as session_router
from literature_workbench.api.settings import router as settings_router
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.settings.service import SettingsService
from literature_workbench.platform.windows.credentials import (
    SecretStore,
    create_windows_secret_store,
)
from literature_workbench.security.local_session import LocalApiSecurityMiddleware, LocalSession
from literature_workbench.security.origin_guard import OriginGuardMiddleware
from literature_workbench.security.validation_errors import install_validation_error_handler


def create_app(
    settings: AppSettings | None = None,
    *,
    local_session: LocalSession | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    resolved = settings or AppSettings()
    resolved_session = local_session or LocalSession()
    resolved_secret_store = secret_store or create_windows_secret_store()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database.open(resolved.database_path)
        run_migrations(database)
        app.state.database = database
        app.state.settings_service = SettingsService(
            SettingsRepository(database), resolved_secret_store
        )
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
    app.state.local_session = resolved_session
    app.state.secret_store = resolved_secret_store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(resolved.allowed_hosts))
    app.add_middleware(LocalApiSecurityMiddleware, session=resolved_session)
    app.add_middleware(OriginGuardMiddleware)
    install_validation_error_handler(app)
    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(settings_router)
    return app
