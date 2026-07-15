from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, enumerate as enumerate_threads
from types import SimpleNamespace

import httpx
import psutil
import pytest

import literature_workbench.features.models.qwen_process as qwen_process_module

from literature_workbench.features.models.qwen_process import (
    ProcessIdentity,
    QwenProcessError,
    QwenProcessManager,
)


EXPECTED_ALIAS = "literature-workbench-qwen"
EXPECTED_EXE = Path(r"D:\Qwen\apps\llama.cpp-b9564-cuda12.4\llama-server.exe")
EXPECTED_MODEL = Path(
    r"D:\Qwen\models\qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
    r"\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_XS.gguf"
)
EXPECTED_MMPROJ = Path(
    r"D:\Qwen\models\qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
    r"\mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf"
)


@dataclass
class FakeClock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def expected_identity(pid: int = 321) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        create_time=1234.5,
        exe=str(EXPECTED_EXE),
        cmdline=(
            str(EXPECTED_EXE),
            "--host", "127.0.0.1", "--port", "18080",
            "--model", str(EXPECTED_MODEL), "--mmproj", str(EXPECTED_MMPROJ),
            "--alias", EXPECTED_ALIAS, "--ctx-size", "32768", "--n-predict", "2048",
            "--parallel", "1", "--jinja", "--reasoning", "off",
        ),
    )


def healthy_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 18080
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": EXPECTED_ALIAS}]}, request=request)
        raise AssertionError(request.url)

    return httpx.MockTransport(handler)


def test_existing_expected_service_is_borrowed_and_never_stopped(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    launches: list[tuple[str, ...]] = []
    stops: list[int] = []
    manager = QwenProcessManager(
        tmp_path / "runtime" / "qwen",
        transport=healthy_transport(calls),
        launcher=lambda args, log: launches.append(args) or 321,
        identity_lookup=lambda pid: expected_identity(pid),
        listener_owner=lambda host, port: expected_identity(),
        stopper=stops.append,
    )

    lease = manager.ensure_running()
    manager.stop_owned()

    assert lease.borrowed is True
    assert launches == []
    assert stops == []
    assert [request.url.path for request in calls] == ["/health", "/v1/models"]


def test_healthy_service_with_unprovable_listener_owner_fails_closed(
    tmp_path: Path,
) -> None:
    launches: list[tuple[str, ...]] = []
    stops: list[int] = []
    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=healthy_transport([]),
        launcher=lambda args, log: launches.append(args) or 321,
        listener_owner=lambda host, port: None,
        stopper=stops.append,
    )

    with pytest.raises(QwenProcessError, match="qwen_port_identity_mismatch"):
        manager.ensure_running()

    assert launches == []
    assert stops == []


@pytest.mark.parametrize("mutation", ["changed_ctx_size", "extra_flag"])
def test_healthy_same_exe_service_with_nonexact_fixed_command_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    identity = expected_identity()
    command = list(identity.cmdline)
    if mutation == "changed_ctx_size":
        command[command.index("--ctx-size") + 1] = "4096"
    else:
        command.append("--metrics")
    hostile = ProcessIdentity(
        identity.pid,
        identity.create_time,
        identity.exe,
        tuple(command),
    )
    launches: list[tuple[str, ...]] = []
    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=healthy_transport([]),
        launcher=lambda args, log: launches.append(args) or 321,
        listener_owner=lambda host, port: hostile,
    )

    with pytest.raises(QwenProcessError, match="qwen_port_identity_mismatch"):
        manager.ensure_running()

    assert launches == []


def test_windows_listener_owner_resolver_uses_exact_loopback_endpoint_and_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = [
        SimpleNamespace(
            status=psutil.CONN_LISTEN,
            pid=321,
            laddr=("127.0.0.1", 18080),
        ),
        SimpleNamespace(
            status=psutil.CONN_LISTEN,
            pid=999,
            laddr=("127.0.0.1", 18081),
        ),
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda **_: connections)
    monkeypatch.setattr(
        qwen_process_module,
        "_lookup_identity",
        lambda pid: expected_identity(pid),
    )

    assert qwen_process_module._resolve_listener_owner(
        "127.0.0.1", 18080
    ) == expected_identity()


def test_unknown_service_on_fixed_port_fails_closed(tmp_path: Path) -> None:
    launches: list[tuple[str, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        return httpx.Response(200, json={"data": [{"id": "unknown-model"}]}, request=request)

    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(handler),
        launcher=lambda args, log: launches.append(args) or 321,
    )
    with pytest.raises(QwenProcessError, match="qwen_port_identity_mismatch"):
        manager.ensure_running()
    assert launches == []


def test_unavailable_service_launches_only_fixed_assets_and_records_ownership(
    tmp_path: Path,
) -> None:
    requests = 0
    launched: list[tuple[tuple[str, ...], Path]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests <= 2:
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        return httpx.Response(200, json={"data": [{"id": EXPECTED_ALIAS}]}, request=request)

    runtime = tmp_path / "runtime" / "qwen"
    manager = QwenProcessManager(
        runtime,
        transport=httpx.MockTransport(handler),
        launcher=lambda args, log: launched.append((args, log)) or 321,
        identity_lookup=lambda pid: expected_identity(pid),
        clock=FakeClock(),
    )
    lease = manager.ensure_running()

    assert lease.borrowed is False
    assert len(launched) == 1
    args, log_path = launched[0]
    assert args == (
        str(EXPECTED_EXE),
        "--host", "127.0.0.1", "--port", "18080",
        "--model", str(EXPECTED_MODEL), "--mmproj", str(EXPECTED_MMPROJ),
        "--alias", EXPECTED_ALIAS, "--ctx-size", "32768", "--n-predict", "2048",
        "--parallel", "1", "--jinja", "--reasoning", "off",
    )
    assert log_path.parent == runtime
    record = json.loads((runtime / "owned-process.json").read_text(encoding="utf-8"))
    assert record == {
        "pid": 321,
        "create_time": 1234.5,
        "exe": str(EXPECTED_EXE),
        "model": str(EXPECTED_MODEL),
    }


def test_start_timeout_is_fixed_and_sanitized(tmp_path: Path) -> None:
    clock = FakeClock()
    stops: list[int] = []
    live = True

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail", request=request)

    def stop(pid: int) -> None:
        nonlocal live
        stops.append(pid)
        live = False

    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(offline),
        launcher=lambda args, log: 321,
        identity_lookup=lambda pid: expected_identity(pid) if live else None,
        stopper=stop,
        clock=clock,
    )
    with pytest.raises(QwenProcessError) as caught:
        manager.ensure_running()
    assert str(caught.value) == "qwen_start_timeout"
    assert clock.now == 120.0
    assert stops == [321]
    assert not (tmp_path / "qwen" / "owned-process.json").exists()


def test_stop_requires_matching_pid_create_time_exe_and_model(tmp_path: Path) -> None:
    runtime = tmp_path / "qwen"
    runtime.mkdir(parents=True)
    record = {
        "pid": 321,
        "create_time": 1234.5,
        "exe": str(EXPECTED_EXE),
        "model": str(EXPECTED_MODEL),
    }
    (runtime / "owned-process.json").write_text(json.dumps(record), encoding="utf-8")
    stops: list[int] = []
    mismatched = expected_identity()
    mismatched = ProcessIdentity(
        mismatched.pid,
        mismatched.create_time + 1,
        mismatched.exe,
        mismatched.cmdline,
    )
    manager = QwenProcessManager(
        runtime,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
        identity_lookup=lambda pid: mismatched,
        stopper=stops.append,
    )
    with pytest.raises(QwenProcessError, match="qwen_owned_process_mismatch"):
        manager.stop_owned()
    assert stops == []


@pytest.mark.parametrize("not_ready", ["health", "models"])
def test_post_launch_not_ready_response_is_polled_until_ready(
    tmp_path: Path,
    not_ready: str,
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            raise httpx.ConnectError("offline", request=request)
        if not_ready == "health" and request_count == 2:
            return httpx.Response(503, json={"status": "loading"}, request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if not_ready == "models" and request_count == 3:
            return httpx.Response(200, json={"data": []}, request=request)
        return httpx.Response(
            200,
            json={"data": [{"id": EXPECTED_ALIAS}]},
            request=request,
        )

    clock = FakeClock()
    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(handler),
        launcher=lambda args, log: 321,
        identity_lookup=lambda pid: expected_identity(pid),
        clock=clock,
    )

    lease = manager.ensure_running()

    assert lease.borrowed is False
    assert clock.now == 1.0


@pytest.mark.parametrize("path", ["/health", "/v1/models"])
def test_prelaunch_probe_rejects_oversized_response_body(tmp_path: Path, path: str) -> None:
    launches: list[tuple[str, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health" and path != "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        return httpx.Response(200, content=b"x" * (64 * 1024 + 1), request=request)

    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(handler),
        launcher=lambda args, log: launches.append(args) or 321,
    )
    with pytest.raises(QwenProcessError, match="qwen_probe_response_too_large"):
        manager.ensure_running()
    assert launches == []


def test_prelaunch_probe_closes_slow_drip_response_at_total_deadline(
    tmp_path: Path,
) -> None:
    closed = Event()

    class SlowDrip(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            while not closed.wait(0.2):
                yield b" "

        def close(self) -> None:
            closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowDrip(), request=request)

    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(handler),
        probe_timeout_seconds=0.02,
        launcher=lambda args, log: pytest.fail("deadline failure must not launch Qwen"),
    )
    started = time.monotonic()

    with pytest.raises(QwenProcessError, match="qwen_probe_timeout"):
        manager.ensure_running()

    assert time.monotonic() - started < 0.15
    assert closed.wait(0.1)
    assert not any(thread.name == "qwen-http-deadline" for thread in enumerate_threads())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exe", r"C:\hostile\llama-server.exe"),
        ("model", r"C:\hostile\model.gguf"),
    ],
)
def test_hostile_record_fixedness_is_checked_before_live_process_lookup(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    runtime = tmp_path / "qwen"
    runtime.mkdir(parents=True)
    (runtime / "owned-process.json").write_text(
        json.dumps({
            "pid": 321,
            "create_time": 1234.5,
            "exe": str(EXPECTED_EXE),
            "model": str(EXPECTED_MODEL),
            field: value,
        }),
        encoding="utf-8",
    )
    lookups: list[int] = []
    manager = QwenProcessManager(
        runtime,
        identity_lookup=lambda pid: lookups.append(pid) or expected_identity(pid),
    )

    with pytest.raises(QwenProcessError, match="qwen_owned_process_mismatch"):
        manager.stop_owned()
    assert lookups == []


def test_model_path_elsewhere_in_command_line_does_not_prove_ownership(tmp_path: Path) -> None:
    runtime = tmp_path / "qwen"
    runtime.mkdir(parents=True)
    (runtime / "owned-process.json").write_text(
        json.dumps(
            {
                "pid": 321,
                "create_time": 1234.5,
                "exe": str(EXPECTED_EXE),
                "model": str(EXPECTED_MODEL),
            }
        ),
        encoding="utf-8",
    )
    hostile = ProcessIdentity(
        pid=321,
        create_time=1234.5,
        exe=str(EXPECTED_EXE),
        cmdline=(str(EXPECTED_EXE), "--unrelated", str(EXPECTED_MODEL)),
    )
    stops: list[int] = []
    manager = QwenProcessManager(
        runtime,
        identity_lookup=lambda pid: hostile,
        stopper=stops.append,
    )

    with pytest.raises(QwenProcessError, match="qwen_owned_process_mismatch"):
        manager.stop_owned()
    assert stops == []


def test_ownership_persistence_failure_cleans_exact_launched_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text
    stops: list[int] = []
    live = True

    def fail_record_write(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "owned-process.tmp":
            raise OSError("private runtime path detail")
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", fail_record_write)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    def stop(pid: int) -> None:
        nonlocal live
        stops.append(pid)
        live = False

    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(offline),
        launcher=lambda args, log: 321,
        identity_lookup=lambda pid: expected_identity(pid) if live else None,
        stopper=stop,
    )
    with pytest.raises(QwenProcessError) as caught:
        manager.ensure_running()
    assert str(caught.value) == "qwen_ownership_persist_failed"
    assert "private runtime path detail" not in str(caught.value)
    assert stops == [321]


def test_post_launch_oversized_probe_is_fatal_and_cleans_launched_process(
    tmp_path: Path,
) -> None:
    requests = 0
    stops: list[int] = []
    live = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=b"x" * (64 * 1024 + 1), request=request)

    def stop(pid: int) -> None:
        nonlocal live
        stops.append(pid)
        live = False

    runtime = tmp_path / "qwen"
    manager = QwenProcessManager(
        runtime,
        transport=httpx.MockTransport(handler),
        launcher=lambda args, log: 321,
        identity_lookup=lambda pid: expected_identity(pid) if live else None,
        stopper=stop,
    )
    with pytest.raises(QwenProcessError, match="qwen_probe_response_too_large"):
        manager.ensure_running()
    assert stops == [321]
    assert not (runtime / "owned-process.json").exists()


def test_runtime_creation_and_process_stop_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "qwen"
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(
        PermissionError("private runtime path")
    ))
    manager = QwenProcessManager(
        runtime,
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request))
        ),
    )
    with pytest.raises(QwenProcessError) as caught:
        manager.ensure_running()
    assert str(caught.value) == "qwen_runtime_failed"

    monkeypatch.undo()
    runtime.mkdir(parents=True)
    (runtime / "owned-process.json").write_text(
        json.dumps(
            {
                "pid": 321,
                "create_time": 1234.5,
                "exe": str(EXPECTED_EXE),
                "model": str(EXPECTED_MODEL),
            }
        ),
        encoding="utf-8",
    )
    manager = QwenProcessManager(
        runtime,
        identity_lookup=lambda pid: expected_identity(pid),
        stopper=lambda pid: (_ for _ in ()).throw(psutil.AccessDenied(pid=pid)),
    )
    with pytest.raises(QwenProcessError) as caught:
        manager.stop_owned()
    assert str(caught.value) == "qwen_stop_failed"


def test_stop_owned_keeps_evidence_when_stopper_does_not_end_exact_process(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "qwen"
    runtime.mkdir(parents=True)
    record_path = runtime / "owned-process.json"
    record_path.write_text(
        json.dumps(
            {
                "pid": 321,
                "create_time": 1234.5,
                "exe": str(EXPECTED_EXE),
                "model": str(EXPECTED_MODEL),
            }
        ),
        encoding="utf-8",
    )
    manager = QwenProcessManager(
        runtime,
        identity_lookup=lambda pid: expected_identity(pid),
        stopper=lambda pid: None,
    )

    with pytest.raises(QwenProcessError, match="qwen_stop_failed"):
        manager.stop_owned()

    assert record_path.exists()


def test_startup_cleanup_keeps_evidence_when_exact_launched_process_survives(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "qwen"
    runtime.mkdir(parents=True)
    identity = expected_identity()
    manager = QwenProcessManager(
        runtime,
        identity_lookup=lambda pid: identity,
        stopper=lambda pid: None,
    )
    manager._write_ownership(identity)

    with pytest.raises(QwenProcessError, match="qwen_cleanup_failed"):
        manager._cleanup_launched(identity)

    assert (runtime / "owned-process.json").exists()
