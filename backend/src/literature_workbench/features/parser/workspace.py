from __future__ import annotations

import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.library.import_files import TrustedNamedMutex
from literature_workbench.platform.windows.filesystem import (
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ,
    GENERIC_READ,
    HandleMetadata,
    INVALID_HANDLE_VALUE,
    Win32FileMetadataProvider,
    Win32OwnedFileHandle,
    _metadata_for_handle,
)


GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_WRITE = 0x00000002
CREATE_NEW = 1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_BEGIN = 0
FILE_RENAME_INFO_CLASS = 3
FILE_DISPOSITION_INFO_CLASS = 4


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateFileW = _kernel32.CreateFileW
_CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CreateFileW.restype = wintypes.HANDLE
_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL
_ReadFile = _kernel32.ReadFile
_ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_ReadFile.restype = wintypes.BOOL
_WriteFile = _kernel32.WriteFile
_WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_WriteFile.restype = wintypes.BOOL
_SetFilePointerEx = _kernel32.SetFilePointerEx
_SetFilePointerEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_longlong,
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.DWORD,
]
_SetFilePointerEx.restype = wintypes.BOOL
_SetEndOfFile = _kernel32.SetEndOfFile
_SetEndOfFile.argtypes = [wintypes.HANDLE]
_SetEndOfFile.restype = wintypes.BOOL
_FlushFileBuffers = _kernel32.FlushFileBuffers
_FlushFileBuffers.argtypes = [wintypes.HANDLE]
_FlushFileBuffers.restype = wintypes.BOOL
_SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
_SetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_SetFileInformationByHandle.restype = wintypes.BOOL


class UnsafeParserWorkspace(ValueError):
    pass


class WorkspaceMinerULease:
    def __init__(self, name: str, timeout_seconds: float) -> None:
        self._mutex = TrustedNamedMutex(name)
        self._timeout_seconds = timeout_seconds

    def __enter__(self) -> WorkspaceMinerULease:
        self._mutex.acquire(self._timeout_seconds)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._mutex.close()


class TrustedParserDirectory:
    def __init__(self, path: Path, handle: Win32OwnedFileHandle) -> None:
        self.path = Path(os.path.abspath(path))
        self._handle: Win32OwnedFileHandle | None = handle
        self._held_directories: tuple[Win32OwnedFileHandle, ...] = ()

    def metadata(self) -> HandleMetadata:
        if self._handle is None:
            raise ValueError("trusted_parser_directory_closed")
        metadata = self._handle.current_metadata()
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or not _same_path(metadata.final_path, self.path)
        ):
            raise UnsafeParserWorkspace("trusted_parser_directory_unsafe")
        return metadata

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        for directory in reversed(self._held_directories):
            directory.close()
        self._held_directories = ()

    def hold_parent(self, parent: Win32OwnedFileHandle) -> None:
        self._held_directories += (parent,)

    def rename(
        self,
        destination: Path,
        held_destination_parent: Win32OwnedFileHandle,
    ) -> None:
        before = self.metadata()
        if self._handle is None:
            raise ValueError("trusted_parser_directory_closed")
        raw_handle = self._handle.borrow_native_handle()
        destination = Path(os.path.abspath(destination))
        _rename_handle(raw_handle, destination)
        moved = self._handle.current_metadata()
        if (
            moved.is_reparse_point
            or not _same_path(moved.final_path, destination)
            or (moved.volume_id, moved.file_id)
            != (before.volume_id, before.file_id)
        ):
            raise UnsafeParserWorkspace("mineru_result_identity_changed")
        self.path = destination
        self._held_directories += (held_destination_parent,)


class _FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", wintypes.BOOLEAN),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


def _raise_last_error(operation: str) -> None:
    code = ctypes.get_last_error()
    raise OSError(code, f"{operation} failed", None, code)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _extended_windows_path(path: Path) -> str:
    """Return a Win32 long-path spelling without changing the trusted identity."""
    raw = os.path.abspath(path)
    if raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _promotion_journal_payload(
    content_id: ContentId,
    run_id: UUID,
    manifest_digest: str,
    state: str,
) -> bytes:
    if state not in {
        "copying",
        "promoted",
        "output_recorded",
        "db_completed",
        "published",
    }:
        raise ValueError("invalid_mineru_promotion_state")
    return json.dumps(
        {
            "content_id": str(content_id),
            "run_id": str(run_id),
            "manifest_digest": manifest_digest,
            "state": state,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _rename_handle(handle: int, destination: Path, *, replace: bool = False) -> None:
    encoded = str(destination).encode("utf-16-le")
    offset = _FILE_RENAME_INFO.FileName.offset
    buffer = ctypes.create_string_buffer(offset + len(encoded) + 2)
    info = _FILE_RENAME_INFO.from_buffer(buffer)
    info.ReplaceIfExists = replace
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    if not _SetFileInformationByHandle(
        wintypes.HANDLE(handle),
        FILE_RENAME_INFO_CLASS,
        buffer,
        len(buffer),
    ):
        _raise_last_error("SetFileInformationByHandle(parser directory rename)")


def _flush_directory(path: Path) -> None:
    raw = _CreateFileW(
        str(path),
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle = ctypes.cast(raw, ctypes.c_void_p).value
    if handle is None or handle == INVALID_HANDLE_VALUE:
        _raise_last_error("CreateFileW(parser durable directory)")
    assert handle is not None
    try:
        metadata = _metadata_for_handle(int(handle))
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or not _same_path(metadata.final_path, path)
        ):
            raise UnsafeParserWorkspace("parser_durable_directory_unsafe")
        if not _FlushFileBuffers(wintypes.HANDLE(handle)):
            _raise_last_error("FlushFileBuffers(parser directory)")
    finally:
        _CloseHandle(wintypes.HANDLE(handle))


class TrustedParserFile:
    def __init__(
        self,
        handle: int,
        expected_path: Path,
        held_directories: tuple[Win32OwnedFileHandle, ...],
    ) -> None:
        self._handle: int | None = handle
        self._expected_path = Path(os.path.abspath(expected_path))
        self._held_directories = held_directories
        self._owner_to_close: object | None = None

    @property
    def path(self) -> Path:
        return self._expected_path

    def metadata(self) -> HandleMetadata:
        metadata = _metadata_for_handle(self._require_handle())
        if (
            metadata.is_directory
            or metadata.is_reparse_point
            or not _same_path(metadata.final_path, self._expected_path)
        ):
            raise UnsafeParserWorkspace("trusted_parser_file_identity_changed")
        return metadata

    def current_metadata(self) -> HandleMetadata:
        return _metadata_for_handle(self._require_handle())

    def rebind_path(self, path: Path) -> None:
        self._expected_path = Path(os.path.abspath(path))
        self.metadata()

    def write(self, data: bytes) -> None:
        handle = self._require_handle()
        buffer = ctypes.create_string_buffer(data)
        written = wintypes.DWORD()
        if not _WriteFile(
            wintypes.HANDLE(handle), buffer, len(data), ctypes.byref(written), None
        ):
            _raise_last_error("WriteFile(parser workspace)")
        if int(written.value) != len(data):
            raise OSError("short_parser_workspace_write")

    def replace_bytes(self, data: bytes) -> None:
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx(parser workspace)")
        if not _SetEndOfFile(wintypes.HANDLE(handle)):
            _raise_last_error("SetEndOfFile(parser workspace)")
        self.write(data)
        self.flush()

    def flush(self) -> None:
        if not _FlushFileBuffers(wintypes.HANDLE(self._require_handle())):
            _raise_last_error("FlushFileBuffers(parser workspace)")

    def sha256(self, cancellation: object | None = None) -> str:
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx(parser workspace)")
        digest = hashlib.sha256()
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()  # type: ignore[attr-defined]
            buffer = ctypes.create_string_buffer(4 * 1024**2)
            read = wintypes.DWORD()
            if not _ReadFile(
                wintypes.HANDLE(handle),
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                _raise_last_error("ReadFile(parser workspace)")
            if read.value == 0:
                break
            digest.update(buffer.raw[: read.value])
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx(parser workspace)")
        return digest.hexdigest()

    def read_bounded(self, maximum_bytes: int) -> bytes:
        metadata = self.metadata()
        if metadata.size > maximum_bytes:
            raise ValueError("parser_workspace_file_too_large")
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx(parser workspace)")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            requested = min(1024 * 1024, remaining)
            buffer = ctypes.create_string_buffer(requested)
            read = wintypes.DWORD()
            if not _ReadFile(
                wintypes.HANDLE(handle),
                buffer,
                requested,
                ctypes.byref(read),
                None,
            ):
                _raise_last_error("ReadFile(parser workspace)")
            if read.value == 0:
                break
            chunks.append(buffer.raw[: read.value])
            remaining -= int(read.value)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise ValueError("parser_workspace_file_too_large")
        return payload

    def copy_to(
        self,
        destination: TrustedParserFile,
        cancellation: object | None = None,
    ) -> None:
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx(parser copy source)")
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()  # type: ignore[attr-defined]
            buffer = ctypes.create_string_buffer(4 * 1024**2)
            read = wintypes.DWORD()
            if not _ReadFile(
                wintypes.HANDLE(handle),
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                _raise_last_error("ReadFile(parser copy source)")
            if read.value == 0:
                break
            destination.write(buffer.raw[: read.value])
        destination.flush()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx(parser copy source)")

    def rename(self, destination: Path, *, replace: bool = False) -> None:
        destination = Path(os.path.abspath(destination))
        encoded = str(destination).encode("utf-16-le")
        offset = _FILE_RENAME_INFO.FileName.offset
        buffer = ctypes.create_string_buffer(offset + len(encoded) + 2)
        info = _FILE_RENAME_INFO.from_buffer(buffer)
        info.ReplaceIfExists = replace
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        if not _SetFileInformationByHandle(
            wintypes.HANDLE(self._require_handle()),
            FILE_RENAME_INFO_CLASS,
            buffer,
            len(buffer),
        ):
            _raise_last_error("SetFileInformationByHandle(parser rename)")
        self._expected_path = destination
        self.metadata()

    def delete(self) -> None:
        info = _FILE_DISPOSITION_INFO(True)
        if not _SetFileInformationByHandle(
            wintypes.HANDLE(self._require_handle()),
            FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _raise_last_error("SetFileInformationByHandle(parser delete)")

    def close(self) -> None:
        if self._handle is not None:
            _CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
        for directory in reversed(self._held_directories):
            directory.close()
        self._held_directories = ()
        if self._owner_to_close is not None:
            self._owner_to_close.close()  # type: ignore[attr-defined]
            self._owner_to_close = None

    def adopt_owner(self, owner: object) -> None:
        if self._owner_to_close is not None:
            raise ValueError("trusted_parser_file_already_has_owner")
        self._owner_to_close = owner

    def release_context(
        self,
    ) -> tuple[tuple[Win32OwnedFileHandle, ...], object | None]:
        if self._handle is not None:
            _CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
        directories = self._held_directories
        owner = self._owner_to_close
        self._held_directories = ()
        self._owner_to_close = None
        return directories, owner

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ValueError("trusted_parser_file_closed")
        return self._handle


@dataclass(frozen=True, slots=True)
class _ManifestFile:
    relative_path: str
    volume_id: str
    file_id: str
    size: int
    sha256: str
    handle: TrustedParserFile


@dataclass(frozen=True, slots=True)
class _ManifestDirectory:
    relative_path: str
    volume_id: str
    file_id: str
    handle: Win32OwnedFileHandle


class TrustedMinerUResult:
    def __init__(
        self,
        content_id: ContentId,
        run_id: UUID,
        directory: TrustedParserDirectory,
        directories: tuple[_ManifestDirectory, ...],
        files: tuple[_ManifestFile, ...],
        manifest: TrustedParserFile,
        manifest_digest: str,
        journal: TrustedParserFile | None = None,
    ) -> None:
        self.content_id = content_id
        self.run_id = run_id
        self.directory = directory
        self._directories = directories
        self._files = files
        self._manifest = manifest
        self.manifest_digest = manifest_digest
        self._journal = journal

    @property
    def path(self) -> Path:
        return self.directory.path

    def verify(self) -> None:
        root = self.directory.metadata()
        if root.is_reparse_point:
            raise UnsafeParserWorkspace("mineru_result_root_unsafe")
        if self._manifest.sha256() != self.manifest_digest:
            raise UnsafeParserWorkspace("mineru_result_manifest_changed")
        for directory_item in self._directories:
            metadata = directory_item.handle.current_metadata()
            expected = self.path.joinpath(*directory_item.relative_path.split("/"))
            if (
                not metadata.is_directory
                or metadata.is_reparse_point
                or not _same_path(metadata.final_path, expected)
                or (metadata.volume_id, metadata.file_id)
                != (directory_item.volume_id, directory_item.file_id)
            ):
                raise UnsafeParserWorkspace("mineru_result_directory_changed")
        for file_item in self._files:
            metadata = file_item.handle.metadata()
            if (
                metadata.hard_link_count != 1
                or (metadata.volume_id, metadata.file_id, metadata.size)
                != (file_item.volume_id, file_item.file_id, file_item.size)
                or file_item.handle.sha256() != file_item.sha256
            ):
                raise UnsafeParserWorkspace("mineru_result_file_changed")

    def rebind_after_promotion(self, destination: Path) -> None:
        for file_item in self._files:
            file_item.handle.rebind_path(
                destination.joinpath(*file_item.relative_path.split("/"))
            )
        self._manifest.rebind_path(destination / "manifest.json")
        self.verify()

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None
        self._manifest.close()
        for file_item in self._files:
            file_item.handle.close()
        for directory_item in reversed(self._directories):
            directory_item.handle.close()
        self.directory.close()

    def update_journal(self, state: str) -> None:
        if self._journal is None:
            raise UnsafeParserWorkspace("mineru_promotion_journal_missing")
        self._journal.replace_bytes(
            _promotion_journal_payload(
                self.content_id,
                self.run_id,
                self.manifest_digest,
                state,
            )
        )


class TrustedParserWorkspace:
    def __init__(
        self,
        data_root: Path,
        root: Win32OwnedFileHandle,
        staging: Win32OwnedFileHandle,
        mineru_work: Win32OwnedFileHandle,
        parse_results: Win32OwnedFileHandle,
    ) -> None:
        self.data_root = Path(os.path.abspath(data_root))
        self.staging_directory = self.data_root / "staging"
        self.mineru_work_directory = self.data_root / "mineru-work"
        self.parse_results_directory = self.data_root / "parse-results"
        self._root = root
        self._staging = staging
        self._mineru_work = mineru_work
        self._parse_results = parse_results
        identity = root.metadata
        self._mutex_namespace = hashlib.sha256(
            f"{identity.volume_id}:{identity.file_id}".encode("utf-8")
        ).hexdigest()[:24]

    @classmethod
    def create(cls, data_root: Path) -> TrustedParserWorkspace:
        root = Path(os.path.abspath(data_root))
        paths = (root, root / "staging", root / "mineru-work", root / "parse-results")
        provider = Win32FileMetadataProvider()
        handles: list[Win32OwnedFileHandle] = []
        try:
            for path in paths:
                path.mkdir(exist_ok=True)
                handle = provider.open_directory(path, share_delete=False)
                handles.append(handle)
                metadata = handle.metadata
                if (
                    not metadata.is_directory
                    or metadata.is_reparse_point
                    or not _same_path(metadata.final_path, path)
                ):
                    raise UnsafeParserWorkspace("trusted_parser_workspace_unsafe")
            return cls(root, *handles)
        except BaseException:
            for handle in reversed(handles):
                handle.close()
            raise

    def create_staging_file(
        self, content_id: ContentId, attempt_id: UUID
    ) -> TrustedParserFile:
        content = self._held_child_directory(
            self.staging_directory / str(content_id), require_new=False
        )
        path = content.metadata.final_path / f"{attempt_id}.part"
        return self._open_file(path, create=True, held_directories=(content,))

    def create_journal(
        self, content_id: ContentId, attempt_id: UUID
    ) -> TrustedParserFile:
        content = self._held_child_directory(
            self.staging_directory / str(content_id), require_new=False
        )
        path = content.metadata.final_path / f"{attempt_id}.promotion.json"
        return self._open_file(path, create=True, held_directories=(content,))

    def bind_staged_read_guard(
        self,
        writable: TrustedParserFile,
        expected_sha256: str,
    ) -> TrustedParserFile:
        before = writable.metadata()
        path = writable.path
        directories, owner = writable.release_context()
        guarded = self._open_file(
            path,
            create=False,
            held_directories=directories,
            writable=False,
        )
        if owner is not None:
            guarded.adopt_owner(owner)
        try:
            after = guarded.metadata()
            if (
                (after.volume_id, after.file_id, after.size)
                != (before.volume_id, before.file_id, before.size)
                or guarded.sha256() != expected_sha256
            ):
                raise UnsafeParserWorkspace("staged_identity_changed_during_guard_bind")
        except BaseException:
            guarded.close()
            raise
        return guarded

    def open_staging_file(
        self, content_id: ContentId, attempt_id: UUID, suffix: str
    ) -> TrustedParserFile:
        if suffix not in {".part", ".pdf", ".promotion.json"}:
            raise ValueError("invalid_staging_suffix")
        content = self._held_child_directory(
            self.staging_directory / str(content_id), require_new=False
        )
        path = content.metadata.final_path / f"{attempt_id}{suffix}"
        try:
            return self._open_file(path, create=False, held_directories=(content,))
        except BaseException:
            content.close()
            raise

    def journal_payload(
        self,
        content_id: ContentId,
        attempt_id: UUID,
        sha256: str,
        state: str,
    ) -> bytes:
        return json.dumps(
            {
                "attempt_id": str(attempt_id),
                "content_id": str(content_id),
                "sha256": sha256,
                "state": state,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def mineru_lease(self, *, timeout_seconds: float) -> WorkspaceMinerULease:
        if timeout_seconds <= 0:
            raise ValueError("mineru_lease_timeout_invalid")
        return WorkspaceMinerULease(
            f"Local\\LiteratureWorkbench.Parser.{self._mutex_namespace}.MinerU",
            timeout_seconds,
        )

    def create_mineru_work(self, run_id: UUID) -> TrustedParserDirectory:
        path = self.mineru_work_directory / str(run_id)
        handle = self._held_child_directory(
            path, require_new=True, allow_rename=True
        )
        return TrustedParserDirectory(path, handle)

    def open_mineru_work(self, run_id: UUID) -> TrustedParserDirectory:
        path = self.mineru_work_directory / str(run_id)
        handle = self._open_directory(path, allow_rename=True)
        work = TrustedParserDirectory(path, handle)
        work.metadata()
        return work

    def seal_mineru_work(
        self,
        content_id: ContentId,
        run_id: UUID,
        work: TrustedParserDirectory,
    ) -> TrustedMinerUResult:
        expected = self.mineru_work_directory / str(run_id)
        if not _same_path(work.path, expected):
            raise UnsafeParserWorkspace("mineru_work_path_mismatch")
        directories: tuple[_ManifestDirectory, ...] = ()
        files: tuple[_ManifestFile, ...] = ()
        manifest: TrustedParserFile | None = None
        try:
            directories, files = self._open_manifest_tree(work.path)
            payload = self._manifest_payload(content_id, run_id, files)
            manifest_path = work.path / "manifest.json"
            writable = self._open_file(
                manifest_path,
                create=True,
                held_directories=(),
            )
            writable.replace_bytes(payload)
            manifest_digest = hashlib.sha256(payload).hexdigest()
            manifest = self.bind_staged_read_guard(writable, manifest_digest)
            result = TrustedMinerUResult(
                content_id,
                run_id,
                work,
                directories,
                files,
                manifest,
                manifest_digest,
            )
            result.verify()
            return result
        except BaseException:
            if manifest is not None:
                manifest.close()
            for file_item in files:
                file_item.handle.close()
            for directory_item in reversed(directories):
                directory_item.handle.close()
            raise

    def promote_sealed_mineru_result(
        self,
        result: TrustedMinerUResult,
    ) -> TrustedMinerUResult:
        result.verify()
        content = self._held_child_directory(
            self.parse_results_directory / str(result.content_id),
            require_new=False,
        )
        destination = content.metadata.final_path / str(result.run_id)
        destination_directory: TrustedParserDirectory | None = None
        copied_directories: list[_ManifestDirectory] = []
        copied_files: list[_ManifestFile] = []
        manifest: TrustedParserFile | None = None
        journal: TrustedParserFile | None = None
        try:
            journal = self._open_file(
                content.metadata.final_path / f"{result.run_id}.promotion.json",
                create=True,
                held_directories=(),
            )
            journal.replace_bytes(
                _promotion_journal_payload(
                    result.content_id,
                    result.run_id,
                    result.manifest_digest,
                    "copying",
                )
            )
            destination_handle = self._held_child_directory(
                destination, require_new=True
            )
            destination_directory = TrustedParserDirectory(
                destination, destination_handle
            )
            destination_directory.hold_parent(content)
            for source_directory in sorted(
                result._directories,
                key=lambda item: (item.relative_path.count("/"), item.relative_path),
            ):
                path = destination.joinpath(
                    *source_directory.relative_path.split("/")
                )
                handle = self._held_child_directory(path, require_new=True)
                metadata = handle.current_metadata()
                copied_directories.append(
                    _ManifestDirectory(
                        source_directory.relative_path,
                        metadata.volume_id,
                        metadata.file_id,
                        handle,
                    )
                )
            for source_file in result._files:
                path = destination.joinpath(*source_file.relative_path.split("/"))
                writable = self._open_file(
                    path,
                    create=True,
                    held_directories=(),
                )
                source_file.handle.copy_to(writable)
                digest = writable.sha256()
                guarded = self.bind_staged_read_guard(writable, digest)
                metadata = guarded.metadata()
                if metadata.hard_link_count != 1:
                    guarded.close()
                    raise UnsafeParserWorkspace("mineru_result_hard_linked_file")
                copied_files.append(
                    _ManifestFile(
                        source_file.relative_path,
                        metadata.volume_id,
                        metadata.file_id,
                        metadata.size,
                        digest,
                        guarded,
                    )
                )
            payload = self._manifest_payload(
                result.content_id, result.run_id, copied_files
            )
            manifest_writer = self._open_file(
                destination / "manifest.json",
                create=True,
                held_directories=(),
            )
            manifest_writer.replace_bytes(payload)
            manifest_digest = hashlib.sha256(payload).hexdigest()
            manifest = self.bind_staged_read_guard(
                manifest_writer, manifest_digest
            )
            promoted = TrustedMinerUResult(
                result.content_id,
                result.run_id,
                destination_directory,
                tuple(copied_directories),
                tuple(copied_files),
                manifest,
                manifest_digest,
                journal,
            )
            promoted.verify()
            promoted.update_journal("promoted")
            journal = None
            result.close()
            return promoted
        except BaseException:
            if journal is not None:
                journal.close()
            if manifest is not None:
                manifest.close()
            for file_item in copied_files:
                file_item.handle.close()
            for directory_item in reversed(copied_directories):
                directory_item.handle.close()
            if destination_directory is not None:
                destination_directory.close()
            else:
                content.close()
            raise

    def promote_mineru_work(
        self,
        content_id: ContentId,
        run_id: UUID,
        work: TrustedParserDirectory,
    ) -> TrustedParserDirectory:
        content = self._held_child_directory(
            self.parse_results_directory / str(content_id), require_new=False
        )
        destination = content.metadata.final_path / str(run_id)
        try:
            work.rename(destination, content)
        except BaseException:
            content.close()
            raise
        return work

    def open_mineru_result(
        self, content_id: ContentId, run_id: UUID
    ) -> TrustedParserDirectory:
        content = self._held_child_directory(
            self.parse_results_directory / str(content_id), require_new=False
        )
        path = content.metadata.final_path / str(run_id)
        try:
            handle = Win32FileMetadataProvider().open_directory(
                path, share_delete=False
            )
            result = TrustedParserDirectory(path, handle)
            result.metadata()
            result.hold_parent(content)
            return result
        except BaseException:
            content.close()
            raise

    def open_sealed_mineru_result(
        self,
        content_id: ContentId,
        run_id: UUID,
    ) -> TrustedMinerUResult:
        directory = self.open_mineru_result(content_id, run_id)
        manifest: TrustedParserFile | None = None
        journal: TrustedParserFile | None = None
        directories: tuple[_ManifestDirectory, ...] = ()
        files: tuple[_ManifestFile, ...] = ()
        try:
            manifest = self._open_file(
                directory.path / "manifest.json",
                create=False,
                held_directories=(),
                writable=False,
            )
            payload = manifest.read_bounded(8 * 1024 * 1024)
            parsed = json.loads(payload.decode("utf-8"))
            directories, files = self._open_manifest_tree(directory.path)
            expected = json.loads(
                self._manifest_payload(content_id, run_id, files).decode("utf-8")
            )
            if parsed != expected:
                raise UnsafeParserWorkspace("mineru_result_manifest_mismatch")
            journal = self._open_file(
                directory.path.parent / f"{run_id}.promotion.json",
                create=False,
                held_directories=(),
            )
            journal_record = json.loads(
                journal.read_bounded(64 * 1024).decode("utf-8")
            )
            allowed_states = {
                "promoted",
                "output_recorded",
                "db_completed",
                "published",
            }
            manifest_digest = hashlib.sha256(payload).hexdigest()
            if journal_record not in [
                json.loads(
                    _promotion_journal_payload(
                        content_id, run_id, manifest_digest, state
                    ).decode("utf-8")
                )
                for state in allowed_states
            ]:
                raise UnsafeParserWorkspace("mineru_promotion_journal_mismatch")
            result = TrustedMinerUResult(
                content_id,
                run_id,
                directory,
                directories,
                files,
                manifest,
                manifest_digest,
                journal,
            )
            journal = None
            result.verify()
            return result
        except BaseException:
            if journal is not None:
                journal.close()
            if manifest is not None:
                manifest.close()
            for file_item in files:
                file_item.handle.close()
            for directory_item in reversed(directories):
                directory_item.handle.close()
            directory.close()
            raise

    def publish_result_pointer(
        self,
        content_id: ContentId,
        run_id: UUID,
        result: TrustedMinerUResult,
    ) -> None:
        expected = self.parse_results_directory / str(content_id) / str(run_id)
        result.verify()
        metadata = result.directory.metadata()
        if not _same_path(result.path, expected) or metadata.is_reparse_point:
            raise UnsafeParserWorkspace("mineru_result_publish_identity_invalid")
        content = self._held_child_directory(expected.parent, require_new=False)
        temporary: TrustedParserFile | None = None
        try:
            temp_path = content.metadata.final_path / f"current.{run_id}.tmp"
            temporary = self._open_file(
                temp_path,
                create=True,
                held_directories=(content,),
            )
            relative = f"parse-results/{content_id}/{run_id}"
            temporary.replace_bytes(
                json.dumps(
                    {
                        "attempt_id": str(run_id),
                        "attempt_path": relative,
                        "path_digest": hashlib.sha256(
                            relative.encode("utf-8")
                        ).hexdigest(),
                        "manifest_digest": result.manifest_digest,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            temporary.rename(
                temporary.path.with_name("current.json"), replace=True
            )
            _flush_directory(temporary.path.parent)
            result.update_journal("published")
        except BaseException:
            if temporary is not None:
                try:
                    temporary.delete()
                except OSError:
                    pass
            raise
        finally:
            if temporary is not None:
                temporary.close()

    def _open_manifest_tree(
        self,
        root: Path,
    ) -> tuple[tuple[_ManifestDirectory, ...], tuple[_ManifestFile, ...]]:
        directories: list[_ManifestDirectory] = []
        files: list[_ManifestFile] = []

        def visit(current: Path, relative_parent: str) -> None:
            for entry in sorted(
                os.scandir(_extended_windows_path(current)), key=lambda item: item.name
            ):
                relative = (
                    entry.name
                    if not relative_parent
                    else f"{relative_parent}/{entry.name}"
                )
                if relative == "manifest.json":
                    continue
                path = current / entry.name
                if entry.is_symlink():
                    raise UnsafeParserWorkspace("mineru_result_reparse_point")
                if entry.is_dir(follow_symlinks=False):
                    handle = Win32FileMetadataProvider().open_directory(
                        path, share_delete=False
                    )
                    metadata = handle.current_metadata()
                    if (
                        not metadata.is_directory
                        or metadata.is_reparse_point
                        or not _same_path(metadata.final_path, path)
                    ):
                        handle.close()
                        raise UnsafeParserWorkspace("mineru_result_directory_unsafe")
                    directories.append(
                        _ManifestDirectory(
                            relative,
                            metadata.volume_id,
                            metadata.file_id,
                            handle,
                        )
                    )
                    visit(path, relative)
                    continue
                file = self._open_file(
                    path,
                    create=False,
                    held_directories=(),
                    writable=False,
                )
                metadata = file.metadata()
                if metadata.hard_link_count != 1:
                    file.close()
                    raise UnsafeParserWorkspace("mineru_result_hard_linked_file")
                files.append(
                    _ManifestFile(
                        relative,
                        metadata.volume_id,
                        metadata.file_id,
                        metadata.size,
                        file.sha256(),
                        file,
                    )
                )

        try:
            visit(root, "")
            return tuple(directories), tuple(files)
        except BaseException:
            for file_item in files:
                file_item.handle.close()
            for directory_item in reversed(directories):
                directory_item.handle.close()
            raise

    @staticmethod
    def _manifest_payload(
        content_id: ContentId,
        run_id: UUID,
        files: tuple[_ManifestFile, ...] | list[_ManifestFile],
    ) -> bytes:
        payload: dict[str, Any] = {
            "content_id": str(content_id),
            "run_id": str(run_id),
            "files": [
                {
                    "path": item.relative_path,
                    "volume_id": item.volume_id,
                    "file_id": item.file_id,
                    "size": item.size,
                    "sha256": item.sha256,
                }
                for item in sorted(files, key=lambda value: value.relative_path)
            ],
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def cleanup_staged(
        self,
        guarded: TrustedParserFile,
        content_id: ContentId,
        attempt_id: UUID,
        expected_sha256: str,
    ) -> bool:
        directories: tuple[Win32OwnedFileHandle, ...] = ()
        owner: object | None = None
        journal: TrustedParserFile | None = None
        writable: TrustedParserFile | None = None
        try:
            before = guarded.metadata()
            if guarded.sha256() != expected_sha256:
                return False
            directories, owner = guarded.release_context()
            journal = self.open_staging_file(
                content_id, attempt_id, ".promotion.json"
            )
            record = json.loads(journal.read_bounded(64 * 1024).decode("utf-8"))
            if record != json.loads(
                self.journal_payload(
                    content_id, attempt_id, expected_sha256, "renamed"
                ).decode("utf-8")
            ):
                return False
            writable = self._open_file(
                guarded.path,
                create=False,
                held_directories=directories,
            )
            directories = ()
            after = writable.metadata()
            if (
                (after.volume_id, after.file_id, after.size)
                != (before.volume_id, before.file_id, before.size)
                or writable.sha256() != expected_sha256
            ):
                return False
            writable.delete()
            journal.delete()
            return True
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            return False
        finally:
            guarded.close()
            if writable is not None:
                writable.close()
            if journal is not None:
                journal.close()
            for directory in reversed(directories):
                directory.close()
            if owner is not None:
                owner.close()  # type: ignore[attr-defined]

    def _held_child_directory(
        self,
        path: Path,
        *,
        require_new: bool,
        allow_rename: bool = False,
    ) -> Win32OwnedFileHandle:
        path = Path(os.path.abspath(path))
        mkdir_path = Path(_extended_windows_path(path))
        if require_new:
            mkdir_path.mkdir()
        else:
            mkdir_path.mkdir(exist_ok=True)
        handle = self._open_directory(path, allow_rename=allow_rename)
        metadata = handle.metadata
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or not _same_path(metadata.final_path, path)
        ):
            handle.close()
            raise UnsafeParserWorkspace("trusted_parser_directory_unsafe")
        return handle

    @staticmethod
    def _open_directory(
        path: Path,
        *,
        allow_rename: bool,
    ) -> Win32OwnedFileHandle:
        if not allow_rename:
            return Win32FileMetadataProvider().open_directory(
                Path(_extended_windows_path(path)), share_delete=False
            )
        raw = _CreateFileW(
            _extended_windows_path(path),
            FILE_LIST_DIRECTORY | DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle = ctypes.cast(raw, ctypes.c_void_p).value
        if handle is None or handle == INVALID_HANDLE_VALUE:
            _raise_last_error("CreateFileW(parser renamable directory)")
        assert handle is not None
        native_handle = int(handle)
        try:
            metadata = _metadata_for_handle(native_handle)
            return Win32OwnedFileHandle(native_handle, metadata)
        except BaseException:
            _CloseHandle(wintypes.HANDLE(native_handle))
            raise

    @staticmethod
    def _open_file(
        path: Path,
        *,
        create: bool,
        held_directories: tuple[Win32OwnedFileHandle, ...],
        writable: bool = True,
    ) -> TrustedParserFile:
        access = GENERIC_READ
        if writable:
            access |= GENERIC_WRITE | DELETE
        raw = _CreateFileW(
            _extended_windows_path(path),
            access,
            FILE_SHARE_READ,
            None,
            CREATE_NEW if create else OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle = ctypes.cast(raw, ctypes.c_void_p).value
        if handle is None or handle == INVALID_HANDLE_VALUE:
            _raise_last_error("CreateFileW(parser workspace)")
        assert handle is not None
        file = TrustedParserFile(int(handle), path, held_directories)
        try:
            file.metadata()
        except BaseException:
            file.close()
            raise
        return file

    def close(self) -> None:
        for handle in (
            self._parse_results,
            self._mineru_work,
            self._staging,
            self._root,
        ):
            handle.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def open_trusted_relative_file(
    root: Path,
    relative_parts: tuple[str, ...],
) -> TrustedParserFile:
    if not relative_parts or any(
        not part or part in {".", ".."} or "/" in part or "\\" in part
        for part in relative_parts
    ):
        raise UnsafeParserWorkspace("invalid_parser_relative_path")
    root = Path(os.path.abspath(root))
    provider = Win32FileMetadataProvider()
    handles: list[Win32OwnedFileHandle] = []
    try:
        current = root
        for index, component in enumerate(("", *relative_parts[:-1])):
            if component:
                current /= component
            # The composition-owned work/result root may itself carry DELETE
            # access for a later handle-bound rename while denying delete-share.
            # A second read handle must therefore share that existing DELETE
            # access; nested components are then pinned without delete-share.
            handle = provider.open_directory(
                Path(_extended_windows_path(current)), share_delete=index == 0
            )
            handles.append(handle)
            metadata = handle.metadata
            if (
                not metadata.is_directory
                or metadata.is_reparse_point
                or not _same_path(metadata.final_path, current)
            ):
                raise UnsafeParserWorkspace("parser_result_directory_unsafe")
        path = current / relative_parts[-1]
        file = TrustedParserWorkspace._open_file(
            path,
            create=False,
            held_directories=tuple(handles),
            writable=False,
        )
        handles = []
        return file
    except BaseException:
        for handle in reversed(handles):
            handle.close()
        raise
