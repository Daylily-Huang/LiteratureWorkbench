from __future__ import annotations

import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from pathlib import Path
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

    def publish_result_pointer(
        self,
        content_id: ContentId,
        run_id: UUID,
        result: TrustedParserDirectory,
    ) -> None:
        expected = self.parse_results_directory / str(content_id) / str(run_id)
        metadata = result.metadata()
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
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            temporary.rename(
                temporary.path.with_name("current.json"), replace=True
            )
            _flush_directory(temporary.path.parent)
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
        if require_new:
            path.mkdir()
        else:
            path.mkdir(exist_ok=True)
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
                path, share_delete=False
            )
        raw = _CreateFileW(
            str(path),
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
            str(path),
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
                current, share_delete=index == 0
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
