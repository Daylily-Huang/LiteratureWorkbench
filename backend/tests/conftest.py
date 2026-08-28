from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.platform.windows.credentials import KeyringBackend, SecretStore
from literature_workbench.security.local_session import LocalSession


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    cookies: dict[str, str]
    csrf: str


@dataclass(slots=True)
class FakeKeyring(KeyringBackend):
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def secret_store(fake_keyring: FakeKeyring) -> SecretStore:
    return SecretStore(fake_keyring)


@pytest.fixture
def session() -> LocalSession:
    return LocalSession()


@pytest.fixture(autouse=True)
def loopback_testclient_default(monkeypatch: pytest.MonkeyPatch) -> None:
    original_init = TestClient.__init__

    def initialize(test_client: TestClient, app: object, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("base_url", "http://127.0.0.1")
        original_init(test_client, app, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TestClient, "__init__", initialize)


@pytest.fixture
def client(
    tmp_path: Path,
    session: LocalSession,
    secret_store: SecretStore,
) -> Iterator[TestClient]:
    settings = AppSettings(data_root=tmp_path, allowed_hosts=("testserver",))
    with TestClient(
        create_app(
            settings,
            local_session=session,
            secret_store=secret_store,
            register_initial_library=False,
        ),
        base_url="http://testserver",
    ) as test_client:
        yield test_client


@pytest.fixture
def authenticated(client: TestClient, session: LocalSession) -> AuthenticatedSession:
    token = session.issue_bootstrap_token()
    response = client.post(
        "/api/session/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"token": token},
    )
    assert response.status_code == 204
    current = client.get("/api/session/current")
    assert current.status_code == 200
    return AuthenticatedSession(
        cookies={"lw_session": client.cookies["lw_session"]},
        csrf=current.json()["csrf_token"],
    )
