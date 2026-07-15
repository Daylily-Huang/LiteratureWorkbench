from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from literature_workbench.features.library.fingerprint import fingerprint_verified_pdf
from literature_workbench.platform.windows.filesystem import FileIdentity, VerifiedHandle


def _handle(payload: bytes) -> VerifiedHandle:
    identity = FileIdentity(
        canonical_path=Path(r"C:\library\paper.pdf"),
        root_id=uuid4(),
        volume_id="volume",
        file_id="file",
        size=len(payload),
        modified_ns=1,
        hard_link_count=1,
        is_reparse_point=False,
    )
    return VerifiedHandle(identity=identity, stream=BytesIO(payload))


def test_fingerprint_streams_verified_handle_from_zero() -> None:
    handle = _handle(b"%PDF-1.7\nbody")
    handle.stream.seek(8)

    result = fingerprint_verified_pdf(handle, chunk_size=3)

    assert result.size_bytes == 13
    assert result.sha256 == "3f972854841afd236b04b5d7435b73216bc5fa6e39a86aff6e492b744086189c"
    assert handle.stream.tell() == 13


def test_fingerprint_rejects_non_pdf_bytes_from_verified_handle() -> None:
    with pytest.raises(ValueError, match="invalid_pdf_header"):
        fingerprint_verified_pdf(_handle(b"not-a-pdf"), chunk_size=2)


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_fingerprint_requires_positive_chunk_size(chunk_size: int) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        fingerprint_verified_pdf(_handle(b"%PDF-x"), chunk_size=chunk_size)
