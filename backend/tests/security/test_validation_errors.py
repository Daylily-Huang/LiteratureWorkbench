from __future__ import annotations

import logging

from fastapi.testclient import TestClient

def test_validation_errors_are_sanitized_and_stable(
    client: TestClient,
    authenticated,
    caplog: object,
) -> None:
    sentinel = "sentinel-invalid-model"
    with caplog.at_level(logging.INFO):  # type: ignore[attr-defined]
        response = client.patch(
            "/api/settings",
            headers={"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf},
            json={"primary_fast_model": sentinel},
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": "request_validation_error",
        "errors": [
            {
                "loc": ["body", "<redacted>"],
                "type": "extra_forbidden",
            }
        ],
    }
    assert sentinel not in response.text
    assert sentinel not in caplog.text  # type: ignore[attr-defined]
    assert sentinel.encode() not in client.app.state.database.path.read_bytes()
    assert "input" not in response.text
    assert "ctx" not in response.text


def test_invalid_secret_is_absent_from_response_logs_and_sqlite(
    client: TestClient,
    authenticated,
    caplog: object,
) -> None:
    sentinel = "secret7"
    with caplog.at_level(logging.INFO):  # type: ignore[attr-defined]
        response = client.put(
            "/api/settings/deepseek-key",
            headers={"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf},
            json={"api_key": sentinel},
        )

    assert response.status_code == 422
    assert sentinel not in response.text
    assert sentinel not in caplog.text  # type: ignore[attr-defined]
    database_bytes = client.app.state.database.path.read_bytes()
    assert sentinel.encode() not in database_bytes


def test_hostile_unknown_field_name_is_redacted_everywhere(
    client: TestClient,
    authenticated,
    caplog: object,
) -> None:
    sentinel = "unknown-field-ds-secret-location"
    with caplog.at_level(logging.INFO):  # type: ignore[attr-defined]
        response = client.patch(
            "/api/settings",
            headers={"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf},
            json={sentinel: "ordinary-value"},
        )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"loc": ["body", "<redacted>"], "type": "extra_forbidden"}
    ]
    assert sentinel not in response.text
    assert sentinel not in caplog.text  # type: ignore[attr-defined]
    assert sentinel.encode() not in client.app.state.database.path.read_bytes()
    assert sentinel not in repr(response.json())
