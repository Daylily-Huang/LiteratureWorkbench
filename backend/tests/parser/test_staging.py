from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import fitz
import pytest

from conftest import ParserLibraryFixture
from literature_workbench.features.library.models import ContentId, DocumentRecord
from literature_workbench.features.parser.models import StagedPdf
from literature_workbench.features.parser.staging import (
    AllSourcesUnavailable,
    StagedContentMismatch,
    cleanup_checkpointed_staging,
    open_current_source_for_content,
    persist_structure_validation,
    recover_checkpointed_staging,
    stage_verified_pdf,
    validate_staged_structure,
)
from literature_workbench.platform.windows.filesystem import (
    FileIdentity,
    SafeFileInspector,
    VerifiedHandle,
)


def _pdf_bytes(*, title: str = "", author: str = "", pages: int = 1) -> bytes:
    document = fitz.open()
    try:
        for _ in range(pages):
            page = document.new_page()
            page.insert_text((36, 72), "temporary parser fixture text " * 5)
        document.set_metadata({"title": title, "author": author})
        return document.tobytes()
    finally:
        document.close()


@dataclass(slots=True)
class CheckpointRecorder:
    events: list[tuple[str, object]] = field(default_factory=list)

    def record_copy_started(self, attempt_id: UUID) -> None:
        self.events.append(("started", attempt_id))

    def record_staged(self, relative_path: str, sha256: str) -> None:
        self.events.append(("staged", (relative_path, sha256)))


def test_source_is_rehashed_from_zero_and_complete_pdf_is_staged(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("论文/测试 文献.pdf", _pdf_bytes())

    class OffsetInspector:
        def __init__(self, delegate: SafeFileInspector) -> None:
            self.delegate = delegate

        def open_registered_pdf(self, document: DocumentRecord) -> VerifiedHandle:
            handle = self.delegate.open_registered_pdf(document)
            handle.stream.seek(17)
            return handle

    source = open_current_source_for_content(
        parser_library.database,
        OffsetInspector(parser_library.inspector),
        registered.content_id,
    )
    assert source.stream.tell() == 0
    checkpoint = CheckpointRecorder()
    attempt_id = uuid4()
    try:
        staged = stage_verified_pdf(
            parser_library.data_root,
            registered.content_id,
            source,
            attempt_id,
            checkpoint,
        )
    finally:
        source.close()

    assert staged.path.read_bytes() == registered.payload
    assert staged.path.name == f"{attempt_id}.pdf"
    assert staged.path.parent.name == registered.content_id
    assert checkpoint.events[0] == ("started", attempt_id)
    assert checkpoint.events[1][0] == "staged"


def test_changed_source_is_reconciled_and_creates_no_staging_bytes(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("changed.pdf", _pdf_bytes())
    registered.path.write_bytes(registered.payload + b"changed")
    reconciled: list[Path] = []

    with pytest.raises(AllSourcesUnavailable):
        open_current_source_for_content(
            parser_library.database,
            parser_library.inspector,
            registered.content_id,
            reconcile=lambda _library_id, path: reconciled.append(path),
        )

    assert reconciled == [registered.path]
    assert not (parser_library.data_root / "staging").exists()


def test_injected_hash_mismatch_never_promotes_part_file(tmp_path: Path) -> None:
    expected = ContentId(hashlib.sha256(b"expected").hexdigest())
    identity = FileIdentity(
        canonical_path=tmp_path / "source.pdf",
        root_id=uuid4(),
        volume_id="volume",
        file_id="file",
        size=9,
        modified_ns=1,
        hard_link_count=1,
        is_reparse_point=False,
    )
    source = VerifiedHandle(identity=identity, stream=BytesIO(b"different"))
    attempt_id = uuid4()

    with pytest.raises(StagedContentMismatch):
        stage_verified_pdf(tmp_path, expected, source, attempt_id, CheckpointRecorder())

    assert not (tmp_path / "staging" / expected / f"{attempt_id}.pdf").exists()


def test_interrupted_copy_leaves_only_exact_checkpointed_part(tmp_path: Path) -> None:
    class InterruptedStream(BytesIO):
        reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if self.reads > 1:
                raise OSError("injected interruption")
            return super().read(5)

    payload = b"%PDF-interrupted"
    content_id = ContentId(hashlib.sha256(payload).hexdigest())
    identity = FileIdentity(
        canonical_path=tmp_path / "source.pdf",
        root_id=uuid4(),
        volume_id="volume",
        file_id="file",
        size=len(payload),
        modified_ns=1,
        hard_link_count=1,
        is_reparse_point=False,
    )
    attempt_id = uuid4()

    with pytest.raises(OSError, match="injected interruption"):
        stage_verified_pdf(
            tmp_path,
            content_id,
            VerifiedHandle(identity, InterruptedStream(payload)),
            attempt_id,
            CheckpointRecorder(),
            chunk_size=5,
        )

    part = tmp_path / "staging" / content_id / f"{attempt_id}.part"
    assert part.read_bytes() == payload[:5]
    assert not part.with_suffix(".pdf").exists()


def test_recovery_removes_only_checkpointed_stale_attempt_then_new_attempt_succeeds(
    tmp_path: Path,
) -> None:
    payload = b"%PDF-retry"
    content_id = ContentId(hashlib.sha256(payload).hexdigest())
    stale_attempt = uuid4()
    directory = tmp_path / "staging" / content_id
    directory.mkdir(parents=True)
    stale = directory / f"{stale_attempt}.part"
    unrelated = directory / "unrelated.keep"
    stale.write_bytes(b"partial")
    unrelated.write_bytes(b"leave me")

    recovered = recover_checkpointed_staging(
        tmp_path,
        content_id,
        stale_attempt,
        f"staging/{content_id}/{stale_attempt}.part",
        content_id,
    )

    assert recovered is None
    assert not stale.exists()
    assert unrelated.read_bytes() == b"leave me"
    new_attempt = uuid4()
    identity = FileIdentity(
        canonical_path=tmp_path / "Unicode 原文.pdf",
        root_id=uuid4(),
        volume_id="volume",
        file_id="file",
        size=len(payload),
        modified_ns=1,
        hard_link_count=1,
        is_reparse_point=False,
    )
    staged = stage_verified_pdf(
        tmp_path,
        content_id,
        VerifiedHandle(identity, BytesIO(payload)),
        new_attempt,
        CheckpointRecorder(),
    )
    assert staged.path == directory / f"{new_attempt}.pdf"
    assert "Unicode 原文" not in str(staged.path)


def test_recovery_reuses_only_a_hash_valid_checkpointed_pdf(tmp_path: Path) -> None:
    payload = b"%PDF-complete"
    content_id = ContentId(hashlib.sha256(payload).hexdigest())
    attempt_id = uuid4()
    path = tmp_path / "staging" / content_id / f"{attempt_id}.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    recovered = recover_checkpointed_staging(
        tmp_path,
        content_id,
        attempt_id,
        f"staging/{content_id}/{attempt_id}.pdf",
        content_id,
    )

    assert recovered == StagedPdf(content_id, attempt_id, path, content_id, None)


def test_cleanup_rejects_nonmatching_parent_content_or_attempt(tmp_path: Path) -> None:
    content_id = ContentId("a" * 64)
    attempt_id = uuid4()
    unrelated = tmp_path / "outside.pdf"
    unrelated.write_bytes(b"keep")

    assert not cleanup_checkpointed_staging(
        tmp_path,
        content_id,
        attempt_id,
        "outside.pdf",
    )
    assert unrelated.read_bytes() == b"keep"


def test_structure_validation_reads_owned_metadata_and_page_count(tmp_path: Path) -> None:
    payload = _pdf_bytes(
        title="  A\n  bounded\t title  ",
        author="Alice Example; Bob Example",
        pages=2,
    )
    content_id = ContentId(hashlib.sha256(payload).hexdigest())
    attempt_id = uuid4()
    path = tmp_path / "staging" / content_id / f"{attempt_id}.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    result = validate_staged_structure(
        StagedPdf(content_id, attempt_id, path, content_id, None)
    )

    assert result.status == "valid_pdf"
    assert result.staged.page_count == 2
    assert result.title == "A bounded title"
    assert result.authors == ("Alice Example", "Bob Example")


def test_valid_structure_persistence_enables_cloud_only_for_matching_single_link_source(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("metadata.pdf", _pdf_bytes(title="Study", pages=2))
    source = open_current_source_for_content(
        parser_library.database,
        parser_library.inspector,
        registered.content_id,
    )
    try:
        attempt_id = uuid4()
        staged = stage_verified_pdf(
            parser_library.data_root,
            registered.content_id,
            source,
            attempt_id,
            CheckpointRecorder(),
        )
        validation = validate_staged_structure(staged)
        persist_structure_validation(parser_library.database, validation, source.identity)
    finally:
        source.close()

    with parser_library.database.connect() as connection:
        content = connection.execute(
            "SELECT status,page_count,title FROM contents WHERE sha256=?",
            (registered.content_id,),
        ).fetchone()
        document = connection.execute(
            "SELECT auto_cloud_eligible FROM documents WHERE id=?",
            (str(registered.document_id),),
        ).fetchone()
    assert content is not None
    assert tuple(content) == ("valid_pdf", 2, "Study")
    assert document is not None
    assert int(document["auto_cloud_eligible"]) == 1


@pytest.mark.parametrize("payload", [b"not-pdf", b"%PDF-corrupt"])
def test_malformed_structure_is_invalid_but_staging_remains(
    tmp_path: Path,
    payload: bytes,
) -> None:
    content_id = ContentId(hashlib.sha256(payload).hexdigest())
    attempt_id = uuid4()
    path = tmp_path / "staging" / content_id / f"{attempt_id}.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)

    result = validate_staged_structure(
        StagedPdf(content_id, attempt_id, path, content_id, None)
    )

    assert result.status == "invalid_pdf"
    assert path.read_bytes() == payload
