from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import cast

import httpx
import psutil  # type: ignore[import-untyped]
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout
from starlette.staticfiles import StaticFiles

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.egress.gateway import DeepSeekGateway
from literature_workbench.features.library.coordinator import LibraryScanCoordinator
from literature_workbench.features.library.import_files import TrustedImportRoot
from literature_workbench.features.library.import_service import ImportValidator
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.features.models.qwen_local import QwenLocalClient
from literature_workbench.features.models.qwen_process import QwenProcessError, QwenProcessManager
from literature_workbench.features.parser.worker import ParseWorkerService
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.translation.repository import TranslationRepository
from literature_workbench.features.translation.service import TranslationService
from literature_workbench.platform.windows.acl import TrustedRuntimeRoot, secure_owner_directory
from literature_workbench.platform.windows.credentials import SecretStore, create_windows_secret_store
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.security.local_session import LocalSession
from literature_workbench.worker import (
    JobHandler,
    Worker,
    WorkerPool,
    library_job_handlers,
    parser_job_handlers,
    translation_job_handlers,
)


DEFAULT_PORT = 58380
_CONTROL_BOOTSTRAP = "/__literature_workbench/bootstrap"
_CONTROL_STOP = "/__literature_workbench/stop"
_MANIFEST_NAME = "launcher.json"
_LOCK_NAME = "launcher.lock"
_WORKER_LOCK_NAME = "worker.lock"
_WORKER_BOOTSTRAP_NAME = "worker-bootstrap.json"
_WORKER_READY_NAME = "worker-ready.json"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time: float
    executable: str

    def to_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "create_time": self.create_time,
            "executable": self.executable,
        }

    @classmethod
    def from_json(cls, value: object) -> ProcessIdentity:
        if not isinstance(value, dict):
            raise ValueError("launcher_manifest_invalid")
        raw = cast(dict[str, object], value)
        pid = raw.get("pid")
        create_time = raw.get("create_time")
        executable = raw.get("executable")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(create_time, (int, float))
            or isinstance(create_time, bool)
            or not isinstance(executable, str)
            or not executable
        ):
            raise ValueError("launcher_manifest_invalid")
        return cls(pid, float(create_time), executable)


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    port: int
    supervisor: ProcessIdentity
    worker: ProcessIdentity
    control_token: str
    generation: str

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "host": "127.0.0.1",
            "port": self.port,
            "supervisor": self.supervisor.to_json(),
            "worker": self.worker.to_json(),
            "control_token": self.control_token,
            "generation": self.generation,
        }

    @classmethod
    def from_json(cls, value: object) -> RuntimeManifest:
        if not isinstance(value, dict):
            raise ValueError("launcher_manifest_invalid")
        raw = cast(dict[str, object], value)
        port = raw.get("port")
        token = raw.get("control_token")
        schema_version = raw.get("schema_version")
        generation = raw.get("generation")
        if schema_version == 1 and generation is None:
            generation = "legacy-worker-generation"
        if (
            schema_version not in {1, 2}
            or raw.get("host") != "127.0.0.1"
            or not isinstance(port, int)
            or isinstance(port, bool)
            or port < 1
            or port > 65535
            or not isinstance(token, str)
            or len(token) < 32
            or not isinstance(generation, str)
            or not generation
        ):
            raise ValueError("launcher_manifest_invalid")
        return cls(
            port=port,
            supervisor=ProcessIdentity.from_json(raw.get("supervisor")),
            worker=ProcessIdentity.from_json(raw.get("worker")),
            control_token=token,
            generation=generation,
        )


@dataclass(frozen=True, slots=True)
class WorkerBootstrap:
    generation: str
    token: str
    parent: ProcessIdentity
    session_generation: str

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "generation": self.generation,
            "token": self.token,
            "parent": self.parent.to_json(),
            "session_generation": self.session_generation,
        }

    def write(self, path: Path) -> None:
        _write_json_atomic(path, self.to_json())

    @classmethod
    def from_json(cls, value: object) -> WorkerBootstrap:
        if not isinstance(value, dict):
            raise ValueError("worker_bootstrap_invalid")
        raw = cast(dict[str, object], value)
        generation = raw.get("generation")
        token = raw.get("token")
        session_generation = raw.get("session_generation")
        if (
            raw.get("schema_version") != 2
            or not isinstance(generation, str)
            or not generation
            or not isinstance(token, str)
            or len(token) < 32
            or not isinstance(session_generation, str)
            or not session_generation
        ):
            raise ValueError("worker_bootstrap_invalid")
        return cls(
            generation,
            token,
            ProcessIdentity.from_json(raw.get("parent")),
            session_generation,
        )


@dataclass(frozen=True, slots=True)
class WorkerReady:
    generation: str
    token: str
    worker: ProcessIdentity

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generation": self.generation,
            "token": self.token,
            "worker": self.worker.to_json(),
        }

    def write(self, path: Path) -> None:
        _write_json_atomic(path, self.to_json())

    @classmethod
    def from_json(cls, value: object) -> WorkerReady:
        if not isinstance(value, dict):
            raise ValueError("worker_ready_invalid")
        raw = cast(dict[str, object], value)
        generation = raw.get("generation")
        token = raw.get("token")
        if (
            raw.get("schema_version") != 1
            or not isinstance(generation, str)
            or not generation
            or not isinstance(token, str)
            or len(token) < 32
        ):
            raise ValueError("worker_ready_invalid")
        return cls(generation, token, ProcessIdentity.from_json(raw.get("worker")))


IdentityLookup = Callable[[int], ProcessIdentity | None]
ParentLookup = Callable[[int], int | None]
CommandLineLookup = Callable[[int], tuple[str, ...] | None]
HealthProbe = Callable[[str], bool]
ControlRequest = Callable[[int, str, str], str]
BrowserOpen = Callable[[str], object]
WaitForExit = Callable[[ProcessIdentity, float], bool]


def _same_executable(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _same_identity(expected: ProcessIdentity, actual: ProcessIdentity | None) -> bool:
    return (
        actual is not None
        and actual.pid == expected.pid
        and actual.create_time == expected.create_time
        and _same_executable(actual.executable, expected.executable)
    )


def lookup_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        process = psutil.Process(pid)
        return ProcessIdentity(process.pid, process.create_time(), process.exe())
    except (psutil.Error, OSError):
        return None


def lookup_parent_pid(pid: int) -> int | None:
    try:
        return int(psutil.Process(pid).ppid())
    except (psutil.Error, OSError):
        return None


def lookup_command_line(pid: int) -> tuple[str, ...] | None:
    try:
        return tuple(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError):
        return None


def _read_manifest(path: Path) -> RuntimeManifest:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("launcher_manifest_invalid") from None
    return RuntimeManifest.from_json(raw)


def _write_manifest(path: Path, manifest: RuntimeManifest) -> None:
    _write_json_atomic(path, manifest.to_json())


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, missing_code: str, invalid_code: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(missing_code) from None
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(invalid_code) from None


def consume_worker_bootstrap(
    path: Path,
    *,
    generation: str,
    token: str,
    identity_lookup: IdentityLookup = lookup_process_identity,
) -> WorkerBootstrap:
    loaded = _read_json(path, "worker_bootstrap_missing", "worker_bootstrap_invalid")
    try:
        bootstrap = WorkerBootstrap.from_json(loaded)
    except ValueError:
        raise RuntimeError("worker_bootstrap_invalid") from None
    if not hmac.compare_digest(bootstrap.generation, generation) or not hmac.compare_digest(
        bootstrap.token, token
    ):
        raise RuntimeError("worker_bootstrap_capability_invalid")
    if not _same_identity(bootstrap.parent, identity_lookup(bootstrap.parent.pid)):
        raise RuntimeError("worker_parent_invalid")
    try:
        path.unlink()
    except OSError:
        raise RuntimeError("worker_bootstrap_consume_failed") from None
    return bootstrap


def _health_probe(url: str) -> bool:
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=1.0) as client:
            response = client.get(url)
        return response.status_code == 200 and response.json() == {
            "status": "ok",
            "version": "0.1.0",
            "network_scope": "loopback-only",
        }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _control_request(port: int, token: str, action: str) -> str:
    if action not in {"bootstrap", "stop"}:
        raise ValueError("launcher_control_action_invalid")
    origin = f"http://127.0.0.1:{port}"
    path = _CONTROL_BOOTSTRAP if action == "bootstrap" else _CONTROL_STOP
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=3.0) as client:
        response = client.post(
            f"{origin}{path}",
            headers={"Origin": origin, "X-LW-Control": token},
        )
        response.raise_for_status()
        if action == "stop":
            return "stopping"
        payload: object = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
        raise ValueError("launcher_control_response_invalid")
    return cast(str, payload["token"])


def reuse_running_instance(
    manifest_path: Path,
    *,
    identity_lookup: IdentityLookup = lookup_process_identity,
    health_probe: HealthProbe = _health_probe,
    control_request: ControlRequest = _control_request,
    browser_open: BrowserOpen = webbrowser.open,
) -> bool:
    try:
        manifest = _read_manifest(manifest_path)
        supervisor = identity_lookup(manifest.supervisor.pid)
        worker = identity_lookup(manifest.worker.pid)
        if not _same_identity(manifest.supervisor, supervisor) or not _same_identity(
            manifest.worker, worker
        ):
            return False
        if not health_probe(f"http://127.0.0.1:{manifest.port}/api/health"):
            return False
        bootstrap_token = control_request(
            manifest.port, manifest.control_token, "bootstrap"
        )
        browser_open(
            f"http://127.0.0.1:{manifest.port}/bootstrap#{bootstrap_token}"
        )
        return True
    except (OSError, ValueError, httpx.HTTPError):
        return False


def stop_running_instance(
    manifest_path: Path,
    *,
    identity_lookup: IdentityLookup = lookup_process_identity,
    health_probe: HealthProbe = _health_probe,
    control_request: ControlRequest = _control_request,
    locks_released: Callable[[Path], bool] | None = None,
    qwen_manager_factory: Callable[[Path], QwenProcessManager] = QwenProcessManager,
    exact_stopper: Callable[[ProcessIdentity], None] | None = None,
    timeout_seconds: float = 60.0,
) -> bool:
    try:
        manifest = _read_manifest(manifest_path)
        if not _same_identity(
            manifest.supervisor, identity_lookup(manifest.supervisor.pid)
        ):
            return False
        qwen_manager = qwen_manager_factory(manifest_path.parent.parent / "qwen")
        healthy = health_probe(f"http://127.0.0.1:{manifest.port}/api/health")
        forced = not healthy
        if healthy:
            control_request(manifest.port, manifest.control_token, "stop")
            owned_qwen = qwen_manager.owned_identity()
        else:
            stop_exact = exact_stopper or _stop_exact_identity
            if _same_identity(
                manifest.worker, identity_lookup(manifest.worker.pid)
            ):
                stop_exact(manifest.worker)
            stop_exact(manifest.supervisor)
            owned_qwen = qwen_manager.owned_identity()
            qwen_manager.stop_owned()
        lock_probe = locks_released or _runtime_locks_released
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            supervisor_gone = not _same_identity(
                manifest.supervisor, identity_lookup(manifest.supervisor.pid)
            )
            worker_gone = not _same_identity(
                manifest.worker, identity_lookup(manifest.worker.pid)
            )
            qwen_gone = qwen_manager.owned_identity_absent(owned_qwen)
            locks_gone = lock_probe(manifest_path.parent)
            if forced and supervisor_gone and worker_gone and locks_gone and qwen_gone:
                manifest_path.unlink(missing_ok=True)
            if (
                supervisor_gone
                and worker_gone
                and not manifest_path.exists()
                and locks_gone
                and qwen_gone
            ):
                return True
            time.sleep(0.05)
        return False
    except (OSError, ValueError, httpx.HTTPError, QwenProcessError):
        return False


def _runtime_locks_released(runtime_dir: Path) -> bool:
    acquired: list[FileLock] = []
    try:
        for name in (_LOCK_NAME, _WORKER_LOCK_NAME):
            lock = FileLock(runtime_dir / name)
            lock.acquire(timeout=0)
            acquired.append(lock)
        return True
    except Timeout:
        return False
    finally:
        for lock in reversed(acquired):
            lock.release()


def _wait_for_process_exit(identity: ProcessIdentity, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _same_identity(identity, lookup_process_identity(identity.pid)):
            return True
        time.sleep(0.05)
    return not _same_identity(identity, lookup_process_identity(identity.pid))


def _terminate_pid(pid: int) -> None:
    psutil.Process(pid).terminate()


def recover_exact_stale_worker(
    manifest_path: Path,
    *,
    identity_lookup: IdentityLookup = lookup_process_identity,
    stopper: Callable[[int], None] = _terminate_pid,
    wait_for_exit: WaitForExit = _wait_for_process_exit,
) -> bool:
    try:
        manifest = _read_manifest(manifest_path)
    except ValueError:
        return False
    if _same_identity(manifest.supervisor, identity_lookup(manifest.supervisor.pid)):
        return False
    actual_worker = identity_lookup(manifest.worker.pid)
    if not _same_identity(manifest.worker, actual_worker):
        manifest_path.unlink(missing_ok=True)
        return False
    try:
        stopper(manifest.worker.pid)
    except (OSError, psutil.Error):
        return False
    if not wait_for_exit(manifest.worker, 10.0):
        return False
    manifest_path.unlink(missing_ok=True)
    return True


def merge_worker_handlers(
    scanner: LibraryScanner,
    import_validator: ImportValidator,
    parser: ParseWorkerService,
    translation: TranslationService,
) -> Mapping[str, JobHandler]:
    handlers: dict[str, JobHandler] = {}
    for group in (
        library_job_handlers(scanner, import_validator),
        parser_job_handlers(parser),
        translation_job_handlers(translation),
    ):
        overlap = handlers.keys() & group.keys()
        if overlap:
            raise RuntimeError("duplicate_worker_handler")
        handlers.update(group)
    return handlers


def partition_worker_handlers(
    scanner: LibraryScanner,
    import_validator: ImportValidator,
    parser: ParseWorkerService,
    translation: TranslationService,
) -> tuple[Mapping[str, JobHandler], Mapping[str, JobHandler]]:
    all_handlers = merge_worker_handlers(scanner, import_validator, parser, translation)
    interactive = {
        kind: handler for kind, handler in all_handlers.items()
        if kind == "translate_selection"
    }
    background = {
        kind: handler for kind, handler in all_handlers.items()
        if kind != "translate_selection"
    }
    return background, interactive


def create_desktop_app(
    settings: AppSettings,
    session: LocalSession,
    control_token: str,
    stop_event: Event,
    web_dist: Path,
    *,
    secret_store: SecretStore | None = None,
    register_initial_library: bool = True,
) -> FastAPI:
    distribution = web_dist.resolve(strict=True)
    index = distribution / "index.html"
    if not index.is_file():
        raise FileNotFoundError("web_dist_missing")
    app = create_app(
        settings,
        local_session=session,
        secret_store=secret_store,
        register_initial_library=register_initial_library,
    )

    def require_control(candidate: str | None) -> None:
        if candidate is None or not hmac.compare_digest(candidate, control_token):
            raise HTTPException(status_code=403, detail="control_required")

    @app.post(_CONTROL_BOOTSTRAP, include_in_schema=False)
    def launcher_bootstrap(x_lw_control: str | None = Header(default=None)) -> dict[str, str]:
        require_control(x_lw_control)
        if stop_event.is_set():
            raise HTTPException(status_code=409, detail="launcher_stopping")
        return {"token": session.issue_bootstrap_token()}

    @app.post(_CONTROL_STOP, status_code=204, include_in_schema=False)
    def launcher_stop(x_lw_control: str | None = Header(default=None)) -> None:
        require_control(x_lw_control)
        stop_event.set()

    for static_name in ("assets", "pdfjs"):
        directory = distribution / static_name
        if directory.is_dir():
            app.mount(f"/{static_name}", StaticFiles(directory=directory), name=static_name)

    @app.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{requested_path:path}", include_in_schema=False)
    def spa_fallback(requested_path: str) -> FileResponse:
        if requested_path.startswith(("api/", "__literature_workbench/")):
            raise HTTPException(status_code=404)
        candidate = (distribution / requested_path).resolve()
        try:
            candidate.relative_to(distribution)
        except ValueError:
            raise HTTPException(status_code=404) from None
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    return app


def _choose_port(default: int = DEFAULT_PORT) -> int:
    for port in (default, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError("loopback_port_unavailable")


def _worker_python() -> str:
    current = Path(sys.executable)
    if current.name.casefold() == "pythonw.exe":
        console = current.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(current)


def _worker_runtime_python() -> str:
    worker = Path(_worker_python())
    base = Path(getattr(sys, "_base_executable", worker))
    return str(base.with_name(worker.name))


def _spawn_worker(
    log_path: Path, generation: str, token: str
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    try:
        return subprocess.Popen(
            [
                _worker_python(),
                "-m",
                "literature_workbench.launcher",
                "_worker",
                generation,
                token,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        log.close()


def _wait_for_identity(pid: int, timeout_seconds: float = 5.0) -> ProcessIdentity:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        identity = lookup_process_identity(pid)
        if identity is not None:
            return identity
        time.sleep(0.05)
    raise RuntimeError("worker_identity_unavailable")


def wait_for_worker_ready(
    path: Path,
    *,
    generation: str,
    token: str,
    expected_worker: ProcessIdentity,
    expected_child_executable: str | None = None,
    child_alive: Callable[[], bool],
    identity_lookup: IdentityLookup = lookup_process_identity,
    parent_lookup: ParentLookup = lookup_parent_pid,
    cmdline_lookup: CommandLineLookup = lookup_command_line,
    timeout_seconds: float = 15.0,
) -> ProcessIdentity:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not child_alive():
            raise RuntimeError("worker_initialization_failed")
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
            ready = WorkerReady.from_json(loaded)
        except (OSError, json.JSONDecodeError, ValueError):
            time.sleep(0.05)
            continue
        if hmac.compare_digest(ready.generation, generation) and hmac.compare_digest(
            ready.token, token
        ):
            if _same_identity(expected_worker, ready.worker):
                return ready.worker
            actual = identity_lookup(ready.worker.pid)
            command = cmdline_lookup(ready.worker.pid)
            if (
                expected_child_executable is not None
                and _same_identity(ready.worker, actual)
                and parent_lookup(ready.worker.pid) == expected_worker.pid
                and _same_executable(ready.worker.executable, expected_child_executable)
                and command is not None
                and len(command) == 6
                and _same_executable(command[0], expected_child_executable)
                and command[1:5]
                == (
                    "-m",
                    "literature_workbench.launcher",
                    "_worker",
                    generation,
                )
                and hmac.compare_digest(command[5], token)
            ):
                return ready.worker
        time.sleep(0.05)
    raise RuntimeError("worker_ready_timeout")


async def run_worker_until_parent_exit(
    worker: Worker | WorkerPool,
    parent: ProcessIdentity,
    *,
    identity_lookup: IdentityLookup = lookup_process_identity,
    signal_ready: Callable[[], None],
    poll_interval: float = 0.25,
) -> None:
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0)
    if task.done():
        await task
        raise RuntimeError("worker_run_loop_failed")
    signal_ready()
    try:
        while _same_identity(parent, identity_lookup(parent.pid)):
            if task.done():
                await task
                raise RuntimeError("worker_run_loop_failed")
            await asyncio.sleep(poll_interval)
    finally:
        worker.stop()
        await task


def _wait_worker_lock_available(path: Path, timeout_seconds: float = 15.0) -> None:
    lock = FileLock(path)
    try:
        lock.acquire(timeout=timeout_seconds)
    except Timeout:
        raise RuntimeError("worker_lock_held") from None
    finally:
        if lock.is_locked:
            lock.release()


def _stop_exact_process(
    process: subprocess.Popen[bytes], expected: ProcessIdentity | None
) -> None:
    if expected is not None:
        current = lookup_process_identity(expected.pid)
        if current is not None and not _same_identity(expected, current):
            return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except (OSError, psutil.Error):
        return


def _stop_exact_identity(expected: ProcessIdentity) -> None:
    if not _same_identity(expected, lookup_process_identity(expected.pid)):
        return
    try:
        process = psutil.Process(expected.pid)
        process.terminate()
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        if _same_identity(expected, lookup_process_identity(expected.pid)):
            process.kill()
            process.wait(timeout=5)
    except (OSError, psutil.Error):
        return


def _wait_until_healthy(port: int, thread: Thread, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("loopback_server_failed")
        if _health_probe(url):
            return
        time.sleep(0.1)
    raise RuntimeError("loopback_server_not_ready")


def _stop_owned_qwen(runtime_dir: Path) -> None:
    try:
        QwenProcessManager(runtime_dir).stop_owned()
    except QwenProcessError:
        raise RuntimeError("owned_qwen_cleanup_failed") from None


def run_supervisor(
    settings: AppSettings,
    web_dist: Path,
    *,
    browser_open: BrowserOpen = webbrowser.open,
    acl_hardener: Callable[[Path, TrustedRuntimeRoot], None] = secure_owner_directory,
) -> None:
    runtime_dir = settings.data_root / "runtime" / "launcher"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    acl_hardener(runtime_dir, TrustedRuntimeRoot(settings.data_root))
    manifest_path = runtime_dir / _MANIFEST_NAME
    worker_lock_path = runtime_dir / _WORKER_LOCK_NAME
    bootstrap_path = runtime_dir / _WORKER_BOOTSTRAP_NAME
    ready_path = runtime_dir / _WORKER_READY_NAME
    recover_exact_stale_worker(manifest_path)
    _wait_worker_lock_available(worker_lock_path)
    stop_event = Event()
    control_token = secrets.token_urlsafe(32)
    worker_token = secrets.token_urlsafe(32)
    generation = secrets.token_urlsafe(24)
    session = LocalSession()
    port = _choose_port()
    app = create_desktop_app(settings, session, control_token, stop_event, web_dist)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            proxy_headers=False,
            forwarded_allow_ips="",
            log_config=None,
        )
    )
    server_thread = Thread(target=server.run, name="literature-workbench-api", daemon=True)
    worker: subprocess.Popen[bytes] | None = None
    worker_launcher_identity: ProcessIdentity | None = None
    worker_identity: ProcessIdentity | None = None
    supervisor_identity = lookup_process_identity(os.getpid())
    if supervisor_identity is None:
        raise RuntimeError("supervisor_identity_unavailable")
    server_thread.start()
    try:
        _wait_until_healthy(port, server_thread)
        WorkerBootstrap(
            generation,
            worker_token,
            supervisor_identity,
            session.session_generation,
        ).write(bootstrap_path)
        worker = _spawn_worker(runtime_dir / "worker.log", generation, worker_token)
        worker_launcher_identity = _wait_for_identity(worker.pid)
        worker_identity = wait_for_worker_ready(
            ready_path,
            generation=generation,
            token=worker_token,
            expected_worker=worker_launcher_identity,
            expected_child_executable=_worker_runtime_python(),
            child_alive=lambda: worker is not None and worker.poll() is None,
        )
        ready_path.unlink(missing_ok=True)
        manifest = RuntimeManifest(
            port=port,
            supervisor=supervisor_identity,
            worker=worker_identity,
            control_token=control_token,
            generation=generation,
        )
        _write_manifest(manifest_path, manifest)
        browser_open(
            f"http://127.0.0.1:{port}/bootstrap#{session.issue_bootstrap_token()}"
        )
        while not stop_event.wait(0.25):
            if worker.poll() is not None:
                raise RuntimeError("worker_exited")
            if not server_thread.is_alive():
                raise RuntimeError("loopback_server_failed")
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        if worker is not None:
            if (
                worker_identity is not None
                and worker_launcher_identity is not None
                and not _same_identity(worker_launcher_identity, worker_identity)
            ):
                _stop_exact_identity(worker_identity)
            _stop_exact_process(worker, worker_launcher_identity)
        qwen_cleanup_failed = False
        try:
            _stop_owned_qwen(settings.qwen_runtime_dir)
        except RuntimeError:
            qwen_cleanup_failed = True
        bootstrap_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        if not qwen_cleanup_failed:
            manifest_path.unlink(missing_ok=True)
        if qwen_cleanup_failed:
            raise RuntimeError("owned_qwen_cleanup_failed") from None


def run_bound_worker(
    settings: AppSettings,
    generation: str,
    token: str,
    *,
    identity_lookup: IdentityLookup = lookup_process_identity,
) -> None:
    runtime_dir = settings.data_root / "runtime" / "launcher"
    bootstrap = consume_worker_bootstrap(
        runtime_dir / _WORKER_BOOTSTRAP_NAME,
        generation=generation,
        token=token,
        identity_lookup=identity_lookup,
    )
    worker_lock = FileLock(runtime_dir / _WORKER_LOCK_NAME)
    try:
        worker_lock.acquire(timeout=0)
    except Timeout:
        raise RuntimeError("worker_already_running") from None
    trusted_root: TrustedImportRoot | None = None
    qwen_process: QwenProcessManager | None = None
    ready_path = runtime_dir / _WORKER_READY_NAME
    try:
        database = Database.open(settings.database_path)
        run_migrations(database)

        def queue_clock() -> datetime:
            return datetime.now(UTC)

        queue = TaskQueue(database, queue_clock)
        translation_repository = TranslationRepository(database, queue_clock)
        translation_repository.expire_other_sessions(bootstrap.session_generation)
        queue.requeue_expired()
        inspector = SafeWindowsFileInspector()
        trusted_root = TrustedImportRoot.create(settings.data_root)
        qwen_process = QwenProcessManager(settings.qwen_runtime_dir)
        library_repository = LibraryRepository(database, queue)
        scanner = LibraryScanner(
            database,
            library_repository,
            inspector,
            queue,
            coordinator=LibraryScanCoordinator(),
        )
        import_validator = ImportValidator(
            database, queue, trusted_root, inspector=inspector
        )
        parser = ParseWorkerService(
            database,
            inspector,
            settings.data_root,
            reconcile=scanner.reconcile_path,
        )
        secret_store = create_windows_secret_store()
        translation = TranslationService(
            translation_repository,
            SettingsRepository(database),
            queue,
            DeepSeekGateway(
                database,
                secret_store,
                process_generation=generation,
            ),
            QwenLocalClient(),
            qwen_process,
        )
        background_handlers, interactive_handlers = partition_worker_handlers(
            scanner,
            import_validator,
            parser,
            translation,
        )
        worker = WorkerPool(
            Worker(
                queue,
                f"desktop-worker-{generation}-background",
                background_handlers,
            ),
            Worker(
                queue,
                f"desktop-worker-{generation}-translation",
                interactive_handlers,
            ),
        )
        worker_identity = identity_lookup(os.getpid())
        if worker_identity is None:
            raise RuntimeError("worker_identity_unavailable")

        def signal_ready() -> None:
            WorkerReady(generation, token, worker_identity).write(ready_path)

        asyncio.run(
            run_worker_until_parent_exit(
                worker,
                bootstrap.parent,
                identity_lookup=identity_lookup,
                signal_ready=signal_ready,
            )
        )
    finally:
        ready_path.unlink(missing_ok=True)
        qwen_cleanup_failed = False
        if qwen_process is not None:
            try:
                qwen_process.stop_owned()
            except QwenProcessError:
                qwen_cleanup_failed = True
        if trusted_root is not None:
            trusted_root.close()
        worker_lock.release()
        if qwen_cleanup_failed:
            raise RuntimeError("owned_qwen_cleanup_failed") from None


def _runtime_paths(settings: AppSettings) -> tuple[Path, Path]:
    runtime_dir = settings.data_root / "runtime" / "launcher"
    return runtime_dir / _LOCK_NAME, runtime_dir / _MANIFEST_NAME


def start(settings: AppSettings, web_dist: Path) -> None:
    lock_path, manifest_path = _runtime_paths(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path)
    deadline = time.monotonic() + 15
    while True:
        try:
            lock.acquire(timeout=0)
            break
        except Timeout:
            if reuse_running_instance(manifest_path):
                return
            if time.monotonic() >= deadline:
                if stop_running_instance(manifest_path, timeout_seconds=20.0):
                    deadline = time.monotonic() + 15
                    continue
                raise RuntimeError("existing_instance_unhealthy") from None
            time.sleep(0.1)
    try:
        run_supervisor(settings, web_dist)
    finally:
        lock.release()


def _project_web_dist() -> Path:
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="literature-workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    if sys.platform != "win32":
        raise SystemExit("Literature Workbench launcher supports Windows only")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["_worker"]:
        if len(arguments) != 3 or len(arguments[1]) < 16 or len(arguments[2]) < 32:
            raise SystemExit("invalid internal worker capability")
        run_bound_worker(AppSettings(), arguments[1], arguments[2])
        return
    args = build_argument_parser().parse_args(arguments)
    settings = AppSettings()
    if args.command == "start":
        start(settings, _project_web_dist())
    else:
        _, manifest_path = _runtime_paths(settings)
        if not stop_running_instance(manifest_path):
            raise SystemExit("No validated Literature Workbench instance is running")


if __name__ == "__main__":
    main()
