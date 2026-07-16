from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from literature_workbench.api.health import router as health_router
from literature_workbench.api.imports import router as imports_router
from literature_workbench.api.pdf_files import router as pdf_files_router
from literature_workbench.api.parsing import router as parsing_router
from literature_workbench.api.libraries import NativeFolderDialog
from literature_workbench.api.libraries import router as libraries_router
from literature_workbench.api.documents import router as documents_router
from literature_workbench.api.session import router as session_router
from literature_workbench.api.settings import router as settings_router
from literature_workbench.api.translations import router as translations_router
from literature_workbench.api.annotations import router as annotations_router
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.settings.service import SettingsService
from literature_workbench.features.egress.gateway import DeepSeekGateway
from literature_workbench.features.models.qwen_local import QwenLocalClient
from literature_workbench.features.models.qwen_process import QwenProcessManager
from literature_workbench.features.translation.repository import TranslationRepository
from literature_workbench.features.translation.service import TranslationService
from literature_workbench.features.annotations.service import PdfAnnotationService
from literature_workbench.features.parser.normalizer import ParseRunRepository
from literature_workbench.features.library.coordinator import LibraryScanCoordinator
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.roots import LibraryRootService
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.features.library.import_intents import ImportIntentRepository
from literature_workbench.features.library.import_service import ImportService, ImportValidator
from literature_workbench.features.library.import_files import MutexTimeout, TrustedImportRoot
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        trusted_root = TrustedImportRoot.create(resolved.data_root)
        supervisor_lock = trusted_root.mutex("api-supervisor")
        try:
            supervisor_lock.acquire(timeout=0)
        except MutexTimeout:
            supervisor_lock.close()
            trusted_root.close()
            raise RuntimeError("api_supervisor_already_running") from None
        try:
            database = Database.open(resolved.database_path)
            run_migrations(database)
            app.state.database = database
            app.state.trusted_import_root = trusted_root
            queue_clock = _utc_now
            task_queue = TaskQueue(database, queue_clock)
            app.state.queue_clock = queue_clock
            app.state.task_queue = task_queue
            app.state.parse_run_repository = ParseRunRepository(database)
            import_repository = ImportIntentRepository(database, queue_clock)
            import_repository.expire_pending()
            app.state.import_intent_repository = import_repository
            app.state.import_service = ImportService(
                database,
                import_repository,
                trusted_root,
                resolved_session.session_generation,
                queue=task_queue,
                now=queue_clock,
            )
            app.state.import_service.reconcile_stale_receiving()
            app.state.import_service.sweep_failed_residue()
            app.state.import_service.sweep_failed_promotions()
            deepseek_gateway = DeepSeekGateway(
                database,
                resolved_secret_store,
                process_generation=resolved_session.session_generation,
            )
            app.state.deepseek_gateway = deepseek_gateway
            app.state.settings_service = SettingsService(
                SettingsRepository(database),
                resolved_secret_store,
                deepseek_gateway,
            )
            app.state.translation_service = TranslationService(
                TranslationRepository(database, queue_clock),
                SettingsRepository(database),
                task_queue,
                deepseek_gateway,
                QwenLocalClient(),
                QwenProcessManager(resolved.qwen_runtime_dir),
            )
            app.state.annotation_service = PdfAnnotationService(
                database, resolved.data_root, task_queue
            )
            library_repository = LibraryRepository(database, task_queue)
            app.state.library_repository = library_repository
            app.state.library_root_service = LibraryRootService(
                library_repository, resolved_inspector
            )
            library_scan_coordinator = LibraryScanCoordinator()
            app.state.library_scan_coordinator = library_scan_coordinator
            app.state.library_scanner = LibraryScanner(
                database,
                library_repository,
                resolved_inspector,
                task_queue,
                coordinator=library_scan_coordinator,
            )
            app.state.import_validator = ImportValidator(
                database,
                task_queue,
                trusted_root,
                inspector=resolved_inspector,
            )
            if register_initial_library and library_repository.count() == 0:
                app.state.library_root_service.register_initial_approved_library()
            yield
        finally:
            supervisor_lock.release()
            supervisor_lock.close()
            trusted_root.close()

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
    app.include_router(translations_router)
    app.include_router(annotations_router)
    app.include_router(libraries_router)
    app.include_router(documents_router)
    app.include_router(pdf_files_router)
    app.include_router(parsing_router)
    app.include_router(imports_router)
    return app
