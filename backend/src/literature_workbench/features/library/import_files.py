from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from uuid import UUID

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
CREATE_NEW = 1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_BEGIN = 0
FILE_RENAME_INFO_CLASS = 3
FILE_DISPOSITION_INFO_CLASS = 4
HANDLE_FLAG_INHERIT = 0x00000001
CREATE_NO_WINDOW = 0x08000000
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004


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
_WriteFile = _kernel32.WriteFile
_WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_WriteFile.restype = wintypes.BOOL
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
_SetHandleInformation = _kernel32.SetHandleInformation
_SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
_SetHandleInformation.restype = wintypes.BOOL
_ReOpenFile = _kernel32.ReOpenFile
_ReOpenFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
_ReOpenFile.restype = wintypes.HANDLE
_CreateMutexW = _kernel32.CreateMutexW
_CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_CreateMutexW.restype = wintypes.HANDLE
_WaitForSingleObject = _kernel32.WaitForSingleObject
_WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_WaitForSingleObject.restype = wintypes.DWORD
_ReleaseMutex = _kernel32.ReleaseMutex
_ReleaseMutex.argtypes = [wintypes.HANDLE]
_ReleaseMutex.restype = wintypes.BOOL

WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


class MutexTimeout(TimeoutError):
    pass


class TrustedNamedMutex:
    def __init__(self, name: str) -> None:
        raw = _CreateMutexW(None, False, name)
        handle = ctypes.cast(raw, ctypes.c_void_p).value
        if handle is None or handle == INVALID_HANDLE_VALUE:
            _raise_last_error("CreateMutexW")
        if handle is None:
            raise OSError("invalid_mutex_handle")
        self._handle: int | None = int(handle)
        self._owned = False

    def acquire(self, timeout: float) -> None:
        if timeout < 0:
            raise ValueError("mutex_timeout_invalid")
        handle = self._require_handle()
        result = int(_WaitForSingleObject(wintypes.HANDLE(handle), int(timeout * 1000)))
        if result == WAIT_TIMEOUT:
            raise MutexTimeout("mutex_timeout")
        if result not in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            raise OSError(result, "WaitForSingleObject failed")
        self._owned = True

    def release(self) -> None:
        if not self._owned:
            return
        if not _ReleaseMutex(wintypes.HANDLE(self._require_handle())):
            _raise_last_error("ReleaseMutex")
        self._owned = False

    def close(self) -> None:
        try:
            self.release()
        except OSError as error:
            if getattr(error, "winerror", None) != 288:
                raise
            self._owned = False
        if self._handle is not None:
            _CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ValueError("mutex_closed")
        return self._handle


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
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


class TrustedImportFile:
    def __init__(
        self,
        owner: TrustedImportRoot,
        handle: int,
        held_directories: tuple[Win32OwnedFileHandle, ...] = (),
    ) -> None:
        self._owner = owner
        self._handle: int | None = handle
        self._held_directories = held_directories

    def metadata(self) -> HandleMetadata:
        metadata = _metadata_for_handle(self._require_handle())
        if metadata.is_directory or metadata.is_reparse_point:
            raise ValueError("trusted_import_file_unsafe")
        return metadata

    def write(self, data: bytes) -> None:
        handle = self._require_handle()
        for offset in range(0, len(data), 4 * 1024**2):
            chunk = data[offset : offset + 4 * 1024**2]
            buffer = ctypes.create_string_buffer(chunk)
            written = wintypes.DWORD()
            if not _WriteFile(
                wintypes.HANDLE(handle),
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                _raise_last_error("WriteFile")
            if int(written.value) != len(chunk):
                raise OSError("short_write")

    def flush(self) -> None:
        if not _FlushFileBuffers(wintypes.HANDLE(self._require_handle())):
            _raise_last_error("FlushFileBuffers")

    def sha256(self, cancellation: object | None = None) -> str:
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx")
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
                _raise_last_error("ReadFile")
            if read.value == 0:
                break
            digest.update(buffer.raw[: read.value])
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx")
        return digest.hexdigest()

    def read_prefix(self, length: int) -> bytes:
        handle = self._require_handle()
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx")
        buffer = ctypes.create_string_buffer(length)
        read = wintypes.DWORD()
        if not _ReadFile(
            wintypes.HANDLE(handle), buffer, length, ctypes.byref(read), None
        ):
            _raise_last_error("ReadFile")
        if not _SetFilePointerEx(wintypes.HANDLE(handle), 0, None, FILE_BEGIN):
            _raise_last_error("SetFilePointerEx")
        return buffer.raw[: read.value]

    def probe_pdf(self, *, timeout_seconds: float, cancellation: object | None = None) -> bool:
        handle = self._require_handle()
        if cancellation is not None:
            cancellation.raise_if_cancelled()  # type: ignore[attr-defined]
        raw_probe = _ReOpenFile(
            wintypes.HANDLE(handle),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            0,
        )
        probe_handle = ctypes.cast(raw_probe, ctypes.c_void_p).value
        if probe_handle is None or probe_handle == INVALID_HANDLE_VALUE:
            _raise_last_error("ReOpenFile(probe)")
        if probe_handle is None:
            raise OSError("invalid_probe_handle")
        if not _SetHandleInformation(
            wintypes.HANDLE(probe_handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
        ):
            _CloseHandle(wintypes.HANDLE(probe_handle))
            _raise_last_error("SetHandleInformation")
        probe = (
            "import ctypes,fitz,mmap,msvcrt,os,sys; h=int(sys.argv[1]); "
            "n=ctypes.c_ulong(); denied=not ctypes.windll.kernel32.WriteFile(h,b'x',1,ctypes.byref(n),None); "
            "fd=msvcrt.open_osfhandle(h,os.O_RDONLY|os.O_BINARY); "
            "m=mmap.mmap(fd,0,access=mmap.ACCESS_READ); v=memoryview(m); "
            "d=fitz.open(stream=v,filetype='pdf'); "
            "ok=(denied and not d.needs_pass and not d.is_repaired and d.page_count>0); "
            "d.load_page(0) if ok else None; d.close(); v.release(); m.close(); "
            "os.close(fd); raise SystemExit(0 if ok else 2)"
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [probe_handle]}
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", probe, str(probe_handle)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                startupinfo=startupinfo,
                creationflags=CREATE_NO_WINDOW,
            )
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()  # type: ignore[attr-defined]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    return False
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
            return process.returncode == 0
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if not _SetHandleInformation(
                wintypes.HANDLE(probe_handle), HANDLE_FLAG_INHERIT, 0
            ):
                _CloseHandle(wintypes.HANDLE(probe_handle))
                _raise_last_error("SetHandleInformation(clear)")
            _CloseHandle(wintypes.HANDLE(probe_handle))

    def rename_incoming(self, intent_id: UUID, suffix: str) -> None:
        parent = self._owner.open_durable_directory(self._owner.incoming_directory)
        try:
            self._rename(self._owner.incoming_path(intent_id, suffix), parent)
        finally:
            parent.close()

    def rename_final(self, year: str, month: str, file_name: str) -> None:
        directories = self._owner.final_directories(year, month)
        durable_parent: Win32OwnedFileHandle | None = None
        try:
            durable_parent = self._owner.open_durable_directory(
                directories[-1].metadata.final_path
            )
            self._rename(
                directories[-1].metadata.final_path / file_name, durable_parent
            )
        except BaseException:
            for directory in reversed(directories):
                directory.close()
            raise
        finally:
            if durable_parent is not None:
                durable_parent.close()
        self._held_directories += directories

    def _rename(
        self, destination: Path, durable_parent: Win32OwnedFileHandle
    ) -> None:
        self.flush()
        encoded = str(destination).encode("utf-16-le")
        offset = _FILE_RENAME_INFO.FileName.offset
        buffer = ctypes.create_string_buffer(offset + len(encoded) + 2)
        info = _FILE_RENAME_INFO.from_buffer(buffer)
        info.ReplaceIfExists = False
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        if not _SetFileInformationByHandle(
            wintypes.HANDLE(self._require_handle()),
            FILE_RENAME_INFO_CLASS,
            buffer,
            len(buffer),
        ):
            _raise_last_error("SetFileInformationByHandle(FileRenameInfo)")
        self.flush()
        if not _FlushFileBuffers(
            wintypes.HANDLE(durable_parent._require_handle())
        ):
            _raise_last_error("FlushFileBuffers(parent directory)")

    def delete(self) -> None:
        info = _FILE_DISPOSITION_INFO(True)
        if not _SetFileInformationByHandle(
            wintypes.HANDLE(self._require_handle()),
            FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _raise_last_error("SetFileInformationByHandle(FileDispositionInfo)")

    def close(self) -> None:
        if self._handle is not None:
            _CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
        for directory in reversed(self._held_directories):
            directory.close()
        self._held_directories = ()

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ValueError("trusted_import_handle_closed")
        return self._handle


class TrustedImportRoot:
    def __init__(
        self,
        data_root: Path,
        root_handle: Win32OwnedFileHandle,
        imports_handle: Win32OwnedFileHandle,
        incoming_handle: Win32OwnedFileHandle,
        library_handle: Win32OwnedFileHandle,
        locks_handle: Win32OwnedFileHandle,
    ) -> None:
        self.data_root = data_root
        self.imports_directory = data_root / "imports"
        self.incoming_directory = self.imports_directory / ".incoming"
        self.library_directory = self.imports_directory / "library"
        self.locks_directory = self.imports_directory / ".locks"
        self._root_handle = root_handle
        self._imports_handle = imports_handle
        self._incoming_handle = incoming_handle
        self._library_handle = library_handle
        self._locks_handle = locks_handle
        root_identity = root_handle.metadata
        self._mutex_namespace = hashlib.sha256(
            f"{root_identity.volume_id}:{root_identity.file_id}".encode("utf-8")
        ).hexdigest()[:24]

    @classmethod
    def create(cls, data_root: Path) -> TrustedImportRoot:
        data_root = Path(os.path.abspath(data_root))
        imports = data_root / "imports"
        incoming = imports / ".incoming"
        library = imports / "library"
        locks = imports / ".locks"
        provider = Win32FileMetadataProvider()
        handles: list[Win32OwnedFileHandle] = []
        try:
            for directory in (data_root, imports, incoming, library, locks):
                directory.mkdir(exist_ok=True)
                handle = provider.open_directory(directory, share_delete=False)
                handles.append(handle)
                metadata = handle.metadata
                if (
                    not metadata.is_directory
                    or metadata.is_reparse_point
                    or not _same_path(metadata.final_path, directory)
                ):
                    raise ValueError("trusted_import_root_unsafe")
            return cls(data_root, *handles)
        except BaseException:
            for handle in reversed(handles):
                handle.close()
            raise

    def incoming_path(self, intent_id: UUID, suffix: str) -> Path:
        return self.incoming_directory / f"{intent_id}.{suffix}"

    def mutex(self, key: str) -> TrustedNamedMutex:
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return TrustedNamedMutex(
            f"Local\\LiteratureWorkbench.Import.{self._mutex_namespace}.{key_digest}"
        )

    def open_durable_directory(self, path: Path) -> Win32OwnedFileHandle:
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
            _raise_last_error("CreateFileW(durable directory)")
        if handle is None:
            raise OSError("invalid_durable_directory_handle")
        metadata = _metadata_for_handle(int(handle))
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or not _same_path(metadata.final_path, path)
        ):
            _CloseHandle(wintypes.HANDLE(handle))
            raise ValueError("trusted_import_root_unsafe")
        return Win32OwnedFileHandle(int(handle), metadata)

    def create_incoming(self, intent_id: UUID) -> TrustedImportFile:
        return self._create_file(self.incoming_path(intent_id, "part"), create=True)

    def open_incoming(self, intent_id: UUID, suffix: str) -> TrustedImportFile:
        expected = self.incoming_path(intent_id, suffix)
        file = self._create_file(expected, create=False)
        if not _same_path(file.metadata().final_path, expected):
            file.close()
            raise ValueError("trusted_import_file_unsafe")
        return file

    def open_final(self, path: Path) -> TrustedImportFile:
        path = Path(os.path.abspath(path))
        try:
            path.relative_to(self.library_directory)
        except ValueError as error:
            raise ValueError("trusted_import_file_unsafe") from error
        relative = path.relative_to(self.library_directory)
        if len(relative.parts) != 3:
            raise ValueError("trusted_import_file_unsafe")
        directories = self.final_directories(relative.parts[0], relative.parts[1])
        try:
            file = self._create_file(path, create=False, held_directories=directories)
        except BaseException:
            for directory in reversed(directories):
                directory.close()
            raise
        if not _same_path(file.metadata().final_path, path):
            file.close()
            raise ValueError("trusted_import_file_unsafe")
        return file

    def final_directories(
        self, year: str, month: str
    ) -> tuple[Win32OwnedFileHandle, Win32OwnedFileHandle]:
        if len(year) != 4 or not year.isdigit() or len(month) != 2 or not month.isdigit():
            raise ValueError("invalid_final_directory")
        year_path = self.library_directory / year
        month_path = year_path / month
        year_path.mkdir(exist_ok=True)
        month_path.mkdir(exist_ok=True)
        provider = Win32FileMetadataProvider()
        year_handle = provider.open_directory(year_path, share_delete=False)
        try:
            if year_handle.metadata.is_reparse_point or not _same_path(
                year_handle.metadata.final_path, year_path
            ):
                raise ValueError("trusted_import_root_unsafe")
            month_handle = provider.open_directory(month_path, share_delete=False)
            if month_handle.metadata.is_reparse_point or not _same_path(
                month_handle.metadata.final_path, month_path
            ):
                month_handle.close()
                raise ValueError("trusted_import_root_unsafe")
            return year_handle, month_handle
        except BaseException:
            year_handle.close()
            raise

    def _create_file(
        self,
        path: Path,
        *,
        create: bool,
        held_directories: tuple[Win32OwnedFileHandle, ...] = (),
    ) -> TrustedImportFile:
        raw = _CreateFileW(
            str(path),
            GENERIC_READ | GENERIC_WRITE | DELETE,
            FILE_SHARE_READ,
            None,
            CREATE_NEW if create else OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle = ctypes.cast(raw, ctypes.c_void_p).value
        if handle is None or handle == INVALID_HANDLE_VALUE:
            _raise_last_error("CreateFileW(import)")
        if handle is None:
            raise OSError("invalid_import_handle")
        return TrustedImportFile(self, int(handle), held_directories)

    def close(self) -> None:
        for handle in (
            self._locks_handle,
            self._library_handle,
            self._incoming_handle,
            self._imports_handle,
            self._root_handle,
        ):
            handle.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
