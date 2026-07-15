from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from literature_workbench.features.models.qwen_process import (
    QWEN_ALIAS,
    QWEN_EXE,
    QWEN_MODEL,
    ProcessIdentity,
    QwenProcessError,
    QwenProcessManager,
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
        exe=str(QWEN_EXE),
        cmdline=(str(QWEN_EXE), "--model", str(QWEN_MODEL)),
    )


def healthy_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 18080
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": QWEN_ALIAS}]}, request=request)
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
        stopper=stops.append,
    )

    lease = manager.ensure_running()
    manager.stop_owned()

    assert lease.borrowed is True
    assert launches == []
    assert stops == []
    assert [request.url.path for request in calls] == ["/health", "/v1/models"]


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
        return httpx.Response(200, json={"data": [{"id": QWEN_ALIAS}]}, request=request)

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
        str(QWEN_EXE),
        "--host", "127.0.0.1", "--port", "18080",
        "--model", str(QWEN_MODEL), "--mmproj", str(manager.mmproj),
        "--alias", QWEN_ALIAS, "--ctx-size", "32768", "--n-predict", "2048",
        "--parallel", "1", "--jinja", "--reasoning", "off",
    )
    assert log_path.parent == runtime
    record = json.loads((runtime / "owned-process.json").read_text(encoding="utf-8"))
    assert record == {
        "pid": 321,
        "create_time": 1234.5,
        "exe": str(QWEN_EXE),
        "model": str(QWEN_MODEL),
    }


def test_start_timeout_is_fixed_and_sanitized(tmp_path: Path) -> None:
    clock = FakeClock()

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail", request=request)

    manager = QwenProcessManager(
        tmp_path / "qwen",
        transport=httpx.MockTransport(offline),
        launcher=lambda args, log: 321,
        identity_lookup=lambda pid: expected_identity(pid),
        stopper=lambda pid: None,
        clock=clock,
    )
    with pytest.raises(QwenProcessError) as caught:
        manager.ensure_running()
    assert str(caught.value) == "qwen_start_timeout"
    assert clock.now == 120.0


def test_stop_requires_matching_pid_create_time_exe_and_model(tmp_path: Path) -> None:
    runtime = tmp_path / "qwen"
    runtime.mkdir(parents=True)
    record = {
        "pid": 321,
        "create_time": 1234.5,
        "exe": str(QWEN_EXE),
        "model": str(QWEN_MODEL),
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
