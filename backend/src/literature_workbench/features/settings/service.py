from __future__ import annotations

from literature_workbench.features.settings.models import PublicSettings, SettingsPatch
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.platform.windows.credentials import SecretStore


class SettingsService:
    def __init__(self, repository: SettingsRepository, secret_store: SecretStore) -> None:
        self._repository = repository
        self._secret_store = secret_store

    def read_public(self) -> PublicSettings:
        record = self._repository.read()
        return PublicSettings(
            deepseek_key_configured=self._secret_store.deepseek_key_configured(),
            default_translation_mode=record.default_translation_mode,
        )

    def patch_public(self, patch: SettingsPatch) -> PublicSettings:
        if "default_translation_mode" in patch.model_fields_set:
            mode = patch.default_translation_mode
            if mode is None:
                raise AssertionError("SettingsPatch rejects explicit null")
            self._repository.update_translation_mode(mode)
        return self.read_public()

    def put_deepseek_key(self, api_key: str) -> None:
        self._secret_store.put_deepseek_key(api_key)

    def delete_deepseek_key(self) -> None:
        self._secret_store.delete_deepseek_key()
