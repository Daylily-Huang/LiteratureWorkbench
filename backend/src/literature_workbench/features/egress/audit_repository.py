from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.egress.billing import UsageCharge


@dataclass(frozen=True, slots=True)
class AuditAttempt:
    id: str
    model: str
    purpose: str


class EgressAuditRepository:
    def __init__(self, database: Database, process_generation: str) -> None:
        self._database = database
        self._process_generation = process_generation

    def start(
        self,
        *,
        task_id: str,
        authorization_type: str,
        document_id: str | None,
        content_sha256: str | None,
        display_file_name: str | None,
        model: str,
        purpose: str,
        payload_kind: str,
        payload_bytes: int,
        payload_sha256: str,
    ) -> AuditAttempt:
        audit_id = str(uuid4())
        request_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_no),0)+1 AS next_attempt
                FROM egress_audit WHERE task_id=? AND purpose=?
                """,
                (task_id, purpose),
            ).fetchone()
            if row is None:
                raise RuntimeError("egress_audit_attempt_failed")
            attempt_no = int(row["next_attempt"])
            connection.execute(
                """
                INSERT INTO egress_audit(
                  id,task_id,request_id,attempt_no,process_generation,event_time,
                  authorization_type,document_id,content_sha256,display_file_name,
                  provider,model,purpose,payload_kind,payload_bytes,payload_sha256,
                  outcome,detail_code,latency_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,'deepseek',?,?,?,?,?,'started',NULL,NULL)
                """,
                (
                    audit_id,
                    task_id,
                    request_id,
                    attempt_no,
                    self._process_generation,
                    now,
                    authorization_type,
                    document_id,
                    content_sha256,
                    display_file_name,
                    model,
                    purpose,
                    payload_kind,
                    payload_bytes,
                    payload_sha256,
                ),
            )
        return AuditAttempt(audit_id, model, purpose)

    def finish(
        self,
        attempt: AuditAttempt,
        *,
        outcome: str,
        detail_code: str | None,
        latency_ms: int,
        charge: UsageCharge | None = None,
    ) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE egress_audit
                SET outcome=?,detail_code=?,latency_ms=?
                WHERE id=? AND outcome='started'
                """,
                (outcome, detail_code, latency_ms, attempt.id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("egress_audit_finish_failed")
            if charge is not None:
                pricing = charge.pricing
                connection.execute(
                    """
                    INSERT INTO deepseek_usage(
                      audit_id,recorded_at,purpose,model,prompt_cache_hit_tokens,
                      prompt_cache_miss_tokens,completion_tokens,total_tokens,
                      cost_nano_cny,pricing_version,cache_hit_nano_cny_per_token,
                      cache_miss_nano_cny_per_token,output_nano_cny_per_token)
                    SELECT id,event_time,purpose,model,?,?,?,?,?,?,?,?,?
                    FROM egress_audit WHERE id=?
                    """,
                    (
                        charge.usage.prompt_cache_hit_tokens,
                        charge.usage.prompt_cache_miss_tokens,
                        charge.usage.completion_tokens,
                        charge.usage.total_tokens,
                        charge.cost_nano_cny,
                        charge.pricing_version,
                        None if pricing is None else pricing.cache_hit_nano_cny_per_token,
                        None if pricing is None else pricing.cache_miss_nano_cny_per_token,
                        None if pricing is None else pricing.output_nano_cny_per_token,
                        attempt.id,
                    ),
                )
