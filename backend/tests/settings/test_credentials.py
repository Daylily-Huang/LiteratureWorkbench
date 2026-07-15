from __future__ import annotations

import os

import pytest
from keyring.errors import PasswordDeleteError

from literature_workbench.platform.windows.credentials import (
    SecretStore,
    create_windows_secret_store,
)


def test_deepseek_key_is_never_returned(fake_keyring) -> None:
    store = SecretStore(fake_keyring)
    store.put_deepseek_key("ds-secret-value")
    assert store.deepseek_key_configured() is True
    assert store.get_deepseek_key().get_secret_value() == "ds-secret-value"
    assert "ds-secret-value" not in repr(store)
    assert fake_keyring.values[("LiteratureWorkbench", "deepseek_api_key")] == "ds-secret-value"


def test_delete_deepseek_key_is_idempotent(fake_keyring) -> None:
    store = SecretStore(fake_keyring)
    store.delete_deepseek_key()
    store.put_deepseek_key("ds-secret-value")
    store.delete_deepseek_key()
    store.delete_deepseek_key()
    assert store.deepseek_key_configured() is False


def test_delete_catches_only_missing_password_error() -> None:
    class MissingPasswordBackend:
        def set_password(self, service: str, username: str, password: str) -> None:
            raise AssertionError("not used")

        def get_password(self, service: str, username: str) -> str | None:
            return None

        def delete_password(self, service: str, username: str) -> None:
            raise PasswordDeleteError("missing")

    SecretStore(MissingPasswordBackend()).delete_deepseek_key()


def test_delete_propagates_arbitrary_backend_failure() -> None:
    class BrokenBackend:
        def set_password(self, service: str, username: str, password: str) -> None:
            raise AssertionError("not used")

        def get_password(self, service: str, username: str) -> str | None:
            return None

        def delete_password(self, service: str, username: str) -> None:
            raise RuntimeError("backend failed")

    with pytest.raises(RuntimeError, match="backend failed"):
        SecretStore(BrokenBackend()).delete_deepseek_key()


def test_production_factory_ignores_environment_selected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "hostile.plaintext.Backend")
    monkeypatch.setattr(
        "keyring.get_keyring",
        lambda: pytest.fail("environment-selected keyring must not be queried"),
    )

    store = create_windows_secret_store()

    assert "WinVaultKeyring" in repr(store)
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "hostile.plaintext.Backend"


def test_unavailable_windows_backend_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableWindowsBackend:
        priority = 0

    monkeypatch.setattr(
        "literature_workbench.platform.windows.credentials.WinVaultKeyring",
        UnavailableWindowsBackend,
    )

    with pytest.raises(RuntimeError, match="Windows Credential Manager"):
        create_windows_secret_store()
