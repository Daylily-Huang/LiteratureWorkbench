from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from literature_workbench.db.database import Database
from literature_workbench.features.settings.models import TranslationMode


_PUBLIC_SETTINGS_KEY = "public_settings"


@dataclass(frozen=True, slots=True)
class SettingsRecord:
    default_translation_mode: TranslationMode
    deepseek_model: str
    qwen_fallback_enabled: bool
    revision: int


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def read(self) -> SettingsRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT value_json, revision FROM app_settings WHERE key = ?",
                (_PUBLIC_SETTINGS_KEY,),
            ).fetchone()
        if row is None:
            return SettingsRecord(
                default_translation_mode="online",
                deepseek_model="deepseek-v4-flash",
                qwen_fallback_enabled=False,
                revision=0,
            )
        values = json.loads(str(row["value_json"]))
        mode: TranslationMode = values["default_translation_mode"]
        return SettingsRecord(
            default_translation_mode=mode,
            deepseek_model=str(values.get("deepseek_model", "deepseek-v4-flash")),
            qwen_fallback_enabled=bool(values.get("qwen_fallback_enabled", False)),
            revision=int(row["revision"]),
        )

    def update(
        self,
        *,
        default_translation_mode: TranslationMode | None = None,
        deepseek_model: str | None = None,
        qwen_fallback_enabled: bool | None = None,
    ) -> SettingsRecord:
        current = self.read()
        mode = default_translation_mode or current.default_translation_mode
        model = deepseek_model or current.deepseek_model
        fallback = (
            current.qwen_fallback_enabled
            if qwen_fallback_enabled is None
            else qwen_fallback_enabled
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM app_settings WHERE key = ?",
                (_PUBLIC_SETTINGS_KEY,),
            ).fetchone()
            revision = 1 if row is None else int(row["revision"]) + 1
            value_json = json.dumps(
                {
                    "default_translation_mode": mode,
                    "deepseek_model": model,
                    "qwen_fallback_enabled": fallback,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO app_settings (key, value_json, revision, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (_PUBLIC_SETTINGS_KEY, value_json, revision, datetime.now(UTC).isoformat()),
            )
        return SettingsRecord(
            default_translation_mode=mode,
            deepseek_model=model,
            qwen_fallback_enabled=fallback,
            revision=revision,
        )

    def update_translation_mode(self, mode: TranslationMode) -> SettingsRecord:
        return self.update(default_translation_mode=mode)
