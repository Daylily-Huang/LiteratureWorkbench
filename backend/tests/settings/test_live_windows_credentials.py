from __future__ import annotations

import os
import secrets

import pytest
from keyring.backends.Windows import WinVaultKeyring


@pytest.mark.live_credentials
@pytest.mark.skipif(
    os.getenv("LW_RUN_LIVE_CREDENTIAL_TESTS") != "1",
    reason="set LW_RUN_LIVE_CREDENTIAL_TESTS=1 to touch an isolated credential",
)
def test_live_windows_credential_round_trip_is_isolated() -> None:
    backend = WinVaultKeyring()
    service = f"LiteratureWorkbench-test-{secrets.token_hex(12)}"
    username = "test-only"
    sentinel = f"test-{secrets.token_urlsafe(24)}"
    try:
        backend.set_password(service, username, sentinel)
        assert backend.get_password(service, username) == sentinel
    finally:
        try:
            backend.delete_password(service, username)
        except Exception:
            if backend.get_password(service, username) is not None:
                raise
