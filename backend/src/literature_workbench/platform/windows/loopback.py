"""A reserved loopback listener and per-workspace, non-secret address preference."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from literature_workbench.platform.windows.paths import io_path, ordinary_path

DEFAULT_PORT = 58380
PREFERENCE_NAME = "address.json"


def workspace_key(root: Path) -> str:
    return hashlib.sha256(str(ordinary_path(root)).casefold().encode("utf-8")).hexdigest()


def _plain(path: Path) -> Path:
    absolute = ordinary_path(path)
    for part in [*reversed(absolute.parents), absolute]:
        checked = io_path(part)
        if checked.exists() or checked.is_symlink():
            info = checked.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise OSError("address_storage_unsafe")
    return io_path(absolute)


def preference_path(root: Path) -> Path:
    return root / "runtime" / "launcher" / PREFERENCE_NAME


def read_preference(root: Path) -> tuple[int | None, str]:
    try:
        path = _plain(preference_path(root))
        if not path.exists():
            return None, ""
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("address_storage_unsafe")
        with path.open("rb") as stream:
            encoded = stream.read(4097)
        if len(encoded) > 4096:
            return None, "address_record_reset"
        value = json.loads(encoded)
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("workspace_id") != workspace_key(root)
        ):
            return None, "address_record_reset"
        port = value.get("port")
        if type(port) is not int or not 1024 <= port <= 65535:
            return None, "address_record_reset"
        return port, ""
    except (ValueError, UnicodeError):
        return None, "address_record_reset"
    except OSError:
        return None, "address_record_unavailable"


def write_preference(root: Path, port: int) -> None:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("address_port_invalid")
    target = _plain(preference_path(root))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("address_storage_unsafe")
    temporary = _plain(target.with_name(f".address-{uuid4()}.tmp"))
    created = False
    try:
        with temporary.open("xb") as stream:
            created = True
            stream.write(
                json.dumps(
                    {"version": 1, "workspace_id": workspace_key(root), "port": port}
                ).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _plain(target))
    finally:
        if created:
            temporary.unlink(missing_ok=True)


@dataclass
class LoopbackBinding:
    root: Path
    listener: socket.socket
    previous_port: int | None
    warning: str
    remembered: bool = False

    @property
    def port(self) -> int:
        return int(self.listener.getsockname()[1])

    def remember(self) -> None:
        if self.previous_port == self.port:
            self.remembered = True
            return
        try:
            if self.warning == "address_record_unavailable":
                return
            write_preference(self.root, self.port)
            self.remembered = True
        except OSError:
            self.warning = "address_record_unavailable"

    def public(self) -> dict[str, object]:
        changed = self.previous_port is not None and self.previous_port != self.port
        return {
            "current_url": f"http://127.0.0.1:{self.port}/",
            "remembered": self.remembered,
            "previous_url": f"http://127.0.0.1:{self.previous_port}/" if changed else None,
            "address_changed": changed,
            "warning": self.warning,
        }


@contextmanager
def reserve_address(root: Path) -> Iterator[LoopbackBinding]:
    previous, warning = read_preference(root)
    listener: socket.socket | None = None
    for port in (previous or DEFAULT_PORT, 0):
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("127.0.0.1", port))
            candidate.set_inheritable(False)
            listener = candidate
            break
        except OSError:
            candidate.close()
    if listener is None:
        raise RuntimeError("loopback_port_unavailable")
    try:
        yield LoopbackBinding(root, listener, previous, warning)
    finally:
        listener.close()
