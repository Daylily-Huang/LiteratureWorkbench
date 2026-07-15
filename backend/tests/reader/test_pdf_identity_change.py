from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import RegisteredPdfFixture
from literature_workbench.features.reader.pdf_stream import (
    DocumentIdentityChanged,
    load_registered_pdf,
    open_verified_pdf,
)
from literature_workbench.platform.windows.filesystem import (
    SafeWindowsFileInspector,
    VerifiedHandle,
)


@pytest.mark.parametrize("mutation", ["replacement", "in_place", "hard_link"])
def test_identity_changed_since_scan_returns_409_before_any_body_bytes(
    registered_pdf: RegisteredPdfFixture,
    mutation: str,
) -> None:
    if mutation == "replacement":
        replacement = registered_pdf.pdf_path.with_name("replacement.pdf")
        replacement.write_bytes(registered_pdf.pdf_bytes)
        registered_pdf.pdf_path.unlink()
        replacement.rename(registered_pdf.pdf_path)
    elif mutation == "in_place":
        before = registered_pdf.pdf_path.stat()
        changed = bytearray(registered_pdf.pdf_bytes)
        changed[-1] ^= 1
        registered_pdf.pdf_path.write_bytes(changed)
        os.utime(
            registered_pdf.pdf_path,
            ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000),
        )
    else:
        os.link(
            registered_pdf.pdf_path,
            registered_pdf.pdf_path.with_name("additional-hard-link.pdf"),
        )

    response = registered_pdf.client.get(
        f"/api/documents/{registered_pdf.document_id}/file",
        headers=registered_pdf.auth_headers,
    )

    assert response.status_code == 409
    assert response.content == b""


def test_missing_registered_file_returns_empty_409(
    registered_pdf: RegisteredPdfFixture,
) -> None:
    registered_pdf.pdf_path.unlink()

    response = registered_pdf.client.get(
        f"/api/documents/{registered_pdf.document_id}/file",
        headers=registered_pdf.auth_headers,
    )

    assert response.status_code == 409
    assert response.content == b""


def test_api_never_accepts_a_local_path_query(registered_pdf: RegisteredPdfFixture) -> None:
    unrelated = Path(__file__)

    response = registered_pdf.client.get(
        f"/api/documents/{registered_pdf.document_id}/file",
        headers=registered_pdf.auth_headers,
        params={"path": str(unrelated)},
    )

    assert response.status_code == 422
    assert str(unrelated) not in response.text


def test_failed_handle_identity_reread_closes_the_held_stream(
    registered_pdf: RegisteredPdfFixture,
) -> None:
    class FailingRereadInspector:
        handle: VerifiedHandle | None = None

        def open_registered_pdf(self, document: object) -> VerifiedHandle:
            handle = SafeWindowsFileInspector().open_registered_pdf(document)  # type: ignore[arg-type]

            def fail_reread() -> object:
                raise OSError("identity reread failed")

            handle.identity_reader = fail_reread  # type: ignore[assignment]
            self.handle = handle
            return handle

    inspector = FailingRereadInspector()
    registered = load_registered_pdf(
        registered_pdf.client.app.state.database,
        registered_pdf.document_id,
    )

    with pytest.raises(DocumentIdentityChanged):
        open_verified_pdf(inspector, registered)  # type: ignore[arg-type]

    assert inspector.handle is not None
    assert inspector.handle.stream.closed
