from __future__ import annotations

from typing import Protocol

from literature_workbench.features.egress.billing import DeepSeekBillingStatus
from literature_workbench.features.settings.models import PublicSettings, SettingsPatch
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.platform.windows.credentials import SecretStore


class ConfigurationProbeGateway(Protocol):
    def configuration_probe(self, model: str) -> str: ...

    def billing_status(self, *, force: bool = False) -> DeepSeekBillingStatus: ...


class SettingsService:
    def __init__(
        self,
        repository: SettingsRepository,
        secret_store: SecretStore,
        probe_gateway: ConfigurationProbeGateway | None = None,
    ) -> None:
        self._repository = repository
        self._secret_store = secret_store
        self._probe_gateway = probe_gateway

    def read_public(self) -> PublicSettings:
        record = self._repository.read()
        return PublicSettings(
            deepseek_key_configured=self._secret_store.deepseek_key_configured(),
            default_translation_mode=record.default_translation_mode,
            deepseek_model=record.deepseek_model,
            qwen_fallback_enabled=record.qwen_fallback_enabled,
            translation_glossary=record.translation_glossary,
        )

    def patch_public(self, patch: SettingsPatch) -> PublicSettings:
        if patch.model_fields_set:
            self._repository.update(
                default_translation_mode=patch.default_translation_mode,
                deepseek_model=patch.deepseek_model,
                qwen_fallback_enabled=patch.qwen_fallback_enabled,
                translation_glossary=patch.translation_glossary,
            )
        return self.read_public()

    def probe_deepseek(self) -> str:
        gateway = self._probe_gateway
        if gateway is None:
            raise RuntimeError("deepseek_probe_unavailable")
        return gateway.configuration_probe(self._repository.read().deepseek_model)

    def read_deepseek_billing(self, *, force: bool = False) -> DeepSeekBillingStatus:
        gateway = self._probe_gateway
        if gateway is None:
            raise RuntimeError("deepseek_billing_unavailable")
        return gateway.billing_status(force=force)

    def put_deepseek_key(self, api_key: str) -> None:
        self._secret_store.put_deepseek_key(api_key)

    def delete_deepseek_key(self) -> None:
        self._secret_store.delete_deepseek_key()
