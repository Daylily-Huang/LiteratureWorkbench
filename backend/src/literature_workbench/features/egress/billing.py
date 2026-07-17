from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from literature_workbench.db.database import Database
from literature_workbench.features.models.deepseek import DeepSeekTokenUsage


PRICING_VERSION = "deepseek-cny-2026-07-17"
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_NANO_PER_CNY = Decimal(1_000_000_000)


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    cache_hit_nano_cny_per_token: int
    cache_miss_nano_cny_per_token: int
    output_nano_cny_per_token: int


@dataclass(frozen=True, slots=True)
class UsageCharge:
    usage: DeepSeekTokenUsage | None
    cost_nano_cny: int | None
    pricing_version: str | None
    pricing: PricingSnapshot | None


_FLASH_PRICING = PricingSnapshot(20, 1_000, 2_000)
_PRO_PRICING = PricingSnapshot(25, 3_000, 6_000)
_PRICES: dict[str, PricingSnapshot] = {
    "deepseek-v4-flash": _FLASH_PRICING,
    "deepseek-chat": _FLASH_PRICING,
    "deepseek-reasoner": _FLASH_PRICING,
    "deepseek-v4-pro": _PRO_PRICING,
}


def price_usage(model: str, usage: DeepSeekTokenUsage) -> UsageCharge:
    pricing = _PRICES.get(model)
    if pricing is None:
        return UsageCharge(usage, None, None, None)
    cost = (
        usage.prompt_cache_hit_tokens * pricing.cache_hit_nano_cny_per_token
        + usage.prompt_cache_miss_tokens * pricing.cache_miss_nano_cny_per_token
        + usage.completion_tokens * pricing.output_nano_cny_per_token
    )
    return UsageCharge(usage, cost, PRICING_VERSION, pricing)


def unknown_usage_charge() -> UsageCharge:
    return UsageCharge(None, None, None, None)


class BalanceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total_balance: str
    granted_balance: str
    topped_up_balance: str


class SiteUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str = "CNY"
    today: str
    month: str
    total: str
    started_at: str | None
    pricing_unknown_requests: int = Field(ge=0)


class DeepSeekBillingStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    balances: list[BalanceInfo]
    site_usage: SiteUsage
    refreshed_at: str | None
    stale: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    available: bool
    balances: list[BalanceInfo]
    refreshed_at: str


def parse_balance_response(raw: bytes, *, refreshed_at: datetime) -> BalanceSnapshot:
    try:
        payload = json.loads(raw)
        available = payload["is_available"]
        values = payload["balance_infos"]
        if not isinstance(available, bool) or not isinstance(values, list) or len(values) > 8:
            raise ValueError
        balances: list[BalanceInfo] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError
            currency = value["currency"]
            if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
                raise ValueError
            balances.append(BalanceInfo(
                currency=currency,
                total_balance=_decimal_string(value["total_balance"]),
                granted_balance=_decimal_string(value["granted_balance"]),
                topped_up_balance=_decimal_string(value["topped_up_balance"]),
            ))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, InvalidOperation):
        raise ValueError("invalid_deepseek_balance_response") from None
    return BalanceSnapshot(
        available=available,
        balances=balances,
        refreshed_at=refreshed_at.astimezone(UTC).isoformat(),
    )


def _decimal_string(value: object) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError
    raw = str(value)
    if len(raw) > 64:
        raise ValueError
    parsed = Decimal(raw)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError
    return format(parsed, "f")


def _money_string(nano_cny: int) -> str:
    value = Decimal(nano_cny) / _NANO_PER_CNY
    return format(value, ".9f").rstrip("0").rstrip(".") or "0"


class DeepSeekUsageRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def summary(self, *, now: datetime | None = None) -> SiteUsage:
        current = (now or datetime.now(UTC)).astimezone()
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today_start.replace(day=1)
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                  MIN(recorded_at) AS started_at,
                  COALESCE(SUM(cost_nano_cny),0) AS total_cost,
                  COALESCE(SUM(CASE WHEN recorded_at>=? THEN cost_nano_cny ELSE 0 END),0)
                    AS month_cost,
                  COALESCE(SUM(CASE WHEN recorded_at>=? THEN cost_nano_cny ELSE 0 END),0)
                    AS today_cost,
                  COALESCE(SUM(CASE WHEN cost_nano_cny IS NULL THEN 1 ELSE 0 END),0)
                    AS unknown_count
                FROM deepseek_usage
                """,
                (
                    month_start.astimezone(UTC).isoformat(),
                    today_start.astimezone(UTC).isoformat(),
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("deepseek_usage_summary_failed")
        return SiteUsage(
            today=_money_string(int(row["today_cost"])),
            month=_money_string(int(row["month_cost"])),
            total=_money_string(int(row["total_cost"])),
            started_at=None if row["started_at"] is None else str(row["started_at"]),
            pricing_unknown_requests=int(row["unknown_count"]),
        )
