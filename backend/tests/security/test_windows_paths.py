from __future__ import annotations

import io
import re
from collections import defaultdict, deque
from pathlib import Path
from uuid import uuid4

import pytest

from literature_workbench.features.library.models import DocumentRecord, LibraryRoot
from literature_workbench.platform.windows import filesystem as windows_filesystem
from literature_workbench.platform.windows.filesystem import (
    HandleMetadata,
    SafeWindowsFileInspector,
    UnsafePath,
)


class FakeOwnedHandle:
    def __init__(self, metadata: HandleMetadata, content: bytes = b"") -> None:
        self.metadata = metadata
        self._content = content
        self.closed = False
        self.detached = False

    def read_prefix(self, length: int) -> bytes:
        return self._content[:length]

    def detach_stream(self) -> io.BytesIO:
        self.detached = True
        return io.BytesIO(self._content)

    def close(self) -> None:
        self.closed = True


class FakeMetadataProvider:
    def __init__(self) -> None:
        self._handles: dict[tuple[str, bool], deque[FakeOwnedHandle]] = defaultdict(deque)

    def queue(self, path: Path, handle: FakeOwnedHandle, *, directory: bool) -> None:
        self._handles[(str(path).casefold(), directory)].append(handle)

    def open_directory(self, path: Path) -> FakeOwnedHandle:
        return self._handles[(str(path).casefold(), True)].popleft()

    def open_file(self, path: Path) -> FakeOwnedHandle:
        return self._handles[(str(path).casefold(), False)].popleft()


def _metadata(
    path: Path,
    *,
    volume_id: str = "0000000000000011",
    file_id: str = "00000000000000000000000000000022",
    directory: bool = False,
    reparse: bool = False,
    links: int = 1,
) -> HandleMetadata:
    return HandleMetadata(
        final_path=path,
        volume_id=volume_id,
        file_id=file_id,
        is_directory=directory,
        is_reparse_point=reparse,
        size=123,
        modified_ns=456,
        hard_link_count=links,
    )


def _root(path: Path) -> LibraryRoot:
    return LibraryRoot(
        id=uuid4(),
        canonical_path=path,
        volume_id="0000000000000011",
        file_id="000000000000000000000000000000aa",
        recursive=True,
        discovery_mode="monitored",
        authorization_source="native_picker",
        status="active",
    )


def _queue_root(provider: FakeMetadataProvider, root: LibraryRoot, *, post_file_id: str | None = None) -> None:
    provider.queue(
        root.canonical_path,
        FakeOwnedHandle(
            _metadata(
                root.canonical_path,
                file_id=root.file_id,
                directory=True,
            )
        ),
        directory=True,
    )
    provider.queue(
        root.canonical_path,
        FakeOwnedHandle(
            _metadata(
                root.canonical_path,
                file_id=post_file_id or root.file_id,
                directory=True,
            )
        ),
        directory=True,
    )


@pytest.mark.parametrize("condition", ["child_symlink", "junction", "file_reparse", "outside_root"])
def test_inspector_rejects_escape_conditions(condition: str) -> None:
    provider = FakeMetadataProvider()
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\sub\paper.pdf")
    inspector = SafeWindowsFileInspector(provider)

    if condition == "outside_root":
        candidate = Path(r"C:\outside\paper.pdf")
    else:
        _queue_root(provider, root)
        provider.queue(
            Path(r"C:\library\sub"),
            FakeOwnedHandle(
                _metadata(
                    Path(r"C:\library\sub"),
                    directory=True,
                    reparse=condition in {"child_symlink", "junction"},
                )
            ),
            directory=True,
        )
        provider.queue(
            candidate,
            FakeOwnedHandle(
                _metadata(candidate, reparse=condition == "file_reparse"),
                b"%PDF-valid",
            ),
            directory=False,
        )

    with pytest.raises(UnsafePath):
        inspector.inspect_under_root(candidate, root)


def test_final_handle_path_catches_intermediate_component_swap() -> None:
    provider = FakeMetadataProvider()
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\sub\paper.pdf")
    _queue_root(provider, root)
    provider.queue(
        Path(r"C:\library\sub"),
        FakeOwnedHandle(_metadata(Path(r"C:\library\sub"), directory=True)),
        directory=True,
    )
    provider.queue(
        candidate,
        FakeOwnedHandle(_metadata(Path(r"D:\escaped\paper.pdf")), b"%PDF-valid"),
        directory=False,
    )

    with pytest.raises(UnsafePath, match="outside_registered_root"):
        SafeWindowsFileInspector(provider).inspect_under_root(candidate, root)


def test_inspector_rejects_registered_root_replacement() -> None:
    provider = FakeMetadataProvider()
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\paper.pdf")
    _queue_root(provider, root, post_file_id="000000000000000000000000000000bb")
    provider.queue(
        candidate,
        FakeOwnedHandle(_metadata(candidate), b"%PDF-valid"),
        directory=False,
    )

    with pytest.raises(UnsafePath, match="root_identity_changed"):
        SafeWindowsFileInspector(provider).inspect_under_root(candidate, root)


def test_open_rejects_file_swapped_since_document_registration() -> None:
    provider = FakeMetadataProvider()
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\paper.pdf")
    _queue_root(provider, root)
    provider.queue(
        candidate,
        FakeOwnedHandle(
            _metadata(candidate, file_id="00000000000000000000000000000099"),
            b"%PDF-valid",
        ),
        directory=False,
    )
    document = DocumentRecord(
        id=uuid4(),
        library_root=root,
        path=candidate,
        volume_id="0000000000000011",
        file_id="00000000000000000000000000000022",
        size_bytes=123,
        modified_ns=456,
    )

    with pytest.raises(UnsafePath, match="file_identity_changed"):
        SafeWindowsFileInspector(provider).open_registered_pdf(document)


def test_inspector_rejects_non_regular_file_and_bad_pdf_header() -> None:
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\paper.pdf")

    for metadata, content, code in [
        (_metadata(candidate, directory=True), b"%PDF-valid", "not_regular_file"),
        (_metadata(candidate), b"not a pdf", "invalid_pdf_header"),
    ]:
        provider = FakeMetadataProvider()
        _queue_root(provider, root)
        provider.queue(candidate, FakeOwnedHandle(metadata, content), directory=False)
        with pytest.raises(UnsafePath, match=code):
            SafeWindowsFileInspector(provider).inspect_under_root(candidate, root)


def test_hard_link_is_candidate_pdf_but_not_auto_cloud_eligible() -> None:
    provider = FakeMetadataProvider()
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\paper.PDF")
    _queue_root(provider, root)
    provider.queue(
        candidate,
        FakeOwnedHandle(_metadata(candidate, links=2), b"%PDF-valid"),
        directory=False,
    )

    identity = SafeWindowsFileInspector(provider).inspect_under_root(candidate, root)

    assert identity.hard_link_count == 2
    assert identity.auto_cloud_eligible is False


def test_open_registered_pdf_returns_owned_verified_stream() -> None:
    provider = FakeMetadataProvider()
    root = _root(Path(r"C:\library"))
    candidate = Path(r"C:\library\paper.pdf")
    _queue_root(provider, root)
    file_handle = FakeOwnedHandle(_metadata(candidate), b"%PDF-valid")
    provider.queue(candidate, file_handle, directory=False)
    document = DocumentRecord(
        id=uuid4(),
        library_root=root,
        path=candidate,
        volume_id="0000000000000011",
        file_id="00000000000000000000000000000022",
        size_bytes=123,
        modified_ns=456,
    )

    verified = SafeWindowsFileInspector(provider).open_registered_pdf(document)

    assert verified.stream.read(5) == b"%PDF-"
    assert file_handle.detached is True
    verified.close()
    assert verified.stream.closed is True


def test_root_inspection_rejects_reparse_and_uses_handle_final_path() -> None:
    provider = FakeMetadataProvider()
    selected = Path(r"C:\display\chosen")
    canonical = Path(r"C:\canonical\chosen")
    provider.queue(
        selected,
        FakeOwnedHandle(_metadata(canonical, directory=True)),
        directory=True,
    )

    identity = SafeWindowsFileInspector(provider).inspect_root_directory(selected)

    assert identity.canonical_path == canonical
    assert identity.volume_id == "0000000000000011"

    reparse_provider = FakeMetadataProvider()
    reparse_provider.queue(
        selected,
        FakeOwnedHandle(_metadata(selected, directory=True, reparse=True)),
        directory=True,
    )
    with pytest.raises(UnsafePath, match="reparse_point"):
        SafeWindowsFileInspector(reparse_provider).inspect_root_directory(selected)


def test_real_windows_handles_inspect_and_open_ordinary_pdf(tmp_path: Path) -> None:
    candidate = tmp_path / "ordinary.pdf"
    candidate.write_bytes(b"%PDF-real-temporary-test")
    inspector = SafeWindowsFileInspector()
    root_identity = inspector.inspect_root_directory(tmp_path)
    root = LibraryRoot(
        id=uuid4(),
        canonical_path=root_identity.canonical_path,
        volume_id=root_identity.volume_id,
        file_id=root_identity.file_id,
        recursive=True,
        discovery_mode="monitored",
        authorization_source="native_picker",
        status="active",
    )

    identity = inspector.inspect_under_root(candidate, root)
    document = DocumentRecord(
        id=uuid4(),
        library_root=root,
        path=identity.canonical_path,
        volume_id=identity.volume_id,
        file_id=identity.file_id,
        size_bytes=identity.size,
        modified_ns=identity.modified_ns,
    )
    verified = inspector.open_registered_pdf(document)
    try:
        assert verified.stream.read() == b"%PDF-real-temporary-test"
    finally:
        verified.close()

    assert re.fullmatch(r"[0-9a-f]{16}", root_identity.volume_id)
    assert re.fullmatch(r"[0-9a-f]{32}", root_identity.file_id)
    assert re.fullmatch(r"[0-9a-f]{16}", identity.volume_id)
    assert re.fullmatch(r"[0-9a-f]{32}", identity.file_id)


def test_detach_stream_closes_transferred_fd_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = windows_filesystem.Win32OwnedFileHandle(
        123,
        _metadata(Path(r"C:\library\paper.pdf")),
    )
    closed: list[int] = []
    monkeypatch.setattr(windows_filesystem.msvcrt, "open_osfhandle", lambda raw, flags: 77)

    def fail_fdopen(fd: int, mode: str, *, closefd: bool):
        raise RuntimeError("fdopen failed")

    monkeypatch.setattr(windows_filesystem.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(windows_filesystem.os, "close", closed.append)

    with pytest.raises(RuntimeError, match="fdopen failed"):
        handle.detach_stream()

    assert closed == [77]
    handle.close()
