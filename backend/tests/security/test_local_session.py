from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from literature_workbench.security.local_session import (
    ApiRoutePolicy,
    LocalSession,
    api_route_policy,
)


def _decoded_size(token: str) -> int:
    return len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_local_session_uses_256_bit_values_and_one_generation() -> None:
    session = LocalSession()
    first = session.issue_bootstrap_token()
    second = session.issue_bootstrap_token()

    assert _decoded_size(session.session_cookie) == 32
    assert _decoded_size(session.csrf_token) == 32
    assert _decoded_size(first) == 32
    assert _decoded_size(second) == 32
    assert first != second
    assert session.session_generation
    assert first not in repr(session)


@dataclass(slots=True)
class FakeMonotonic:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


def test_expired_bootstrap_token_is_rejected() -> None:
    clock = FakeMonotonic()
    session = LocalSession(now=clock)
    token = session.issue_bootstrap_token(ttl_seconds=60)
    clock.value = 160.001

    assert session.consume_bootstrap_token(token) is False


def test_bootstrap_is_posted_once_and_never_uses_query_string(
    client: TestClient,
    session: LocalSession,
) -> None:
    token = session.issue_bootstrap_token()
    response = client.post(
        "/api/session/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"token": token},
    )
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie
    assert "ds-secret" not in response.text
    assert client.post(
        "/api/session/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"token": token},
    ).status_code == 403
    query_token = session.issue_bootstrap_token()
    assert client.post(
        f"/api/session/bootstrap?token={query_token}",
        headers={"Origin": "http://testserver"},
    ).status_code in {404, 405}


def test_current_session_returns_only_in_memory_client_values(
    client: TestClient,
    session: LocalSession,
) -> None:
    token = session.issue_bootstrap_token()
    assert client.post(
        "/api/session/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"token": token},
    ).status_code == 204

    response = client.get("/api/session/current")

    assert response.status_code == 200
    assert response.json() == {
        "csrf_token": session.csrf_token,
        "session_generation": session.session_generation,
    }
    assert session.session_cookie not in response.text


def test_multiple_bootstrap_tokens_do_not_invalidate_authenticated_tabs(
    client: TestClient,
    session: LocalSession,
) -> None:
    first = session.issue_bootstrap_token()
    second = session.issue_bootstrap_token()
    assert client.post(
        "/api/session/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"token": first},
    ).status_code == 204
    original_cookie = client.cookies["lw_session"]
    assert client.post(
        "/api/session/bootstrap",
        headers={"Origin": "http://testserver"},
        json={"token": second},
    ).status_code == 204
    assert client.cookies["lw_session"] == original_cookie
    assert client.get("/api/session/current").status_code == 200


def test_cross_origin_write_is_rejected(
    client: TestClient,
    authenticated,
) -> None:
    response = client.patch(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={"Origin": "https://malicious.example", "X-LW-CSRF": authenticated.csrf},
        json={"default_translation_mode": "local_only"},
    )
    assert response.status_code == 403


def test_missing_csrf_is_rejected_with_correct_origin_and_session(
    client: TestClient,
    authenticated,
) -> None:
    response = client.patch(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={"Origin": "http://testserver"},
        json={"default_translation_mode": "local_only"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/api/settings", {}),
        ("PUT", "/api/settings/deepseek-key", {"api_key": "abcdefgh"}),
        ("DELETE", "/api/settings/deepseek-key", None),
    ],
)
def test_every_authenticated_api_write_denies_missing_csrf(
    client: TestClient,
    authenticated,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    response = client.request(
        method,
        path,
        cookies=authenticated.cookies,
        headers={"Origin": "http://testserver"},
        json=body,
    )
    assert response.status_code == 403


def test_incorrect_csrf_is_rejected_with_correct_origin_and_session(
    client: TestClient,
    authenticated,
) -> None:
    response = client.patch(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={"Origin": "http://testserver", "X-LW-CSRF": "wrong"},
        json={},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("cookie", [None, "wrong-session"])
def test_missing_or_incorrect_session_is_rejected(
    client: TestClient,
    authenticated,
    cookie: str | None,
) -> None:
    cookies = {} if cookie is None else {"lw_session": cookie}
    if cookie is None:
        client.cookies.delete("lw_session")
    response = client.get("/api/settings", cookies=cookies)
    assert response.status_code == 403


def test_wrong_host_is_rejected_with_otherwise_correct_credentials(
    client: TestClient,
    authenticated,
) -> None:
    response = client.patch(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={
            "Host": "malicious.example",
            "Origin": "http://malicious.example",
            "X-LW-CSRF": authenticated.csrf,
        },
        json={},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/session/bootstrap", {"token": "unused-token"}),
        ("PATCH", "/api/settings", {}),
        ("PUT", "/api/settings/deepseek-key", {"api_key": "abcdefgh"}),
        ("DELETE", "/api/settings/deepseek-key", None),
    ],
)
def test_every_browser_write_method_requires_origin(
    client: TestClient,
    authenticated,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    headers = {"X-LW-CSRF": authenticated.csrf}
    response = client.request(
        method,
        path,
        cookies=authenticated.cookies,
        headers=headers,
        json=body,
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://127.0.0.1",
        "http://localhost",
        "http://testserver:80",
        "https://testserver",
        "http://user@testserver",
        "http://testserver/#fragment",
    ],
)
def test_non_exact_origin_is_rejected(
    client: TestClient,
    authenticated,
    origin: str,
) -> None:
    response = client.patch(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={"Origin": origin, "X-LW-CSRF": authenticated.csrf},
        json={},
    )
    assert response.status_code == 403


def test_cross_origin_read_is_rejected_when_origin_is_supplied(
    client: TestClient,
    authenticated,
) -> None:
    response = client.get(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "header",
    [
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Port",
        "X-Forwarded-Prefix",
        "x-FoRwArDeD-server",
    ],
)
def test_forwarding_headers_are_always_rejected(
    client: TestClient,
    authenticated,
    header: str,
) -> None:
    response = client.get(
        "/api/settings",
        cookies=authenticated.cookies,
        headers={header: "127.0.0.1"},
    )
    assert response.status_code == 403


def test_composed_app_api_route_inventory_has_explicit_security_policy(
    client: TestClient,
) -> None:
    inventory: set[tuple[str, str, ApiRoutePolicy]] = set()
    for included in client.app.routes:
        router = getattr(included, "original_router", None)
        for route in getattr(router, "routes", ()):
            if not route.path.startswith("/api"):
                continue
            for method in route.methods:
                policy = api_route_policy(route.path, method)
                inventory.add((route.path, method, policy))
                assert policy is not ApiRoutePolicy.UNCLASSIFIED

    assert ("/api/health", "GET", ApiRoutePolicy.PUBLIC_READ) in inventory
    assert (
        "/api/session/bootstrap",
        "POST",
        ApiRoutePolicy.PUBLIC_BOOTSTRAP,
    ) in inventory
    assert ("/api/settings", "GET", ApiRoutePolicy.SESSION_READ) in inventory
    assert ("/api/settings", "PATCH", ApiRoutePolicy.SESSION_WRITE) in inventory


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/session/current", None),
        ("GET", "/api/settings", None),
        ("PATCH", "/api/settings", {}),
        ("PUT", "/api/settings/deepseek-key", {"api_key": "abcdefgh"}),
        ("DELETE", "/api/settings/deepseek-key", None),
    ],
)
def test_all_non_public_api_routes_deny_missing_cookie(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    response = client.request(
        method,
        path,
        headers={"Origin": "http://testserver"},
        json=body,
    )
    assert response.status_code == 403
