from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx
import psutil  # type: ignore[import-untyped]


QWEN_HOST = "127.0.0.1"
QWEN_PORT = 18080
QWEN_ALIAS = "literature-workbench-qwen"
QWEN_BASE_URL = f"http://{QWEN_HOST}:{QWEN_PORT}"
QWEN_EXE = Path(r"D:\Qwen\apps\llama.cpp-b9564-cuda12.4\llama-server.exe")
QWEN_MODEL = Path(
    r"D:\Qwen\models\qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
    r"\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_XS.gguf"
)
QWEN_MMPROJ = Path(
    r"D:\Qwen\models\qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
    r"\mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf"
)
STARTUP_TIMEOUT_SECONDS = 120.0
MAX_PROBE_RESPONSE_BYTES = 64 * 1024
_OWNERSHIP_FILE = "owned-process.json"


class QwenProcessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QwenLease:
    borrowed: bool
    pid: int | None


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time: float
    exe: str
    cmdline: tuple[str, ...]


class Launcher(Protocol):
    def __call__(self, args: tuple[str, ...], log_path: Path) -> int: ...


class IdentityLookup(Protocol):
    def __call__(self, pid: int) -> ProcessIdentity | None: ...


class Stopper(Protocol):
    def __call__(self, pid: int) -> None: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _launch_hidden(args: tuple[str, ...], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - command is a fixed tuple of trusted assets
            args,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
        )
    return process.pid


def _lookup_identity(pid: int) -> ProcessIdentity | None:
    try:
        process = psutil.Process(pid)
        return ProcessIdentity(
            pid=pid,
            create_time=process.create_time(),
            exe=process.exe(),
            cmdline=tuple(process.cmdline()),
        )
    except (psutil.Error, OSError):
        return None


def _stop_process(pid: int) -> None:
    try:
        process = psutil.Process(pid)
        process.terminate()
        process.wait(timeout=10.0)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)
    except psutil.NoSuchProcess:
        return


class QwenProcessManager:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        launcher: Launcher = _launch_hidden,
        identity_lookup: IdentityLookup = _lookup_identity,
        stopper: Stopper = _stop_process,
        clock: Clock | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.mmproj = QWEN_MMPROJ
        self._transport = transport
        self._launcher = launcher
        self._identity_lookup = identity_lookup
        self._stopper = stopper
        self._clock = clock or _SystemClock()

    def ensure_running(self) -> QwenLease:
        if self._probe_expected_service(prelaunch=True):
            return QwenLease(borrowed=True, pid=None)

        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise QwenProcessError("qwen_runtime_failed") from None
        args = self._fixed_args()
        log_path = self.runtime_dir / "llama-server.log"
        try:
            pid = self._launcher(args, log_path)
        except (OSError, subprocess.SubprocessError):
            raise QwenProcessError("qwen_start_failed") from None

        try:
            identity = self._identity_lookup(pid)
        except (psutil.Error, OSError):
            raise QwenProcessError("qwen_process_inspection_failed") from None
        if identity is None or not self._matches_fixed_process(identity):
            raise QwenProcessError("qwen_started_process_mismatch")
        try:
            self._write_ownership(identity)
        except OSError:
            self._cleanup_launched(identity)
            raise QwenProcessError("qwen_ownership_persist_failed") from None

        deadline = self._clock.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            try:
                if self._probe_expected_service(prelaunch=False):
                    return QwenLease(borrowed=False, pid=pid)
            except QwenProcessError:
                self._cleanup_launched(identity)
                raise
            if self._clock.monotonic() >= deadline:
                self._cleanup_launched(identity)
                raise QwenProcessError("qwen_start_timeout")
            self._clock.sleep(1.0)

    def stop_owned(self) -> bool:
        record_path = self.runtime_dir / _OWNERSHIP_FILE
        try:
            if not record_path.is_file():
                return False
            loaded: object = json.loads(record_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise TypeError
            record = cast(dict[str, object], loaded)
            raw_pid = record["pid"]
            raw_create_time = record["create_time"]
            raw_exe = record["exe"]
            raw_model = record["model"]
            if (
                not isinstance(raw_pid, int)
                or not isinstance(raw_create_time, (int, float))
                or not isinstance(raw_exe, str)
                or not isinstance(raw_model, str)
            ):
                raise TypeError
            pid = raw_pid
            create_time = float(raw_create_time)
            exe = raw_exe
            model = raw_model
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            raise QwenProcessError("qwen_owned_process_record_invalid") from None

        if not self._same_path(exe, str(QWEN_EXE)) or not self._same_path(
            model, str(QWEN_MODEL)
        ):
            raise QwenProcessError("qwen_owned_process_mismatch")
        try:
            identity = self._identity_lookup(pid)
        except (psutil.Error, OSError):
            raise QwenProcessError("qwen_process_inspection_failed") from None
        if (
            identity is None
            or identity.pid != pid
            or identity.create_time != create_time
            or not self._matches_fixed_process(identity)
        ):
            raise QwenProcessError("qwen_owned_process_mismatch")
        try:
            self._stopper(pid)
            record_path.unlink(missing_ok=True)
        except (psutil.Error, OSError):
            raise QwenProcessError("qwen_stop_failed") from None
        return True

    def _probe_expected_service(self, *, prelaunch: bool) -> bool:
        try:
            with httpx.Client(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(2.0),
            ) as client:
                health_status, health_payload = self._read_probe(client, "/health")
                if health_status != 200 or health_payload != {"status": "ok"}:
                    return self._not_ready(prelaunch)
                models_status, models_payload = self._read_probe(client, "/v1/models")
                if models_status != 200 or not self._has_expected_alias(models_payload):
                    return self._not_ready(prelaunch)
                return True
        except httpx.ConnectError:
            return False
        except QwenProcessError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return self._not_ready(prelaunch)

    @staticmethod
    def _read_probe(client: httpx.Client, path: str) -> tuple[int, object | None]:
        with client.stream("GET", f"{QWEN_BASE_URL}{path}") as response:
            if response.status_code != 200:
                return response.status_code, None
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAX_PROBE_RESPONSE_BYTES:
                    raise QwenProcessError("qwen_probe_response_too_large")
                raw.extend(chunk)
        payload: object = json.loads(raw)
        return response.status_code, payload

    @staticmethod
    def _not_ready(prelaunch: bool) -> bool:
        if prelaunch:
            raise QwenProcessError("qwen_port_identity_mismatch")
        return False

    @staticmethod
    def _has_expected_alias(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        data = payload.get("data")
        if not isinstance(data, list):
            return False
        return any(isinstance(item, dict) and item.get("id") == QWEN_ALIAS for item in data)

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return str(Path(left)).casefold() == str(Path(right)).casefold()

    @classmethod
    def _has_exact_option(
        cls,
        cmdline: tuple[str, ...],
        option: str,
        expected: str,
        *,
        path_value: bool = False,
    ) -> bool:
        positions = [index for index, argument in enumerate(cmdline) if argument == option]
        if len(positions) != 1 or positions[0] + 1 >= len(cmdline):
            return False
        actual = cmdline[positions[0] + 1]
        return cls._same_path(actual, expected) if path_value else actual == expected

    def _matches_fixed_process(self, identity: ProcessIdentity) -> bool:
        return (
            self._same_path(identity.exe, str(QWEN_EXE))
            and bool(identity.cmdline)
            and self._same_path(identity.cmdline[0], str(QWEN_EXE))
            and self._has_exact_option(
                identity.cmdline, "--model", str(QWEN_MODEL), path_value=True
            )
            and self._has_exact_option(
                identity.cmdline, "--mmproj", str(QWEN_MMPROJ), path_value=True
            )
            and self._has_exact_option(identity.cmdline, "--host", QWEN_HOST)
            and self._has_exact_option(identity.cmdline, "--port", str(QWEN_PORT))
            and self._has_exact_option(identity.cmdline, "--alias", QWEN_ALIAS)
        )

    def _cleanup_launched(self, launched: ProcessIdentity) -> None:
        try:
            current = self._identity_lookup(launched.pid)
            if (
                current is not None
                and current.create_time == launched.create_time
                and self._matches_fixed_process(current)
            ):
                self._stopper(launched.pid)
        except (psutil.Error, OSError):
            self._remove_ownership_files()
            raise QwenProcessError("qwen_cleanup_failed") from None
        self._remove_ownership_files()

    def _remove_ownership_files(self) -> None:
        try:
            (self.runtime_dir / _OWNERSHIP_FILE).unlink(missing_ok=True)
            (self.runtime_dir / "owned-process.tmp").unlink(missing_ok=True)
        except OSError:
            raise QwenProcessError("qwen_ownership_cleanup_failed") from None

    def _write_ownership(self, identity: ProcessIdentity) -> None:
        payload = {
            "pid": identity.pid,
            "create_time": identity.create_time,
            "exe": str(QWEN_EXE),
            "model": str(QWEN_MODEL),
        }
        path = self.runtime_dir / _OWNERSHIP_FILE
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _fixed_args() -> tuple[str, ...]:
        return (
            str(QWEN_EXE),
            "--host",
            QWEN_HOST,
            "--port",
            str(QWEN_PORT),
            "--model",
            str(QWEN_MODEL),
            "--mmproj",
            str(QWEN_MMPROJ),
            "--alias",
            QWEN_ALIAS,
            "--ctx-size",
            "32768",
            "--n-predict",
            "2048",
            "--parallel",
            "1",
            "--jinja",
            "--reasoning",
            "off",
        )
