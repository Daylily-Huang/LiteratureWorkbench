from __future__ import annotations

from fastapi.testclient import TestClient

from literature_workbench.features.settings.repository import SettingsRepository


def _write_headers(authenticated) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf}


def test_public_settings_have_fixed_models_and_derived_key_state(
    client: TestClient,
    authenticated,
) -> None:
    response = client.get("/api/settings", cookies=authenticated.cookies)
    assert response.status_code == 200
    assert response.json() == {
        "deepseek_key_configured": False,
        "deepseek_base_url": "https://api.deepseek.com",
        "primary_fast_model": "deepseek-v4-flash",
        "primary_strong_model": "deepseek-v4-pro",
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
        cookies=authenticated.cookies,
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
        cookies=authenticated.cookies,
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
        cookies=authenticated.cookies,
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
            cookies=authenticated.cookies,
            headers=_write_headers(authenticated),
            json={field: value},
        )
        assert response.status_code == 422, field


def test_put_and_delete_key_touch_only_credential_backend(
    client: TestClient,
    authenticated,
    fake_keyring,
) -> None:
    sentinel = "ds-secret-value"
    before_db = client.app.state.database.path.read_bytes()
    put = client.put(
        "/api/settings/deepseek-key",
        cookies=authenticated.cookies,
        headers=_write_headers(authenticated),
        json={"api_key": sentinel},
    )
    assert put.status_code == 204
    assert fake_keyring.values[("LiteratureWorkbench", "deepseek_api_key")] == sentinel
    settings_response = client.get("/api/settings", cookies=authenticated.cookies)
    assert settings_response.json()["deepseek_key_configured"] is True
    assert sentinel not in settings_response.text
    assert sentinel.encode() not in client.app.state.database.path.read_bytes()
    assert client.app.state.database.path.read_bytes() == before_db

    delete = client.delete(
        "/api/settings/deepseek-key",
        cookies=authenticated.cookies,
        headers=_write_headers(authenticated),
    )
    assert delete.status_code == 204
    assert fake_keyring.values == {}
    assert client.delete(
        "/api/settings/deepseek-key",
        cookies=authenticated.cookies,
        headers=_write_headers(authenticated),
    ).status_code == 204
