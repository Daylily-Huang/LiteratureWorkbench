from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.roots import (
    INITIAL_APPROVED_LIBRARY,
    LibraryRootService,
)
from literature_workbench.platform.windows.folder_dialog import (
    FIXED_DIALOG_SCRIPT,
    CommandResult,
    FolderDialog,
    FolderDialogError,
)
from literature_workbench.platform.windows.filesystem import RootIdentity, UnsafePath
from literature_workbench.security.local_session import LocalSession
from literature_workbench.security.selection_tokens import InMemoryRootSelectionTokens


@dataclass(slots=True)
class FakeFolderDialog:
    result: Path | None = None
    error: bool = False
    calls: int = 0

    def select(self) -> Path | None:
        self.calls += 1
        if self.error:
            raise FolderDialogError
        return self.result


class FakeInspector:
    def __init__(self, *identities: RootIdentity | Exception) -> None:
        self.identities = deque(identities)
        self.inspected: list[Path] = []

    def inspect_root_directory(self, candidate: Path) -> RootIdentity:
        self.inspected.append(candidate)
        result = self.identities.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def inspect_under_root(self, candidate, root):  # pragma: no cover - protocol-only fake
        raise AssertionError("not used by root registration")

    def open_registered_pdf(self, document):  # pragma: no cover - protocol-only fake
        raise AssertionError("not used by root registration")


class FakeDialogRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str]) -> CommandResult:
        self.calls.append(arguments)
        return self.result


def _identity(path: Path, *, file_id: str = "00000000000000000000000000000022") -> RootIdentity:
    return RootIdentity(
        canonical_path=path,
        volume_id="0000000000000011",
        file_id=file_id,
    )


def _headers(session: LocalSession) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": session.csrf_token}


def _client(
    tmp_path: Path,
    session: LocalSession,
    dialog: FakeFolderDialog,
    inspector: FakeInspector,
) -> TestClient:
    return TestClient(
        create_app(
            AppSettings(data_root=tmp_path, allowed_hosts=("testserver",)),
            local_session=session,
            folder_dialog=dialog,
            file_inspector=inspector,
            selection_tokens=InMemoryRootSelectionTokens(),
            register_initial_library=False,
        ),
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    )


def test_root_registration_rejects_raw_browser_path(tmp_path: Path) -> None:
    session = LocalSession()
    with _client(tmp_path, session, FakeFolderDialog(), FakeInspector()) as client:
        response = client.post(
            "/api/libraries",
            headers=_headers(session),
            json={"path": r"C:\Users\Administrator", "recursive": True},
        )

    assert response.status_code == 422
    assert r"C:\Users\Administrator" not in response.text


def test_folder_dialog_uses_fixed_shell_false_argument_array_and_unicode_path() -> None:
    selected = r"C:\文献 库\论文"
    runner = FakeDialogRunner(CommandResult(returncode=0, stdout=selected + "\r\n", stderr=""))

    result = FolderDialog(runner).select()

    assert result == Path(selected)
    assert runner.calls == [["pwsh.exe", "-NoProfile", "-STA", "-Command", FIXED_DIALOG_SCRIPT]]


def test_folder_dialog_cancellation_returns_none_and_failure_is_closed() -> None:
    cancelled = FolderDialog(FakeDialogRunner(CommandResult(0, "", "")))
    assert cancelled.select() is None

    failed = FolderDialog(FakeDialogRunner(CommandResult(1, "", "private detail")))
    with pytest.raises(FolderDialogError, match="folder_dialog_failed") as exc_info:
        failed.select()
    assert "private detail" not in str(exc_info.value)


def test_select_root_cancellation_returns_204_without_token_or_root(tmp_path: Path) -> None:
    session = LocalSession()
    with _client(tmp_path, session, FakeFolderDialog(result=None), FakeInspector()) as client:
        response = client.post("/api/libraries/select-root", headers=_headers(session))
        count = LibraryRepository(client.app.state.database).count()

    assert response.status_code == 204
    assert count == 0


def test_select_root_dialog_failure_returns_stable_error_without_root(tmp_path: Path) -> None:
    session = LocalSession()
    with _client(
        tmp_path,
        session,
        FakeFolderDialog(error=True),
        FakeInspector(),
    ) as client:
        response = client.post("/api/libraries/select-root", headers=_headers(session))
        count = LibraryRepository(client.app.state.database).count()

    assert response.status_code == 503
    assert response.json() == {"code": "folder_dialog_failed"}
    assert count == 0


def test_select_then_register_uses_one_use_identity_token(tmp_path: Path) -> None:
    session = LocalSession()
    selected = Path(r"C:\文献 库")
    canonical = Path(r"C:\文献 库")
    identity = _identity(canonical)
    inspector = FakeInspector(identity, identity)
    with _client(tmp_path, session, FakeFolderDialog(selected), inspector) as client:
        selection = client.post("/api/libraries/select-root", headers=_headers(session))
        assert selection.status_code == 200
        assert selection.json().keys() == {"selection_token", "display_path", "expires_in_seconds"}
        assert selection.json()["display_path"] == str(canonical)

        registration = client.post(
            "/api/libraries",
            headers=_headers(session),
            json={"selection_token": selection.json()["selection_token"], "recursive": True},
        )
        replay = client.post(
            "/api/libraries",
            headers=_headers(session),
            json={"selection_token": selection.json()["selection_token"], "recursive": True},
        )

    assert registration.status_code == 201
    assert UUID(registration.json()["library_id"])
    assert replay.status_code == 403
    assert selection.json()["selection_token"] not in replay.text


@pytest.mark.parametrize("change", ["path", "volume", "file_id", "reparse"])
def test_registration_reinspects_identity_and_inserts_nothing_when_changed(
    tmp_path: Path,
    change: str,
) -> None:
    session = LocalSession()
    selected = Path(r"C:\library")
    original = _identity(selected)
    changed: RootIdentity | Exception
    if change == "path":
        changed = _identity(Path(r"C:\other"))
    elif change == "volume":
        changed = RootIdentity(selected, "0000000000000099", original.file_id)
    elif change == "file_id":
        changed = _identity(selected, file_id="00000000000000000000000000000099")
    else:
        changed = UnsafePath("reparse_point")
    inspector = FakeInspector(original, changed)
    with _client(tmp_path, session, FakeFolderDialog(selected), inspector) as client:
        selection = client.post("/api/libraries/select-root", headers=_headers(session))
        response = client.post(
            "/api/libraries",
            headers=_headers(session),
            json={"selection_token": selection.json()["selection_token"]},
        )
        count = LibraryRepository(client.app.state.database).count()

    assert response.status_code == 409
    assert response.json() == {"code": "selection_identity_changed"}
    assert count == 0


def test_exact_duplicate_returns_existing_id_and_overlapping_root_is_rejected(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "library.db")
    run_migrations(database)
    repository = LibraryRepository(database)
    parent = Path(r"C:\library")
    child = Path(r"C:\library\nested")
    parent_identity = _identity(parent)
    existing = repository.register(parent_identity, recursive=True, authorization_source="native_picker")

    duplicate = repository.register(
        parent_identity,
        recursive=True,
        authorization_source="native_picker",
    )
    assert duplicate.id == existing.id

    with pytest.raises(ValueError, match="overlapping_library_root"):
        repository.register(
            _identity(child, file_id="00000000000000000000000000000033"),
            recursive=True,
            authorization_source="native_picker",
        )


def test_same_path_with_different_registered_identity_fails_closed(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "library.db")
    run_migrations(database)
    repository = LibraryRepository(database)
    path = Path(r"C:\library")
    repository.register(
        _identity(path),
        recursive=True,
        authorization_source="native_picker",
    )

    with pytest.raises(ValueError, match="registered_root_identity_changed"):
        repository.register(
            _identity(path, file_id="00000000000000000000000000000099"),
            recursive=True,
            authorization_source="native_picker",
        )


def test_initial_approved_root_is_fixed_and_environment_override_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LW_INITIAL_LIBRARY_ROOT", str(tmp_path / "attacker"))
    database = Database.open(tmp_path / "library.db")
    run_migrations(database)
    inspector = FakeInspector(_identity(INITIAL_APPROVED_LIBRARY))
    service = LibraryRootService(LibraryRepository(database), inspector)

    library = service.register_initial_approved_library()

    assert inspector.inspected == [INITIAL_APPROVED_LIBRARY]
    assert library.canonical_path == INITIAL_APPROVED_LIBRARY
    assert library.authorization_source == "initial_user_approved"


def test_select_root_requires_central_session_and_csrf_policy(tmp_path: Path) -> None:
    session = LocalSession()
    selected = Path(r"C:\library")
    with _client(
        tmp_path,
        session,
        FakeFolderDialog(selected),
        FakeInspector(_identity(selected)),
    ) as client:
        missing_csrf = client.post(
            "/api/libraries/select-root",
            headers={"Origin": "http://testserver"},
        )
        client.cookies.clear()
        missing_session = client.post(
            "/api/libraries/select-root",
            headers=_headers(session),
        )

    assert missing_csrf.json() == {"code": "csrf_required"}
    assert missing_session.json() == {"code": "session_required"}
