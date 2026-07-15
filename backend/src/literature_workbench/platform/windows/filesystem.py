from __future__ import annotations

import ctypes
import msvcrt
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO, NoReturn, Protocol
from uuid import UUID

from literature_workbench.features.library.models import DocumentRecord, LibraryRoot


FILE_READ_ATTRIBUTES = 0x0080
FILE_LIST_DIRECTORY = 0x0001
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_BEGIN = 0
FILE_BASIC_INFO_CLASS = 0
FILE_STANDARD_INFO_CLASS = 1
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_ID_INFO_CLASS = 18
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
WINDOWS_EPOCH_100NS = 116_444_736_000_000_000


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    ]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


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
_GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
_GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_GetFileInformationByHandleEx.restype = wintypes.BOOL
_GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
_GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
]
_GetFinalPathNameByHandleW.restype = wintypes.DWORD
_ReadFile = _kernel32.ReadFile
_ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_ReadFile.restype = wintypes.BOOL
_SetFilePointerEx = _kernel32.SetFilePointerEx
_SetFilePointerEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_longlong,
    ctypes.POINTER(ctypes.c_longlong),
    wintypes.DWORD,
]
_SetFilePointerEx.restype = wintypes.BOOL


class UnsafePath(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RootIdentity:
    canonical_path: Path
    volume_id: str
    file_id: str


@dataclass(frozen=True, slots=True)
class FileIdentity:
    canonical_path: Path
    root_id: UUID
    volume_id: str
    file_id: str
    size: int
    modified_ns: int
    hard_link_count: int
    is_reparse_point: bool

    @property
    def auto_cloud_eligible(self) -> bool:
        return self.hard_link_count == 1


@dataclass(slots=True)
class VerifiedHandle:
    identity: FileIdentity
    stream: BinaryIO
    identity_reader: Callable[[], FileIdentity] | None = None

    def current_identity(self) -> FileIdentity:
        if self.identity_reader is None:
            return self.identity
        return self.identity_reader()

    def close(self) -> None:
        self.stream.close()


@dataclass(frozen=True, slots=True)
class HandleMetadata:
    final_path: Path
    volume_id: str
    file_id: str
    is_directory: bool
    is_reparse_point: bool
    size: int
    modified_ns: int
    hard_link_count: int


class OwnedFileHandle(Protocol):
    metadata: HandleMetadata

    def read_prefix(self, length: int) -> bytes: ...

    def detach_stream(self) -> BinaryIO: ...

    def close(self) -> None: ...


class FileMetadataProvider(Protocol):
    def open_directory(self, path: Path) -> OwnedFileHandle: ...

    def open_file(self, path: Path) -> OwnedFileHandle: ...


class SafeFileInspector(Protocol):
    def inspect_root_directory(self, candidate: Path) -> RootIdentity: ...

    def inspect_directory_under_root(
        self, candidate: Path, root: LibraryRoot
    ) -> RootIdentity: ...

    def inspect_under_root(self, candidate: Path, root: LibraryRoot) -> FileIdentity: ...

    def inspect_regular_file(self, candidate: Path, root: LibraryRoot) -> FileIdentity: ...

    def open_registered_pdf(self, document: DocumentRecord) -> VerifiedHandle: ...


def _raise_last_error(operation: str) -> NoReturn:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed", None, error)


def _query(handle: int, info_class: int, value: ctypes.Structure) -> None:
    if not _GetFileInformationByHandleEx(
        wintypes.HANDLE(handle),
        info_class,
        ctypes.byref(value),
        ctypes.sizeof(value),
    ):
        _raise_last_error("GetFileInformationByHandleEx")


def _normalize_final_path(raw: str) -> Path:
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return Path(raw)


def _final_path(handle: int) -> Path:
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        result = _GetFinalPathNameByHandleW(wintypes.HANDLE(handle), buffer, size, 0)
        if result == 0:
            _raise_last_error("GetFinalPathNameByHandleW")
        if result < size:
            return _normalize_final_path(buffer.value)
        size = result + 1


def _metadata_for_handle(handle: int) -> HandleMetadata:
    identity = _FILE_ID_INFO()
    standard = _FILE_STANDARD_INFO()
    basic = _FILE_BASIC_INFO()
    tag = _FILE_ATTRIBUTE_TAG_INFO()
    _query(handle, FILE_ID_INFO_CLASS, identity)
    _query(handle, FILE_STANDARD_INFO_CLASS, standard)
    _query(handle, FILE_BASIC_INFO_CLASS, basic)
    _query(handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, tag)
    attributes = int(tag.FileAttributes)
    is_directory = bool(standard.Directory) or bool(attributes & FILE_ATTRIBUTE_DIRECTORY)
    is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) or int(tag.ReparseTag) != 0
    modified_ns = (int(basic.LastWriteTime) - WINDOWS_EPOCH_100NS) * 100
    return HandleMetadata(
        final_path=_final_path(handle),
        volume_id=f"{int(identity.VolumeSerialNumber):016x}",
        file_id=bytes(identity.FileId.Identifier).hex(),
        is_directory=is_directory,
        is_reparse_point=is_reparse,
        size=int(standard.EndOfFile),
        modified_ns=modified_ns,
        hard_link_count=int(standard.NumberOfLinks),
    )


class Win32OwnedFileHandle:
    def __init__(self, handle: int, metadata: HandleMetadata) -> None:
        self._handle: int | None = handle
        self.metadata = metadata

    def read_prefix(self, length: int) -> bytes:
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx")
        buffer = ctypes.create_string_buffer(length)
        read = wintypes.DWORD()
        if not _ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            length,
            ctypes.byref(read),
            None,
        ):
            _raise_last_error("ReadFile")
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx")
        return buffer.raw[: read.value]

    def detach_stream(self) -> BinaryIO:
        handle = self._require_handle()
        file_descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        self._handle = None
        try:
            return os.fdopen(file_descriptor, "rb", closefd=True)
        except BaseException:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            raise

    def close(self) -> None:
        if self._handle is not None:
            _CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ValueError("handle_is_closed")
        return self._handle


class Win32FileMetadataProvider:
    def open_directory(
        self,
        path: Path,
        *,
        share_delete: bool = True,
    ) -> Win32OwnedFileHandle:
        return self._open(
            path,
            access=FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY,
            flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            share_delete=share_delete,
        )

    def open_file(
        self,
        path: Path,
        *,
        share_delete: bool = True,
    ) -> Win32OwnedFileHandle:
        return self._open(
            path,
            access=GENERIC_READ,
            flags=FILE_FLAG_OPEN_REPARSE_POINT,
            share_delete=share_delete,
        )

    @staticmethod
    def _open(
        path: Path,
        *,
        access: int,
        flags: int,
        share_delete: bool,
    ) -> Win32OwnedFileHandle:
        share_mode = FILE_SHARE_READ | FILE_SHARE_WRITE
        if share_delete:
            share_mode |= FILE_SHARE_DELETE
        raw_handle = _CreateFileW(
            str(path),
            access,
            share_mode,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        handle = ctypes.cast(raw_handle, ctypes.c_void_p).value
        if handle is None or handle == INVALID_HANDLE_VALUE:
            _raise_last_error("CreateFileW")
        owned_handle = int(handle)
        try:
            metadata = _metadata_for_handle(owned_handle)
        except BaseException:
            _CloseHandle(wintypes.HANDLE(owned_handle))
            raise
        return Win32OwnedFileHandle(owned_handle, metadata)


class SafeWindowsFileInspector:
    def __init__(self, provider: FileMetadataProvider | None = None) -> None:
        self._provider = provider or Win32FileMetadataProvider()

    def inspect_root_directory(self, candidate: Path) -> RootIdentity:
        handle = self._provider.open_directory(Path(os.path.abspath(candidate)))
        try:
            metadata = handle.metadata
            if not metadata.is_directory:
                raise UnsafePath("not_directory")
            if metadata.is_reparse_point:
                raise UnsafePath("reparse_point")
            return RootIdentity(
                canonical_path=metadata.final_path,
                volume_id=metadata.volume_id,
                file_id=metadata.file_id,
            )
        finally:
            handle.close()

    def inspect_under_root(self, candidate: Path, root: LibraryRoot) -> FileIdentity:
        identity, handle = self._inspect_candidate(candidate, root, require_pdf=True)
        handle.close()
        return identity

    def inspect_directory_under_root(
        self, candidate: Path, root: LibraryRoot
    ) -> RootIdentity:
        candidate = Path(os.path.abspath(candidate))
        root_path = Path(os.path.abspath(root.canonical_path))
        self._require_lexically_contained(candidate, root_path)
        root_handle = self._provider.open_directory(root_path)
        try:
            root_final = self._require_registered_root(root_handle.metadata, root)
            relative = Path(os.path.relpath(candidate, root_path))
            current = root_path
            final_metadata = root_handle.metadata
            for component in (() if str(relative) == "." else relative.parts):
                current /= component
                component_handle = self._provider.open_directory(current)
                try:
                    final_metadata = component_handle.metadata
                    if not final_metadata.is_directory:
                        raise UnsafePath("intermediate_not_directory")
                    if final_metadata.is_reparse_point:
                        raise UnsafePath("reparse_point")
                    if not self._is_contained(final_metadata.final_path, root_final):
                        raise UnsafePath("outside_registered_root")
                finally:
                    component_handle.close()
            second_root = self._provider.open_directory(root_path)
            try:
                second_final = self._require_registered_root(second_root.metadata, root)
                if not self._same_path(second_final, root_final):
                    raise UnsafePath("root_identity_changed")
            finally:
                second_root.close()
            return RootIdentity(
                canonical_path=final_metadata.final_path,
                volume_id=final_metadata.volume_id,
                file_id=final_metadata.file_id,
            )
        finally:
            root_handle.close()

    def inspect_regular_file(self, candidate: Path, root: LibraryRoot) -> FileIdentity:
        identity, handle = self._inspect_candidate(candidate, root, require_pdf=False)
        handle.close()
        return identity

    def open_registered_pdf(self, document: DocumentRecord) -> VerifiedHandle:
        identity, handle = self._inspect_candidate(
            document.path, document.library_root, require_pdf=True
        )
        if (identity.volume_id, identity.file_id) != (document.volume_id, document.file_id):
            handle.close()
            raise UnsafePath("file_identity_changed")
        try:
            stream = handle.detach_stream()
        except BaseException:
            handle.close()
            raise
        def current_identity() -> FileIdentity:
            raw_handle = msvcrt.get_osfhandle(stream.fileno())
            metadata = _metadata_for_handle(raw_handle)
            return self._identity_from_metadata(metadata, document.library_root)

        return VerifiedHandle(
            identity=identity,
            stream=stream,
            identity_reader=current_identity,
        )

    def _inspect_candidate(
        self,
        candidate: Path,
        root: LibraryRoot,
        *,
        require_pdf: bool,
    ) -> tuple[FileIdentity, OwnedFileHandle]:
        candidate = Path(os.path.abspath(candidate))
        root_path = Path(os.path.abspath(root.canonical_path))
        if require_pdf and candidate.suffix.casefold() != ".pdf":
            raise UnsafePath("not_pdf")
        self._require_lexically_contained(candidate, root_path)

        root_handle = self._provider.open_directory(root_path)
        file_handle: OwnedFileHandle | None = None
        try:
            root_final = self._require_registered_root(root_handle.metadata, root)
            relative = Path(os.path.relpath(candidate, root_path))
            current = root_path
            for component in relative.parts[:-1]:
                current /= component
                component_handle = self._provider.open_directory(current)
                try:
                    metadata = component_handle.metadata
                    if not metadata.is_directory:
                        raise UnsafePath("intermediate_not_directory")
                    if metadata.is_reparse_point:
                        raise UnsafePath("reparse_point")
                finally:
                    component_handle.close()

            file_handle = self._provider.open_file(candidate)
            metadata = file_handle.metadata
            if metadata.is_directory:
                raise UnsafePath("not_regular_file")
            if metadata.is_reparse_point:
                raise UnsafePath("reparse_point")
            if not self._is_contained(metadata.final_path, root_final):
                raise UnsafePath("outside_registered_root")
            if require_pdf and file_handle.read_prefix(5) != b"%PDF-":
                raise UnsafePath("invalid_pdf_header")

            second_root = self._provider.open_directory(root_path)
            try:
                second_final = self._require_registered_root(second_root.metadata, root)
                if not self._same_path(second_final, root_final):
                    raise UnsafePath("root_identity_changed")
            finally:
                second_root.close()

            identity = self._identity_from_metadata(metadata, root)
            owned_handle = file_handle
            file_handle = None
            return identity, owned_handle
        finally:
            if file_handle is not None:
                file_handle.close()
            root_handle.close()

    @staticmethod
    def _identity_from_metadata(metadata: HandleMetadata, root: LibraryRoot) -> FileIdentity:
        return FileIdentity(
            canonical_path=metadata.final_path,
            root_id=root.id,
            volume_id=metadata.volume_id,
            file_id=metadata.file_id,
            size=metadata.size,
            modified_ns=metadata.modified_ns,
            hard_link_count=metadata.hard_link_count,
            is_reparse_point=metadata.is_reparse_point,
        )

    @staticmethod
    def _require_registered_root(metadata: HandleMetadata, root: LibraryRoot) -> Path:
        if not metadata.is_directory:
            raise UnsafePath("root_not_directory")
        if metadata.is_reparse_point:
            raise UnsafePath("root_reparse_point")
        if (metadata.volume_id, metadata.file_id) != (root.volume_id, root.file_id):
            raise UnsafePath("root_identity_changed")
        return metadata.final_path

    @classmethod
    def _require_lexically_contained(cls, candidate: Path, root: Path) -> None:
        if not cls._is_contained(candidate, root):
            raise UnsafePath("outside_registered_root")

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))

    @staticmethod
    def _is_contained(candidate: Path, root: Path) -> bool:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        root_key = os.path.normcase(os.path.abspath(root))
        try:
            return os.path.commonpath((candidate_key, root_key)) == root_key
        except ValueError:
            return False
