from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

import literature_workbench.platform.windows.job_object as job_object
from literature_workbench.platform.windows.job_object import (
    ControlledProcess,
    ProcessIdentity,
    ProcessSpec,
    Win32ProcessBackend,
    _required_handle_value,
    launch_suspended_in_job,
)


def test_required_handle_value_extracts_ctypes_handle() -> None:
    assert _required_handle_value(wintypes.HANDLE(1234), "test") == 1234


def test_win32_child_construction_failure_terminates_exact_suspended_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_pid = 0

    def fail_child_construction(
        _process_handle: int,
        _thread_handle: int,
        pid: int,
        *_args: object,
    ) -> None:
        nonlocal captured_pid
        captured_pid = pid
        raise RuntimeError("injected_child_construction_failure")

    monkeypatch.setattr(job_object, "_Win32SuspendedChild", fail_child_construction)
    args = (r"C:\Windows\System32\cmd.exe", "/d", "/c", "exit", "17")
    spec = ProcessSpec(
        args=args,
        command_line=subprocess.list2cmdline(args),
        cwd=Path.cwd(),
    )

    with pytest.raises(RuntimeError, match="injected_child_construction_failure"):
        Win32ProcessBackend().create_suspended(spec)

    assert captured_pid > 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    handle = open_process(0x1000, False, captured_pid)
    if handle:
        try:
            exit_code = wintypes.DWORD()
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL
            assert get_exit_code(handle, ctypes.byref(exit_code))
            assert exit_code.value != 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)


def test_startup_handle_list_does_not_leak_unrelated_inheritable_handle(
    tmp_path: Path,
) -> None:
    descriptor = os.open(tmp_path / "sentinel.txt", os.O_CREAT | os.O_RDONLY)
    sentinel_handle = msvcrt.get_osfhandle(descriptor)
    os.set_handle_inheritable(sentinel_handle, True)
    probe = (
        "import ctypes,sys; h=int(sys.argv[1]); "
        "kind=ctypes.windll.kernel32.GetFileType(h); "
        "raise SystemExit(9 if kind else 0)"
    )
    args = (sys.executable, "-c", probe, str(sentinel_handle))
    try:
        process = launch_suspended_in_job(
            ProcessSpec(
                args=args,
                command_line=subprocess.list2cmdline(args),
                cwd=tmp_path,
            ),
            uuid4(),
            lambda _: None,
        )
        exit_code, _, _ = process.wait_and_collect()
    finally:
        os.close(descriptor)

    assert exit_code == 0


def test_parent_can_close_only_child_stdin_to_deliver_eof(tmp_path: Path) -> None:
    probe = (
        "import sys; data=sys.stdin.buffer.read(); "
        "raise SystemExit(0 if data == b'' else 8)"
    )
    args = (sys.executable, "-c", probe)
    process = launch_suspended_in_job(
        ProcessSpec(
            args=args,
            command_line=subprocess.list2cmdline(args),
            cwd=tmp_path,
        ),
        uuid4(),
        lambda _: None,
    )

    try:
        process.close_stdin()
    except BaseException:
        process.cancel(process.identity, grace_seconds=1)
        raise

    exit_code, _, _ = process.wait_and_collect()
    assert exit_code == 0


def test_large_stdout_and_stderr_are_drained_without_deadlock_and_keep_tails(
    tmp_path: Path,
) -> None:
    stdout_tail = b"STDOUT-TAIL-SENTINEL"
    stderr_tail = b"STDERR-TAIL-SENTINEL"
    probe = (
        "import sys; "
        "sys.stdout.buffer.write(b'o' * (256 * 1024) + b'STDOUT-TAIL-SENTINEL'); "
        "sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'e' * (256 * 1024) + b'STDERR-TAIL-SENTINEL'); "
        "sys.stderr.buffer.flush()"
    )
    args = (sys.executable, "-c", probe)
    process = launch_suspended_in_job(
        ProcessSpec(args=args, command_line=subprocess.list2cmdline(args), cwd=tmp_path),
        uuid4(),
        lambda _: None,
    )

    exit_code, stdout, stderr = process.wait_and_collect()

    assert exit_code == 0
    assert len(stdout) == job_object.MAX_CAPTURE_BYTES
    assert len(stderr) == job_object.MAX_CAPTURE_BYTES
    assert stdout.endswith(stdout_tail)
    assert stderr.endswith(stderr_tail)


def test_cancellation_closes_job_and_leaves_no_spawned_descendant(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "descendant.pid"
    descendant = "import time; time.sleep(60)"
    probe = (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='ascii'); "
        "sys.stdin.buffer.read()"
    )
    args = (sys.executable, "-c", probe, str(pid_file))
    process = launch_suspended_in_job(
        ProcessSpec(args=args, command_line=subprocess.list2cmdline(args), cwd=tmp_path),
        uuid4(),
        lambda _: None,
    )
    deadline = time.monotonic() + 5
    while (
        (not pid_file.exists() or pid_file.stat().st_size == 0)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text(encoding="ascii"))

    process.cancel(process.identity, grace_seconds=1)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000, False, descendant_pid)
    if handle:
        try:
            assert kernel32.WaitForSingleObject(handle, 2000) == 0
        finally:
            kernel32.CloseHandle(handle)


@dataclass(slots=True)
class FakeChild:
    events: list[str]
    pid: int = 321
    created_ns: int = 987654321
    executable: str = r"C:\Program Files\PowerShell\7\pwsh.exe"
    arguments: tuple[str, ...] = ("pwsh.exe", "-NoProfile")
    wait_result: int | None = None

    def resume(self) -> None:
        self.events.append("resume")

    def terminate(self) -> None:
        self.events.append("terminate-child")

    def request_graceful_exit(self) -> None:
        self.events.append("graceful-exit")

    def wait(self, timeout: float | None) -> int | None:
        self.events.append(f"wait:{timeout}")
        return self.wait_result

    def read_bounded_output(self) -> tuple[bytes, bytes]:
        return b"stdout", b"stderr"

    def close(self) -> None:
        self.events.append("close-child")


@dataclass(slots=True)
class FakeJob:
    events: list[str]
    token: str = "owned-job-token"

    def assign(self, child: FakeChild) -> None:
        assert child.pid == 321
        self.events.append("assign-job")

    def close(self) -> None:
        self.events.append("close-job")


@dataclass(slots=True)
class FakeBackend:
    events: list[str] = field(default_factory=list)
    child: FakeChild = field(init=False)
    job: FakeJob = field(init=False)

    def __post_init__(self) -> None:
        self.child = FakeChild(self.events)
        self.job = FakeJob(self.events)

    def create_suspended(self, spec: ProcessSpec) -> FakeChild:
        assert spec.shell is False
        self.events.append("create-suspended")
        self.child.executable = spec.args[0]
        self.child.arguments = spec.args
        return self.child

    def create_kill_on_close_job(self) -> FakeJob:
        self.events.append("create-job")
        return self.job


def _spec() -> ProcessSpec:
    args = (r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile")
    return ProcessSpec(args=args, command_line='"pwsh.exe" -NoProfile', cwd=Path("work"))


def test_launcher_orders_create_assign_record_then_resume() -> None:
    backend = FakeBackend()
    recorded: list[ProcessIdentity] = []

    process = launch_suspended_in_job(
        _spec(),
        uuid4(),
        lambda identity: (backend.events.append("record-identity"), recorded.append(identity)),
        backend=backend,
    )

    assert isinstance(process, ControlledProcess)
    assert backend.events == [
        "create-suspended",
        "create-job",
        "assign-job",
        "record-identity",
        "resume",
    ]
    assert recorded[0].job_token == "owned-job-token"
    assert recorded[0].pid == 321


def test_failure_before_resume_terminates_only_new_child_and_job() -> None:
    backend = FakeBackend()

    def fail_record(_: ProcessIdentity) -> None:
        backend.events.append("record-identity")
        raise OSError("checkpoint unavailable")

    with pytest.raises(OSError, match="checkpoint unavailable"):
        launch_suspended_in_job(_spec(), uuid4(), fail_record, backend=backend)

    assert "resume" not in backend.events
    assert backend.events == [
        "create-suspended",
        "create-job",
        "assign-job",
        "record-identity",
        "terminate-child",
        "close-job",
        "close-child",
    ]


def test_cancellation_requires_exact_recorded_identity_before_job_close() -> None:
    backend = FakeBackend()
    process = launch_suspended_in_job(
        _spec(), uuid4(), lambda _: None, backend=backend
    )
    baseline = list(backend.events)
    wrong = ProcessIdentity(
        attempt_id=process.identity.attempt_id,
        pid=process.identity.pid,
        created_ns=process.identity.created_ns,
        executable=process.identity.executable,
        arguments=process.identity.arguments,
        job_token="wrong-token",
    )

    with pytest.raises(ValueError, match="process_identity_mismatch"):
        process.cancel(wrong, grace_seconds=0.1)

    assert backend.events == baseline
    process.cancel(process.identity, grace_seconds=0.1)
    assert backend.events[-4:] == [
        "graceful-exit",
        "wait:0.1",
        "close-job",
        "close-child",
    ]
