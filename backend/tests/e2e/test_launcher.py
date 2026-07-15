from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from fastapi.testclient import TestClient
from filelock import Timeout

import literature_workbench.launcher as launcher_module
from literature_workbench.config import AppSettings
from literature_workbench.launcher import (
    ProcessIdentity,
    RuntimeManifest,
    create_desktop_app,
    merge_worker_handlers,
    reuse_running_instance,
    start,
)
from literature_workbench.features.library.import_service import ImportValidator
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.features.parser.worker import ParseWorkerService
from literature_workbench.features.translation.service import TranslationService
from literature_workbench.platform.windows.credentials import KeyringBackend, SecretStore
from literature_workbench.security.local_session import LocalSession


@dataclass(slots=True)
class MemoryKeyring(KeyringBackend):
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def test_second_launch_validates_exact_instance_and_opens_fresh_fragment_token(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "launcher.json"
    manifest = RuntimeManifest(
        port=58380,
        supervisor=ProcessIdentity(101, 10.5, r"C:\Python\pythonw.exe"),
        worker=ProcessIdentity(202, 11.5, r"C:\Python\python.exe"),
        control_token="local-control-token-32-bytes-minimum",
    )
    manifest_path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    looked_up: list[int] = []
    opened: list[str] = []

    def lookup(pid: int) -> ProcessIdentity | None:
        looked_up.append(pid)
        return {101: manifest.supervisor, 202: manifest.worker}.get(pid)

    def health(url: str) -> bool:
        assert url == "http://127.0.0.1:58380/api/health"
        return True

    def control(port: int, token: str, action: str) -> str:
        assert (port, token, action) == (
            58380,
            "local-control-token-32-bytes-minimum",
            "bootstrap",
        )
        return "fresh-one-use-token"

    assert reuse_running_instance(
        manifest_path,
        identity_lookup=lookup,
        health_probe=health,
        control_request=control,
        browser_open=opened.append,
    )
    assert looked_up == [101, 202]
    assert opened == ["http://127.0.0.1:58380/bootstrap#fresh-one-use-token"]


def test_second_launch_refuses_stale_manifest_without_control_or_browser(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "launcher.json"
    manifest = RuntimeManifest(
        port=58380,
        supervisor=ProcessIdentity(101, 10.5, r"C:\Python\pythonw.exe"),
        worker=ProcessIdentity(202, 11.5, r"C:\Python\python.exe"),
        control_token="local-control-token-32-bytes-minimum",
    )
    manifest_path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    control_calls: list[object] = []
    opened: list[str] = []

    def stale_lookup(pid: int) -> ProcessIdentity | None:
        if pid == 101:
            return ProcessIdentity(pid, 999.0, manifest.supervisor.executable)
        return manifest.worker

    assert not reuse_running_instance(
        manifest_path,
        identity_lookup=stale_lookup,
        health_probe=lambda _: True,
        control_request=lambda *args: control_calls.append(args) or "unused",
        browser_open=opened.append,
    )
    assert control_calls == []
    assert opened == []


def test_launcher_app_serves_spa_and_protects_fresh_bootstrap_control(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "web-dist"
    distribution.mkdir()
    (distribution / "index.html").write_text("<main>workbench-spa</main>", encoding="utf-8")
    session = LocalSession()
    stopped = Event()
    app = create_desktop_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        session,
        "c" * 43,
        stopped,
        distribution,
        secret_store=SecretStore(MemoryKeyring()),
        register_initial_library=False,
    )

    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/bootstrap").text == "<main>workbench-spa</main>"
        assert client.get("/reader/deep/link").text == "<main>workbench-spa</main>"
        assert client.get("/api/not-a-route").status_code == 403
        assert client.post(_control_path("bootstrap"), headers={"Origin": "http://testserver"}).status_code == 403
        response = client.post(
            _control_path("bootstrap"),
            headers={"Origin": "http://testserver", "X-LW-Control": "c" * 43},
        )
        token = response.json()["token"]
        assert response.status_code == 200
        assert client.post(
            "/api/session/bootstrap",
            headers={"Origin": "http://testserver"},
            json={"token": token},
        ).status_code == 204
        assert client.post(
            "/api/session/bootstrap",
            headers={"Origin": "http://testserver"},
            json={"token": token},
        ).status_code == 403
        assert client.post(
            _control_path("stop"),
            headers={"Origin": "http://testserver", "X-LW-Control": "c" * 43},
        ).status_code == 204
        assert stopped.is_set()


def _control_path(action: str) -> str:
    return f"/__literature_workbench/{action}"


def test_contended_start_reuses_instance_without_starting_a_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ContendedLock:
        def acquire(self, timeout: int) -> None:
            assert timeout == 0
            raise Timeout("contended")

        def release(self) -> None:
            raise AssertionError("unowned lock must not be released")

    monkeypatch.setattr(launcher_module, "FileLock", lambda _: ContendedLock())
    monkeypatch.setattr(launcher_module, "reuse_running_instance", lambda _: True)
    monkeypatch.setattr(
        launcher_module,
        "run_supervisor",
        lambda *_: pytest.fail("second invocation must not spawn a supervisor or worker"),
    )

    start(AppSettings(data_root=tmp_path / "isolated-data"), tmp_path / "unused-dist")


def test_production_worker_handler_inventory_includes_translation() -> None:
    handlers = merge_worker_handlers(
        cast(LibraryScanner, object()),
        cast(ImportValidator, object()),
        cast(ParseWorkerService, object()),
        cast(TranslationService, object()),
    )

    assert set(handlers) == {
        "scan_library",
        "validate_import",
        "parse_pdf",
        "translate_selection",
    }
