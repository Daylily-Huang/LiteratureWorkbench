from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from literature_workbench.platform.windows.filesystem import RootIdentity
from literature_workbench.security.selection_tokens import (
    InMemoryRootSelectionTokens,
    InvalidSelectionToken,
)


def _identity(tmp_path: Path) -> RootIdentity:
    return RootIdentity(
        canonical_path=tmp_path,
        volume_id="00000000000000a1",
        file_id="000000000000000000000000000000b2",
    )


def test_selection_token_is_one_use(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    tokens = InMemoryRootSelectionTokens(now=lambda: now)
    generation = uuid4()
    identity = _identity(tmp_path)

    token = tokens.issue(identity, generation)

    assert tokens.consume(token, generation) == identity
    with pytest.raises(InvalidSelectionToken, match="invalid_selection_token"):
        tokens.consume(token, generation)


def test_selection_token_is_bound_to_issuing_session(tmp_path: Path) -> None:
    tokens = InMemoryRootSelectionTokens()
    issuing_generation = uuid4()
    token = tokens.issue(_identity(tmp_path), issuing_generation)

    with pytest.raises(InvalidSelectionToken, match="invalid_selection_token"):
        tokens.consume(token, uuid4())

    assert tokens.consume(token, issuing_generation) == _identity(tmp_path)


def test_expired_selection_token_fails_closed_without_secret_detail(tmp_path: Path) -> None:
    clock = [datetime(2026, 7, 15, tzinfo=UTC)]
    tokens = InMemoryRootSelectionTokens(now=lambda: clock[0])
    generation = uuid4()
    token = tokens.issue(_identity(tmp_path), generation, ttl_seconds=3)
    clock[0] += timedelta(seconds=3)

    with pytest.raises(InvalidSelectionToken) as exc_info:
        tokens.consume(token, generation)

    assert str(exc_info.value) == "invalid_selection_token"
    assert token not in str(exc_info.value)


def test_selection_token_has_256_random_bits_and_only_digest_is_retained(tmp_path: Path) -> None:
    random_value = bytes(range(32))
    tokens = InMemoryRootSelectionTokens(random_bytes=lambda count: random_value)

    token = tokens.issue(_identity(tmp_path), uuid4())

    assert token == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert token not in repr(tokens)
    assert token.encode("ascii") not in tokens.storage_snapshot()


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_selection_token_rejects_non_positive_ttl(tmp_path: Path, ttl_seconds: int) -> None:
    tokens = InMemoryRootSelectionTokens()

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        tokens.issue(_identity(tmp_path), uuid4(), ttl_seconds=ttl_seconds)
