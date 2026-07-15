from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from conftest import RegisteredPdfFixture
from literature_workbench.features.reader.pdf_stream import ByteRange, stream_pdf
from literature_workbench.platform.windows.filesystem import FileIdentity, VerifiedHandle


def _url(value: RegisteredPdfFixture) -> str:
    return f"/api/documents/{value.document_id}/file"


def _verified_handle(payload: bytes) -> VerifiedHandle:
    identity = FileIdentity(
        canonical_path=Path("fixture.pdf"),
        root_id=uuid4(),
        volume_id="volume",
        file_id="file",
        size=len(payload),
        modified_ns=1,
        hard_link_count=1,
        is_reparse_point=False,
    )
    return VerifiedHandle(identity=identity, stream=BytesIO(payload))


def test_stream_chunks_are_bounded_and_handle_closes_after_completion() -> None:
    payload = b"x" * (2 * 1024 * 1024 + 17)
    handle = _verified_handle(payload)

    chunks = list(stream_pdf(handle, ByteRange(0, len(payload) - 1)))

    assert b"".join(chunks) == payload
    assert max(map(len, chunks)) <= 1024 * 1024
    assert handle.stream.closed is True


def test_stream_generator_close_releases_verified_handle_on_disconnect() -> None:
    handle = _verified_handle(b"abcdefgh")
    body = stream_pdf(handle, ByteRange(0, 7), chunk_size=4)

    assert next(body) == b"abcd"
    assert handle.stream.closed is False
    body.close()

    assert handle.stream.closed is True


def test_registered_pdf_requires_same_origin_session_cookie(
    registered_pdf: RegisteredPdfFixture,
) -> None:
    missing = registered_pdf.client.get(_url(registered_pdf))
    cross_origin = registered_pdf.client.get(
        _url(registered_pdf),
        headers={
            **registered_pdf.auth_headers,
            "Origin": "https://attacker.example",
        },
    )

    assert missing.status_code == 403
    assert missing.json() == {"code": "session_required"}
    assert cross_origin.status_code == 403
    assert cross_origin.json() == {"code": "origin_rejected"}


def test_full_pdf_response_has_private_inline_headers(
    registered_pdf: RegisteredPdfFixture,
) -> None:
    response = registered_pdf.client.get(
        _url(registered_pdf), headers=registered_pdf.auth_headers
    )

    assert response.status_code == 200
    assert response.content == registered_pdf.pdf_bytes
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "1024"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["etag"] == f'"sha256:{registered_pdf.content_id}"'


def test_bounded_byte_range_returns_206(registered_pdf: RegisteredPdfFixture) -> None:
    response = registered_pdf.client.get(
        _url(registered_pdf),
        headers={**registered_pdf.auth_headers, "Range": "bytes=100-199"},
    )

    assert response.status_code == 206
    assert response.content == registered_pdf.pdf_bytes[100:200]
    assert response.headers["content-range"] == "bytes 100-199/1024"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "100"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=100-", slice(100, 1024)),
        ("bytes=-100", slice(924, 1024)),
        ("bytes=100-9999", slice(100, 1024)),
    ],
)
def test_open_ended_suffix_and_clamped_ranges(
    registered_pdf: RegisteredPdfFixture,
    header: str,
    expected: slice,
) -> None:
    response = registered_pdf.client.get(
        _url(registered_pdf),
        headers={**registered_pdf.auth_headers, "Range": header},
    )

    assert response.status_code == 206
    assert response.content == registered_pdf.pdf_bytes[expected]


def test_matching_if_none_match_returns_empty_304(
    registered_pdf: RegisteredPdfFixture,
) -> None:
    response = registered_pdf.client.get(
        _url(registered_pdf),
        headers={
            **registered_pdf.auth_headers,
            "If-None-Match": f'"sha256:{registered_pdf.content_id}"',
        },
    )

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == f'"sha256:{registered_pdf.content_id}"'
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1024-",
        "bytes=20-10",
        "bytes=-0",
        "bytes=",
        "bytes=abc-def",
        "bytes=0-1,4-5",
        "items=0-1",
    ],
)
def test_unsatisfiable_multiple_and_malformed_ranges_return_416(
    registered_pdf: RegisteredPdfFixture,
    header: str,
) -> None:
    response = registered_pdf.client.get(
        _url(registered_pdf),
        headers={**registered_pdf.auth_headers, "Range": header},
    )

    assert response.status_code == 416
    assert response.content == b""
    assert response.headers["content-range"] == "bytes */1024"
    assert response.headers["accept-ranges"] == "bytes"


def test_missing_document_returns_404(registered_pdf: RegisteredPdfFixture) -> None:
    response = registered_pdf.client.get(
        f"/api/documents/{uuid4()}/file", headers=registered_pdf.auth_headers
    )

    assert response.status_code == 404
    assert response.json() == {"code": "document_not_found"}


def test_non_pdf_status_returns_empty_409(registered_pdf: RegisteredPdfFixture) -> None:
    with registered_pdf.client.app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET format='docx',status='unsupported_v01' WHERE id=?",
            (str(registered_pdf.document_id),),
        )

    response = registered_pdf.client.get(
        _url(registered_pdf), headers=registered_pdf.auth_headers
    )

    assert response.status_code == 409
    assert response.content == b""
