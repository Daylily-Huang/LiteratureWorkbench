from __future__ import annotations

from typing import Protocol

from keyring.backends.Windows import WinVaultKeyring
from keyring.errors import PasswordDeleteError
from pydantic import SecretStr


SERVICE_NAME = "LiteratureWorkbench"
DEEPSEEK_USERNAME = "deepseek_api_key"


class KeyringBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretStore:
    __slots__ = ("_backend",)

    def __init__(self, backend: KeyringBackend) -> None:
        self._backend = backend

    def __repr__(self) -> str:
        return f"SecretStore(backend={type(self._backend).__name__})"

    def put_deepseek_key(self, api_key: str) -> None:
        self._backend.set_password(SERVICE_NAME, DEEPSEEK_USERNAME, api_key)

    def get_deepseek_key(self) -> SecretStr | None:
        value = self._backend.get_password(SERVICE_NAME, DEEPSEEK_USERNAME)
        return SecretStr(value) if value is not None else None

    def deepseek_key_configured(self) -> bool:
        return self._backend.get_password(SERVICE_NAME, DEEPSEEK_USERNAME) is not None

    def delete_deepseek_key(self) -> None:
        try:
            self._backend.delete_password(SERVICE_NAME, DEEPSEEK_USERNAME)
        except PasswordDeleteError:
            pass


def create_windows_secret_store() -> SecretStore:
    backend = WinVaultKeyring()  # type: ignore[no-untyped-call]
    if backend.priority <= 0:
        raise RuntimeError("Windows Credential Manager is unavailable")
    return SecretStore(backend)
