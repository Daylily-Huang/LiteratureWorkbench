from __future__ import annotations

import json
import asyncio
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
    WorkerReady,
    build_argument_parser,
    consume_worker_bootstrap,
    create_desktop_app,
    merge_worker_handlers,
    recover_exact_stale_worker,
    reuse_running_instance,
    run_worker_until_parent_exit,
    start,
    stop_running_instance,
    wait_for_worker_ready,
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
        generation="test-generation",
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
        generation="test-generation",
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
        assert client.post(
            _control_path("bootstrap"),
            headers={"Origin": "http://testserver", "X-LW-Control": "c" * 43},
        ).status_code == 409


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


def test_public_cli_exposes_only_start_and_stop_and_rejects_data_root() -> None:
    help_text = build_argument_parser().format_help()

    assert "{start,stop}" in help_text
    assert "worker" not in help_text
    with pytest.raises(SystemExit):
        build_argument_parser().parse_args(["start", "--data-root", "D:\\other"])
    with pytest.raises(SystemExit):
        build_argument_parser().parse_args(["stop", "--data-root", "D:\\other"])


def test_internal_worker_without_runtime_capability_fails_before_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        launcher_module.Database,
        "open",
        lambda *_: pytest.fail("database must not open without a runtime capability"),
    )
    with pytest.raises(SystemExit, match="invalid internal worker capability"):
        launcher_module.main(["_worker"])
    with pytest.raises(RuntimeError, match="worker_bootstrap_missing"):
        consume_worker_bootstrap(
            tmp_path / "missing-bootstrap.json",
            generation="generation",
            token="untrusted-token",
            identity_lookup=lambda _: pytest.fail("parent lookup must follow capability read"),
        )


def test_worker_monitors_exact_parent_identity_and_exits_when_parent_dies() -> None:
    parent = ProcessIdentity(101, 10.5, r"C:\Python\pythonw.exe")

    class FakeWorker:
        stopped = False

        async def run_forever(self) -> None:
            while not self.stopped:
                await asyncio.sleep(0)

        def stop(self) -> None:
            self.stopped = True

    worker = FakeWorker()
    lookups = iter((parent, None))
    ready: list[bool] = []

    asyncio.run(
        run_worker_until_parent_exit(
            cast(object, worker),
            parent,
            identity_lookup=lambda _: next(lookups),
            signal_ready=lambda: ready.append(True),
            poll_interval=0,
        )
    )

    assert ready == [True]
    assert worker.stopped


def test_dead_supervisor_recovers_only_exact_manifest_worker(tmp_path: Path) -> None:
    manifest_path = tmp_path / "launcher.json"
    manifest = RuntimeManifest(
        port=58380,
        supervisor=ProcessIdentity(101, 10.5, r"C:\Python\pythonw.exe"),
        worker=ProcessIdentity(202, 11.5, r"C:\Python\python.exe"),
        control_token="c" * 43,
        generation="old-generation",
    )
    manifest_path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    stopped: list[int] = []

    assert recover_exact_stale_worker(
        manifest_path,
        identity_lookup=lambda pid: None if pid == 101 else manifest.worker,
        stopper=stopped.append,
        wait_for_exit=lambda identity, _: identity.pid == 202,
    )
    assert stopped == [202]

    manifest_path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    unknown = ProcessIdentity(202, 999.0, manifest.worker.executable)
    assert not recover_exact_stale_worker(
        manifest_path,
        identity_lookup=lambda pid: None if pid == 101 else unknown,
        stopper=stopped.append,
        wait_for_exit=lambda *_: True,
    )
    assert stopped == [202]


def test_worker_ready_requires_exact_generation_token_and_child_identity(
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / "worker-ready.json"
    worker = ProcessIdentity(202, 11.5, r"C:\Python\python.exe")
    WorkerReady("generation", "t" * 43, worker).write(ready_path)

    assert wait_for_worker_ready(
        ready_path,
        generation="generation",
        token="t" * 43,
        expected_worker=worker,
        child_alive=lambda: True,
        timeout_seconds=0.1,
    ) == worker

    WorkerReady("wrong-generation", "t" * 43, worker).write(ready_path)
    with pytest.raises(RuntimeError, match="worker_ready_timeout"):
        wait_for_worker_ready(
            ready_path,
            generation="generation",
            token="t" * 43,
            expected_worker=worker,
            child_alive=lambda: True,
            timeout_seconds=0.01,
        )


def test_stop_waits_for_exact_processes_manifest_and_locks_before_success(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "runtime" / "launcher.json"
    manifest_path.parent.mkdir()
    manifest = RuntimeManifest(
        port=58380,
        supervisor=ProcessIdentity(101, 10.5, r"C:\Python\pythonw.exe"),
        worker=ProcessIdentity(202, 11.5, r"C:\Python\python.exe"),
        control_token="c" * 43,
        generation="generation",
    )
    manifest_path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    stopped = False

    def control(*_: object) -> str:
        nonlocal stopped
        stopped = True
        manifest_path.unlink()
        return "stopping"

    def lookup(pid: int) -> ProcessIdentity | None:
        if stopped:
            return None
        return manifest.supervisor if pid == 101 else manifest.worker

    assert stop_running_instance(
        manifest_path,
        identity_lookup=lookup,
        health_probe=lambda _: True,
        control_request=control,
        locks_released=lambda _: stopped,
        timeout_seconds=0.1,
    )


def test_stop_fails_when_captured_owned_qwen_is_not_verified_gone(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "runtime" / "launcher" / "launcher.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = RuntimeManifest(
        port=58380,
        supervisor=ProcessIdentity(101, 10.5, r"C:\Python\pythonw.exe"),
        worker=ProcessIdentity(202, 11.5, r"C:\Python\python.exe"),
        control_token="c" * 43,
        generation="generation",
    )
    manifest_path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    stopped = False
    captured_qwen = object()
    manager_paths: list[Path] = []

    class QwenStillRunning:
        def owned_identity(self) -> object:
            return captured_qwen

        def owned_identity_absent(self, expected: object) -> bool:
            assert expected is captured_qwen
            return False

    def manager_factory(path: Path) -> QwenStillRunning:
        manager_paths.append(path)
        return QwenStillRunning()

    def control(*_: object) -> str:
        nonlocal stopped
        stopped = True
        manifest_path.unlink()
        return "stopping"

    assert not stop_running_instance(
        manifest_path,
        identity_lookup=lambda pid: None if stopped else (
            manifest.supervisor if pid == 101 else manifest.worker
        ),
        health_probe=lambda _: True,
        control_request=control,
        locks_released=lambda _: stopped,
        qwen_manager_factory=manager_factory,
        timeout_seconds=0.02,
    )
    assert manager_paths == [tmp_path / "runtime" / "qwen"]


def test_owned_qwen_cleanup_failure_is_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingQwenManager:
        def stop_owned(self) -> bool:
            raise launcher_module.QwenProcessError("private_qwen_failure")

    monkeypatch.setattr(
        launcher_module,
        "QwenProcessManager",
        lambda _: FailingQwenManager(),
    )

    with pytest.raises(RuntimeError) as caught:
        launcher_module._stop_owned_qwen(tmp_path / "qwen")

    assert str(caught.value) == "owned_qwen_cleanup_failed"
    assert "private_qwen_failure" not in str(caught.value)


def test_dying_instance_releases_lock_and_immediate_start_becomes_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DyingLock:
        attempts = 0

        def acquire(self, timeout: int) -> None:
            assert timeout == 0
            self.attempts += 1
            if self.attempts == 1:
                raise Timeout("dying")

        def release(self) -> None:
            return None

    lock = DyingLock()
    launched: list[bool] = []
    monkeypatch.setattr(launcher_module, "FileLock", lambda _: lock)
    monkeypatch.setattr(launcher_module, "reuse_running_instance", lambda _: False)
    monkeypatch.setattr(
        launcher_module, "run_supervisor", lambda *_: launched.append(True)
    )
    monkeypatch.setattr(launcher_module.time, "sleep", lambda _: None)

    start(AppSettings(data_root=tmp_path / "isolated-data"), tmp_path / "dist")

    assert lock.attempts == 2
    assert launched == [True]
