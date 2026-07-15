from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from literature_workbench.api.health import router as health_router
from literature_workbench.api.libraries import NativeFolderDialog
from literature_workbench.api.libraries import router as libraries_router
from literature_workbench.api.session import router as session_router
from literature_workbench.api.settings import router as settings_router
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.settings.service import SettingsService
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.roots import LibraryRootService
from literature_workbench.platform.windows.folder_dialog import FolderDialog
from literature_workbench.platform.windows.filesystem import (
    SafeFileInspector,
    SafeWindowsFileInspector,
)
from literature_workbench.platform.windows.credentials import (
    SecretStore,
    create_windows_secret_store,
)
from literature_workbench.security.local_session import LocalApiSecurityMiddleware, LocalSession
from literature_workbench.security.origin_guard import OriginGuardMiddleware
from literature_workbench.security.selection_tokens import (
    InMemoryRootSelectionTokens,
    RootSelectionTokens,
)
from literature_workbench.security.validation_errors import install_validation_error_handler


def create_app(
    settings: AppSettings | None = None,
    *,
    local_session: LocalSession | None = None,
    secret_store: SecretStore | None = None,
    folder_dialog: NativeFolderDialog | None = None,
    file_inspector: SafeFileInspector | None = None,
    selection_tokens: RootSelectionTokens | None = None,
    register_initial_library: bool = True,
) -> FastAPI:
    resolved = settings or AppSettings()
    resolved_session = local_session or LocalSession()
    resolved_secret_store = secret_store or create_windows_secret_store()
    resolved_dialog = folder_dialog or FolderDialog()
    resolved_inspector = file_inspector or SafeWindowsFileInspector()
    resolved_selection_tokens = selection_tokens or InMemoryRootSelectionTokens()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database.open(resolved.database_path)
        run_migrations(database)
        app.state.database = database
        app.state.settings_service = SettingsService(
            SettingsRepository(database), resolved_secret_store
        )
        library_repository = LibraryRepository(database)
        app.state.library_root_service = LibraryRootService(
            library_repository, resolved_inspector
        )
        if register_initial_library and library_repository.count() == 0:
            app.state.library_root_service.register_initial_approved_library()
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
    app.state.folder_dialog = resolved_dialog
    app.state.file_inspector = resolved_inspector
    app.state.selection_tokens = resolved_selection_tokens
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(resolved.allowed_hosts))
    app.add_middleware(LocalApiSecurityMiddleware, session=resolved_session)
    app.add_middleware(OriginGuardMiddleware)
    install_validation_error_handler(app)
    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(settings_router)
    app.include_router(libraries_router)
    return app
