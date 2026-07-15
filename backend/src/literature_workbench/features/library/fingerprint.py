from __future__ import annotations

import hashlib
from dataclasses import dataclass

from literature_workbench.platform.windows.filesystem import VerifiedHandle


@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    sha256: str
    size_bytes: int


def fingerprint_verified_pdf(
    handle: VerifiedHandle,
    chunk_size: int = 4 * 1024 * 1024,
) -> ContentFingerprint:
    if chunk_size <= 0:
        raise ValueError("chunk_size_must_be_positive")
    stream = handle.stream
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    while chunk := stream.read(chunk_size):
        if len(prefix) < 5:
            prefix += chunk[: 5 - len(prefix)]
        digest.update(chunk)
        size += len(chunk)
    if prefix != b"%PDF-":
        raise ValueError("invalid_pdf_header")
    return ContentFingerprint(sha256=digest.hexdigest(), size_bytes=size)
