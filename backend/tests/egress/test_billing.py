from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.egress.audit_repository import EgressAuditRepository
from literature_workbench.features.egress.billing import (
    DeepSeekUsageRepository,
    parse_balance_response,
    price_usage,
)
from literature_workbench.features.models.deepseek import DeepSeekTokenUsage


def test_versioned_price_uses_cache_hit_miss_and_output_tokens() -> None:
    usage = DeepSeekTokenUsage(
        prompt_cache_hit_tokens=100,
        prompt_cache_miss_tokens=20,
        completion_tokens=10,
        total_tokens=130,
    )

    flash = price_usage("deepseek-v4-flash", usage)
    pro = price_usage("deepseek-v4-pro", usage)
    unknown = price_usage("future-model", usage)

    assert flash.cost_nano_cny == 42_000
    assert pro.cost_nano_cny == 122_500
    assert flash.pricing_version == "deepseek-cny-2026-07-17"
    assert unknown.cost_nano_cny is None
    assert unknown.pricing is None


def test_audit_finish_records_usage_and_exact_decimal_summary(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "billing.sqlite3")
    run_migrations(database)
    audit = EgressAuditRepository(database, "generation")
    attempt = audit.start(
        task_id="task",
        authorization_type="configured_probe",
        document_id=None,
        content_sha256=None,
        display_file_name=None,
        model="deepseek-v4-flash",
        purpose="configuration_probe",
        payload_kind="fixed_probe",
        payload_bytes=10,
        payload_sha256="a" * 64,
    )
    usage = DeepSeekTokenUsage(100, 20, 10, 130)

    audit.finish(
        attempt,
        outcome="succeeded",
        detail_code=None,
        latency_ms=12,
        charge=price_usage(attempt.model, usage),
    )

    summary = DeepSeekUsageRepository(database).summary(
        now=datetime(2026, 7, 17, 12, tzinfo=UTC),
    )
    assert summary.total == "0.000042"
    assert summary.month == "0.000042"
    assert summary.pricing_unknown_requests == 0
    assert summary.started_at is not None


def test_unknown_model_is_counted_without_inventing_a_price(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "unknown.sqlite3")
    run_migrations(database)
    audit = EgressAuditRepository(database, "generation")
    attempt = audit.start(
        task_id="task",
        authorization_type="configured_probe",
        document_id=None,
        content_sha256=None,
        display_file_name=None,
        model="future-model",
        purpose="configuration_probe",
        payload_kind="fixed_probe",
        payload_bytes=10,
        payload_sha256="a" * 64,
    )
    usage = DeepSeekTokenUsage(0, 4, 2, 6)
    audit.finish(
        attempt,
        outcome="succeeded",
        detail_code=None,
        latency_ms=12,
        charge=price_usage(attempt.model, usage),
    )

    summary = DeepSeekUsageRepository(database).summary()

    assert summary.total == "0"
    assert summary.pricing_unknown_requests == 1


def test_balance_response_keeps_decimal_strings_and_rejects_malformed_values() -> None:
    snapshot = parse_balance_response(
        b'{"is_available":true,"balance_infos":[{"currency":"CNY",'
        b'"total_balance":"12.3400","granted_balance":"2.34",'
        b'"topped_up_balance":"10"}]}',
        refreshed_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    assert snapshot.available is True
    assert snapshot.balances[0].total_balance == "12.3400"

    with pytest.raises(ValueError, match="invalid_deepseek_balance_response"):
        parse_balance_response(
            b'{"is_available":true,"balance_infos":[{"currency":"CNY",'
            b'"total_balance":"NaN","granted_balance":"0",'
            b'"topped_up_balance":"0"}]}',
            refreshed_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
