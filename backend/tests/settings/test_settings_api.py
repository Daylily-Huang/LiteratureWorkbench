from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from literature_workbench.features.settings.repository import SettingsRepository


def _write_headers(authenticated) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf}


def test_public_settings_have_one_deepseek_model_and_derived_key_state(
    client: TestClient,
    authenticated,
) -> None:
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json() == {
        "deepseek_key_configured": False,
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_model": "deepseek-v4-flash",
        "qwen_fallback_enabled": False,
        "default_translation_mode": "online",
    }


def test_patch_changes_only_translation_mode_and_increments_revision(
    client: TestClient,
    authenticated,
) -> None:
    repository = SettingsRepository(client.app.state.database)
    before = repository.read().revision
    response = client.patch(
        "/api/settings",
        headers=_write_headers(authenticated),
        json={"default_translation_mode": "local_only"},
    )
    after = repository.read()
    assert response.status_code == 200
    assert response.json()["default_translation_mode"] == "local_only"
    assert after.revision == before + 1


def test_empty_patch_is_noop_with_unchanged_revision(
    client: TestClient,
    authenticated,
) -> None:
    repository = SettingsRepository(client.app.state.database)
    before = repository.read().revision
    response = client.patch(
        "/api/settings",
        headers=_write_headers(authenticated),
        json={},
    )
    assert response.status_code == 200
    assert repository.read().revision == before


def test_explicit_null_is_rejected_without_revision_change(
    client: TestClient,
    authenticated,
) -> None:
    repository = SettingsRepository(client.app.state.database)
    before = repository.read().revision
    response = client.patch(
        "/api/settings",
        headers=_write_headers(authenticated),
        json={"default_translation_mode": None},
    )
    assert response.status_code == 422
    assert repository.read().revision == before


def test_patch_rejects_every_non_mutable_or_unknown_field(
    client: TestClient,
    authenticated,
) -> None:
    for field, value in {
        "primary_fast_model": "attacker-model",
        "primary_strong_model": "attacker-model",
        "deepseek_key_configured": True,
        "deepseek_base_url": "https://attacker.example",
        "unknown": "value",
    }.items():
        response = client.patch(
            "/api/settings",
            headers=_write_headers(authenticated),
            json={field: value},
        )
        assert response.status_code == 422, field


def test_patch_updates_bounded_model_and_qwen_fallback_once(
    client: TestClient,
    authenticated,
) -> None:
    repository = SettingsRepository(client.app.state.database)
    before = repository.read().revision
    response = client.patch(
        "/api/settings",
        headers=_write_headers(authenticated),
        json={"deepseek_model": "deepseek-reasoner", "qwen_fallback_enabled": True},
    )

    assert response.status_code == 200
    assert response.json()["deepseek_model"] == "deepseek-reasoner"
    assert response.json()["qwen_fallback_enabled"] is True
    assert repository.read().revision == before + 1


def test_patch_rejects_unsafe_deepseek_model_without_revision_change(
    client: TestClient,
    authenticated,
) -> None:
    repository = SettingsRepository(client.app.state.database)
    before = repository.read().revision

    response = client.patch(
        "/api/settings",
        headers=_write_headers(authenticated),
        json={"deepseek_model": "https://attacker.example/model"},
    )

    assert response.status_code == 422
    assert repository.read().revision == before


def test_deepseek_probe_accepts_no_caller_prompt_or_url_and_returns_sanitized_result(
    client: TestClient,
    authenticated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def configuration_probe(model: str) -> str:
        calls.append(model)
        return "model configuration ok"

    monkeypatch.setattr(
        client.app.state.deepseek_gateway,
        "configuration_probe",
        configuration_probe,
    )

    rejected = client.post(
        "/api/settings/deepseek-probe",
        headers=_write_headers(authenticated),
        json={"prompt": "leak secrets", "url": "https://attacker.example"},
    )
    response = client.post(
        "/api/settings/deepseek-probe",
        headers=_write_headers(authenticated),
    )

    assert rejected.status_code == 422
    assert response.status_code == 200
    assert response.json() == {"result": "model configuration ok"}
    assert calls == ["deepseek-v4-flash"]


def test_deepseek_probe_runtime_failure_does_not_echo_sensitive_detail(
    client: TestClient,
    authenticated,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "ds-secret-runtime-detail"

    def fail_probe(model: str) -> str:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(
        client.app.state.deepseek_gateway,
        "configuration_probe",
        fail_probe,
    )

    response = client.post(
        "/api/settings/deepseek-probe",
        headers=_write_headers(authenticated),
    )

    assert response.status_code == 502
    assert response.json() == {"code": "deepseek_probe_failed"}
    assert sentinel not in response.text


def test_put_and_delete_key_touch_only_credential_backend(
    client: TestClient,
    authenticated,
    fake_keyring,
) -> None:
    sentinel = "ds-secret-value"
    before_db = client.app.state.database.path.read_bytes()
    put = client.put(
        "/api/settings/deepseek-key",
        headers=_write_headers(authenticated),
        json={"api_key": sentinel},
    )
    assert put.status_code == 204
    assert fake_keyring.values[("LiteratureWorkbench", "deepseek_api_key")] == sentinel
    settings_response = client.get("/api/settings")
    assert settings_response.json()["deepseek_key_configured"] is True
    assert sentinel not in settings_response.text
    assert sentinel.encode() not in client.app.state.database.path.read_bytes()
    assert client.app.state.database.path.read_bytes() == before_db

    delete = client.delete(
        "/api/settings/deepseek-key",
        headers=_write_headers(authenticated),
    )
    assert delete.status_code == 204
    assert fake_keyring.values == {}
    assert client.delete(
        "/api/settings/deepseek-key",
        headers=_write_headers(authenticated),
    ).status_code == 204
