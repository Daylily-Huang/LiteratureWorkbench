from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import NoReturn, Protocol
from uuid import UUID, uuid4


CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
MAX_CAPTURE_BYTES = 64 * 1024
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    args: tuple[str, ...]
    command_line: str
    cwd: Path
    shell: bool = False


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    attempt_id: UUID
    pid: int
    created_ns: int
    executable: str
    arguments: tuple[str, ...]
    job_token: str


class SuspendedChild(Protocol):
    pid: int
    created_ns: int
    executable: str
    arguments: tuple[str, ...]

    def resume(self) -> None: ...

    def terminate(self) -> None: ...

    def request_graceful_exit(self) -> None: ...

    def wait(self, timeout: float | None) -> int | None: ...

    def read_bounded_output(self) -> tuple[bytes, bytes]: ...

    def close(self) -> None: ...


class KillOnCloseJob(Protocol):
    token: str

    def assign(self, child: SuspendedChild) -> None: ...

    def close(self) -> None: ...


class ProcessBackend(Protocol):
    def create_suspended(self, spec: ProcessSpec) -> SuspendedChild: ...

    def create_kill_on_close_job(self) -> KillOnCloseJob: ...


class ControlledProcess:
    def __init__(
        self,
        identity: ProcessIdentity,
        child: SuspendedChild,
        job: KillOnCloseJob,
    ) -> None:
        self.identity = identity
        self._child = child
        self._job = job
        self._closed = False

    def wait_and_collect(
        self,
        *,
        cancellation_check: Callable[[], None] | None = None,
        poll_seconds: float = 0.1,
    ) -> tuple[int, bytes, bytes]:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds_must_be_positive")
        while (exit_code := self._child.wait(poll_seconds)) is None:
            if cancellation_check is None:
                continue
            try:
                cancellation_check()
            except BaseException:
                self.cancel(self.identity, grace_seconds=1.0)
                raise
        stdout, stderr = self._child.read_bounded_output()
        self.close()
        return exit_code, stdout, stderr

    def cancel(self, expected: ProcessIdentity, *, grace_seconds: float) -> None:
        if expected != self.identity:
            raise ValueError("process_identity_mismatch")
        if grace_seconds < 0:
            raise ValueError("grace_seconds_must_be_nonnegative")
        self._child.request_graceful_exit()
        self._child.wait(grace_seconds)
        self.close()

    def close_stdin(self) -> None:
        self._child.request_graceful_exit()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._job.close()
        finally:
            self._child.close()


def launch_suspended_in_job(
    spec: ProcessSpec,
    attempt_id: UUID,
    record_identity: Callable[[ProcessIdentity], None],
    *,
    backend: ProcessBackend | None = None,
) -> ControlledProcess:
    if spec.shell or not spec.args:
        raise ValueError("invalid_process_spec")
    resolved_backend = backend or Win32ProcessBackend()
    child: SuspendedChild | None = None
    job: KillOnCloseJob | None = None
    try:
        child = resolved_backend.create_suspended(spec)
        job = resolved_backend.create_kill_on_close_job()
        job.assign(child)
        identity = ProcessIdentity(
            attempt_id=attempt_id,
            pid=child.pid,
            created_ns=child.created_ns,
            executable=child.executable,
            arguments=child.arguments,
            job_token=job.token,
        )
        record_identity(identity)
        child.resume()
        return ControlledProcess(identity, child, job)
    except BaseException:
        if child is not None:
            child.terminate()
        if job is not None:
            job.close()
        if child is not None:
            child.close()
        raise


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    ctypes.POINTER(_PROCESS_INFORMATION),
]
_kernel32.CreateProcessW.restype = wintypes.BOOL
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.c_void_p,
    wintypes.DWORD,
]
_kernel32.CreatePipe.restype = wintypes.BOOL
_kernel32.SetHandleInformation.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
]
_kernel32.SetHandleInformation.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
_kernel32.ResumeThread.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME),
]
_kernel32.GetProcessTimes.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
_kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
_kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
_kernel32.DeleteProcThreadAttributeList.restype = None


def _raise_last_error(operation: str) -> NoReturn:
    code = ctypes.get_last_error()
    raise OSError(code, f"{operation} failed", None, code)


class _BoundedPipeReader:
    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._buffer = bytearray()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        import msvcrt

        descriptor = msvcrt.open_osfhandle(self._handle, os.O_RDONLY | os.O_BINARY)
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                while chunk := stream.read(8192):
                    self._buffer.extend(chunk)
                    if len(self._buffer) > MAX_CAPTURE_BYTES:
                        del self._buffer[:-MAX_CAPTURE_BYTES]
        finally:
            self._handle = 0

    def finish(self) -> bytes:
        self._thread.join(timeout=5)
        return bytes(self._buffer)


class _Win32SuspendedChild:
    def __init__(
        self,
        process_handle: int,
        thread_handle: int,
        pid: int,
        spec: ProcessSpec,
        stdin_write: int,
        stdout_read: int,
        stderr_read: int,
    ) -> None:
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self._stdin_write = stdin_write
        self.pid = pid
        self.executable = spec.args[0]
        self.arguments = spec.args
        self.created_ns = _process_created_ns(process_handle)
        self._stdout = _BoundedPipeReader(stdout_read)
        self._stderr = _BoundedPipeReader(stderr_read)

    @property
    def process_handle(self) -> int:
        return self._process_handle

    def resume(self) -> None:
        if int(_kernel32.ResumeThread(wintypes.HANDLE(self._thread_handle))) == 0xFFFFFFFF:
            _raise_last_error("ResumeThread")

    def terminate(self) -> None:
        if self._process_handle:
            _kernel32.TerminateProcess(wintypes.HANDLE(self._process_handle), 1)

    def request_graceful_exit(self) -> None:
        if self._stdin_write:
            _kernel32.CloseHandle(wintypes.HANDLE(self._stdin_write))
            self._stdin_write = 0

    def wait(self, timeout: float | None) -> int | None:
        milliseconds = INFINITE if timeout is None else max(0, int(timeout * 1000))
        result = int(
            _kernel32.WaitForSingleObject(
                wintypes.HANDLE(self._process_handle), milliseconds
            )
        )
        if result == WAIT_TIMEOUT:
            return None
        if result != WAIT_OBJECT_0:
            _raise_last_error("WaitForSingleObject")
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(
            wintypes.HANDLE(self._process_handle), ctypes.byref(exit_code)
        ):
            _raise_last_error("GetExitCodeProcess")
        return int(exit_code.value)

    def read_bounded_output(self) -> tuple[bytes, bytes]:
        return self._stdout.finish(), self._stderr.finish()

    def close(self) -> None:
        self.request_graceful_exit()
        for name in ("_thread_handle", "_process_handle"):
            handle = getattr(self, name)
            if handle:
                _kernel32.CloseHandle(wintypes.HANDLE(handle))
                setattr(self, name, 0)


class _Win32KillOnCloseJob:
    def __init__(self, handle: int) -> None:
        self._handle = handle
        self.token = str(uuid4())

    def assign(self, child: SuspendedChild) -> None:
        if not isinstance(child, _Win32SuspendedChild):
            raise TypeError("win32_job_requires_win32_child")
        if not _kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle), wintypes.HANDLE(child.process_handle)
        ):
            _raise_last_error("AssignProcessToJobObject")

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = 0


class Win32ProcessBackend:
    def create_suspended(self, spec: ProcessSpec) -> SuspendedChild:
        owned_handles: set[int] = set()
        process_handle = 0
        thread_handle = 0
        try:
            stdin_read, stdin_write = _pipe()
            owned_handles.update((stdin_read, stdin_write))
            stdout_read, stdout_write = _pipe()
            owned_handles.update((stdout_read, stdout_write))
            stderr_read, stderr_write = _pipe()
            owned_handles.update((stderr_read, stderr_write))
            for parent_handle in (stdin_write, stdout_read, stderr_read):
                _make_non_inheritable(parent_handle)
            startup = _STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = wintypes.HANDLE(stdin_read)
            startup.StartupInfo.hStdOutput = wintypes.HANDLE(stdout_write)
            startup.StartupInfo.hStdError = wintypes.HANDLE(stderr_write)
            attribute_size = ctypes.c_size_t()
            _kernel32.InitializeProcThreadAttributeList(
                None, 1, 0, ctypes.byref(attribute_size)
            )
            attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
            startup.lpAttributeList = ctypes.cast(
                attribute_buffer, ctypes.c_void_p
            ).value
            inherited_handles = (wintypes.HANDLE * 3)(
                stdin_read, stdout_write, stderr_write
            )
            attribute_initialized = False
            try:
                if not _kernel32.InitializeProcThreadAttributeList(
                    startup.lpAttributeList, 1, 0, ctypes.byref(attribute_size)
                ):
                    _raise_last_error("InitializeProcThreadAttributeList")
                attribute_initialized = True
                if not _kernel32.UpdateProcThreadAttribute(
                    startup.lpAttributeList,
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    ctypes.cast(inherited_handles, ctypes.c_void_p),
                    ctypes.sizeof(inherited_handles),
                    None,
                    None,
                ):
                    _raise_last_error("UpdateProcThreadAttribute")
                information = _PROCESS_INFORMATION()
                command = ctypes.create_unicode_buffer(spec.command_line)
                created = _kernel32.CreateProcessW(
                    spec.args[0],
                    command,
                    None,
                    None,
                    True,
                    CREATE_SUSPENDED
                    | CREATE_NO_WINDOW
                    | EXTENDED_STARTUPINFO_PRESENT,
                    None,
                    str(spec.cwd),
                    ctypes.byref(startup),
                    ctypes.byref(information),
                )
            finally:
                if attribute_initialized:
                    _kernel32.DeleteProcThreadAttributeList(startup.lpAttributeList)
            if not created:
                _raise_last_error("CreateProcessW")
            process_handle = _required_handle_value(
                information.hProcess, "CreateProcessW process"
            )
            thread_handle = _required_handle_value(
                information.hThread, "CreateProcessW thread"
            )
            for handle in (stdin_read, stdout_write, stderr_write):
                _close_handle(handle)
                owned_handles.remove(handle)
            child = _Win32SuspendedChild(
                process_handle,
                thread_handle,
                int(information.dwProcessId),
                spec,
                stdin_write,
                stdout_read,
                stderr_read,
            )
            process_handle = 0
            thread_handle = 0
            owned_handles.difference_update((stdin_write, stdout_read, stderr_read))
            return child
        except BaseException:
            if process_handle:
                _kernel32.TerminateProcess(wintypes.HANDLE(process_handle), 1)
                _kernel32.WaitForSingleObject(wintypes.HANDLE(process_handle), 5000)
            raise
        finally:
            for handle in owned_handles:
                _close_handle(handle)
            _close_handle(thread_handle)
            _close_handle(process_handle)

    def create_kill_on_close_job(self) -> KillOnCloseJob:
        raw = _kernel32.CreateJobObjectW(None, None)
        handle = ctypes.cast(raw, ctypes.c_void_p).value
        if handle is None:
            _raise_last_error("CreateJobObjectW")
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            wintypes.HANDLE(handle),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            _kernel32.CloseHandle(wintypes.HANDLE(handle))
            _raise_last_error("SetInformationJobObject")
        return _Win32KillOnCloseJob(int(handle))


def _pipe() -> tuple[int, int]:
    read = wintypes.HANDLE()
    write = wintypes.HANDLE()
    security = _SECURITY_ATTRIBUTES(
        ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True
    )
    if not _kernel32.CreatePipe(
        ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0
    ):
        _raise_last_error("CreatePipe")
    if read.value is None or write.value is None:
        raise OSError("CreatePipe returned invalid handles")
    return int(read.value), int(write.value)


def _make_non_inheritable(handle: int) -> None:
    if not _kernel32.SetHandleInformation(
        wintypes.HANDLE(handle), HANDLE_FLAG_INHERIT, 0
    ):
        _raise_last_error("SetHandleInformation(parent pipe)")


def _close_handle(handle: int) -> None:
    if handle:
        _kernel32.CloseHandle(wintypes.HANDLE(handle))


def _required_handle_value(handle: wintypes.HANDLE, operation: str) -> int:
    value = ctypes.cast(handle, ctypes.c_void_p).value
    if value is None:
        raise OSError(f"{operation} returned an invalid handle")
    return int(value)


def _process_created_ns(process_handle: int) -> int:
    created = _FILETIME()
    exited = _FILETIME()
    kernel = _FILETIME()
    user = _FILETIME()
    if not _kernel32.GetProcessTimes(
        wintypes.HANDLE(process_handle),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        _raise_last_error("GetProcessTimes")
    ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    return ticks * 100
