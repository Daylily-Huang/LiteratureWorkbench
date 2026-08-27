from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from collections.abc import Callable
from typing import NoReturn
from uuid import UUID, uuid4

import httpx

from literature_workbench.db.database import Database
from literature_workbench.features.egress.audit_repository import (
    AuditAttempt,
    EgressAuditRepository,
)
from literature_workbench.features.egress.billing import (
    BalanceSnapshot,
    DeepSeekBillingStatus,
    DeepSeekUsageRepository,
    SiteUsage,
    parse_balance_response,
    price_usage,
    unknown_usage_charge,
)
from literature_workbench.features.egress.models import VerifiedSelectionProof
from literature_workbench.features.models.deepseek import (
    DeepSeekAdapter,
    InvalidDeepSeekModel,
    InvalidDeepSeekResponse,
)
from literature_workbench.features.translation.repository import (
    current_page_text_candidates,
    match_selection_to_candidates,
    normalize_selection,
)
from literature_workbench.platform.windows.credentials import SecretStore


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BALANCE_RESPONSE_BYTES = 64 * 1024
TOTAL_REQUEST_TIMEOUT_SECONDS = 30.0
BALANCE_CACHE_SECONDS = 60.0
BALANCE_FORCE_REFRESH_MIN_INTERVAL_SECONDS = 15.0
_FIXED_PROBE_PROMPT = "Reply with exactly: model configuration ok"


class EgressRejected(RuntimeError):
    pass


class DeepSeekGateway:
    def __init__(
        self,
        database: Database,
        secret_store: SecretStore,
        *,
        process_generation: str,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._database = database
        self._secret_store = secret_store
        self._audit = EgressAuditRepository(database, process_generation)
        self._adapter = DeepSeekAdapter()
        self._transport = transport
        self._monotonic = monotonic or time.monotonic
        self._usage = DeepSeekUsageRepository(database)
        self._balance_lock = threading.Lock()
        self._balance_cache: tuple[float, BalanceSnapshot] | None = None
        self._balance_last_attempt_at: float | None = None
        self._balance_last_error: str | None = None

    def translate_selection(self, proof: VerifiedSelectionProof) -> str:
        display_name = self._verify_selection(proof)
        prompt = (
            "Translate the following server-verified PDF selection to "
            f"{proof.target_language}. Return only the translation.\n\n"
            f"<selection>\n{proof.verified_text}\n</selection>"
        )
        body = self._serialize(proof.model, prompt)
        attempt = self._audit.start(
            task_id=str(proof.task_id),
            authorization_type="registered_library",
            document_id=str(proof.document_id),
            content_sha256=proof.content_sha256,
            display_file_name=display_name,
            model=proof.model,
            purpose="translate_selection",
            payload_kind="verified_selection",
            payload_bytes=len(body),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )
        return self._send(attempt, body)

    def answer_question(
        self,
        *,
        task_id: str,
        document_id: UUID | str | None,
        content_sha256: str | None,
        display_file_name: str | None,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        purpose: str = "research_question",
    ) -> str:
        """Answer a research question over retrieved fragments.

        The caller (research service) builds the prompt; this method keeps
        the full egress trail — audit, usage accounting, retry visibility —
        identical to translation traffic.  ``purpose`` distinguishes the
        main answer call from auxiliary calls such as query expansion.
        Document context is optional because library-wide calls have no
        single source document.
        """
        body = self._serialize(model, prompt, system_prompt=system_prompt)
        attempt = self._audit.start(
            task_id=task_id,
            authorization_type="registered_library",
            document_id=None if document_id is None else str(document_id),
            content_sha256=content_sha256,
            display_file_name=display_file_name,
            model=model,
            purpose=purpose,
            payload_kind="research_prompt",
            payload_bytes=len(body),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )
        return self._send(attempt, body)

    def configuration_probe(self, model: str) -> str:
        body = self._serialize(model, _FIXED_PROBE_PROMPT)
        attempt = self._audit.start(
            task_id=f"configuration-probe:{uuid4()}",
            authorization_type="configured_probe",
            document_id=None,
            content_sha256=None,
            display_file_name=None,
            model=model,
            purpose="configuration_probe",
            payload_kind="fixed_probe",
            payload_bytes=len(body),
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )
        return self._send(
            attempt,
            body,
            expected_response="model configuration ok",
        )

    def billing_status(self, *, force: bool = False) -> DeepSeekBillingStatus:
        site_usage = self._usage.summary()
        now = self._monotonic()
        with self._balance_lock:
            cached = self._balance_cache
            if not force and cached is not None and now - cached[0] < BALANCE_CACHE_SECONDS:
                snapshot = cached[1]
                return DeepSeekBillingStatus(
                    available=snapshot.available,
                    balances=snapshot.balances,
                    site_usage=site_usage,
                    refreshed_at=snapshot.refreshed_at,
                    stale=False,
                )
            if (
                force
                and self._balance_last_attempt_at is not None
                and now - self._balance_last_attempt_at
                < BALANCE_FORCE_REFRESH_MIN_INTERVAL_SECONDS
            ):
                return self._balance_status_from_cache(
                    cached,
                    site_usage=site_usage,
                    error_code=self._balance_last_error,
                )
            self._balance_last_attempt_at = now
            try:
                snapshot = self._fetch_balance()
            except EgressRejected as error:
                self._balance_last_error = str(error)
                return self._balance_status_from_cache(
                    cached, site_usage=site_usage, error_code=str(error)
                )
            self._balance_last_error = None
            self._balance_cache = (self._monotonic(), snapshot)
        return DeepSeekBillingStatus(
            available=snapshot.available,
            balances=snapshot.balances,
            site_usage=site_usage,
            refreshed_at=snapshot.refreshed_at,
            stale=False,
        )

    @staticmethod
    def _balance_status_from_cache(
        cached: tuple[float, BalanceSnapshot] | None,
        *,
        site_usage: SiteUsage,
        error_code: str | None,
    ) -> DeepSeekBillingStatus:
        if cached is None:
            return DeepSeekBillingStatus(
                available=False,
                balances=[],
                site_usage=site_usage,
                refreshed_at=None,
                stale=True,
                error_code=error_code,
            )
        snapshot = cached[1]
        return DeepSeekBillingStatus(
            available=snapshot.available,
            balances=snapshot.balances,
            site_usage=site_usage,
            refreshed_at=snapshot.refreshed_at,
            stale=error_code is not None,
            error_code=error_code,
        )

    def _verify_selection(self, proof: VerifiedSelectionProof) -> str:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.display_name,p.normalized_text,p.raw_text,p.blocks_json
                FROM documents AS d
                JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
                JOIN document_versions AS v
                  ON v.id=? AND v.document_id=d.id AND v.content_sha256=d.content_sha256
                 AND v.id=(
                   SELECT latest.id FROM document_versions AS latest
                   WHERE latest.document_id=d.id
                   ORDER BY latest.observed_at DESC,latest.rowid DESC LIMIT 1
                 )
                JOIN contents AS c
                  ON c.sha256=d.content_sha256 AND c.mime_type='application/pdf'
                JOIN parse_runs AS r
                  ON r.id=c.current_parse_run_id
                 AND r.content_sha256=c.sha256
                 AND r.status='completed'
                JOIN pages AS p
                  ON p.parse_run_id=r.id
                 AND p.content_sha256=c.sha256
                 AND p.physical_page=?
                WHERE d.id=?
                  AND d.content_sha256=?
                  AND d.format='pdf'
                  AND d.status IN ('active','candidate_pdf','valid_pdf')
                """,
                (
                    str(proof.document_version_id),
                    proof.physical_page,
                    str(proof.document_id),
                    proof.content_sha256,
                ),
            ).fetchone()
        if row is None:
            raise EgressRejected("selection_not_current")
        normalized_selection = normalize_selection(proof.verified_text)
        if (
            match_selection_to_candidates(
                current_page_text_candidates(row), normalized_selection
            )
            is None
        ):
            raise EgressRejected("selection_text_mismatch")
        return str(row["display_name"])

    def _serialize(
        self,
        model: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
    ) -> bytes:
        try:
            payload = self._adapter.build_request(
                model=model, prompt=prompt, system_prompt=system_prompt
            )
        except InvalidDeepSeekModel:
            raise EgressRejected("invalid_deepseek_model") from None
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise EgressRejected("deepseek_request_too_large")
        return body

    def _send(
        self,
        attempt: AuditAttempt,
        body: bytes,
        *,
        expected_response: str | None = None,
    ) -> str:
        started = self._monotonic()
        deadline = started + TOTAL_REQUEST_TIMEOUT_SECONDS
        try:
            secret = self._secret_store.get_deepseek_key()
        except (OSError, RuntimeError):
            self._fail(attempt, "credential_store_failed", started)
        if secret is None:
            self._fail(attempt, "deepseek_key_unavailable", started)
        assert secret is not None
        try:
            with httpx.Client(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30.0, connect=5.0),
            ) as client:
                with client.stream(
                    "POST",
                    DEEPSEEK_CHAT_COMPLETIONS_URL,
                    headers={
                        "Authorization": f"Bearer {secret.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                    content=body,
                ) as response:
                    self._check_total_deadline(attempt, deadline, started)
                    if response.status_code < 200 or response.status_code >= 300:
                        if response.status_code in {401, 403}:
                            self._fail(attempt, "deepseek_authorization_failed", started)
                        if (
                            response.status_code in {408, 425, 429}
                            or response.status_code >= 500
                        ):
                            self._fail(attempt, "deepseek_http_error", started)
                        self._fail(attempt, "deepseek_request_rejected", started)
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        self._check_total_deadline(attempt, deadline, started)
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            self._fail(attempt, "deepseek_response_too_large", started)
                        raw.extend(chunk)
        except EgressRejected:
            raise
        except (httpx.HTTPError, OSError):
            self._fail(attempt, "deepseek_transport_failed", started)
        try:
            result = self._adapter.parse_response(bytes(raw))
        except InvalidDeepSeekResponse:
            self._fail(attempt, "invalid_deepseek_response", started)
        self._check_total_deadline(attempt, deadline, started)
        if expected_response is not None and result.content.strip() != expected_response:
            self._fail(attempt, "configuration_probe_failed", started)
        latency = max(0, int((self._monotonic() - started) * 1000))
        self._audit.finish(
            attempt,
            outcome="succeeded",
            detail_code=None,
            latency_ms=latency,
            charge=(
                unknown_usage_charge()
                if result.usage is None
                else price_usage(attempt.model, result.usage)
            ),
        )
        return expected_response if expected_response is not None else result.content

    def _fetch_balance(self) -> BalanceSnapshot:
        try:
            secret = self._secret_store.get_deepseek_key()
        except (OSError, RuntimeError):
            raise EgressRejected("credential_store_failed") from None
        if secret is None:
            raise EgressRejected("deepseek_key_unavailable")
        attempt = self._audit.start(
            task_id=f"balance-refresh:{uuid4()}",
            authorization_type="configured_balance",
            document_id=None,
            content_sha256=None,
            display_file_name=None,
            model="deepseek-balance",
            purpose="balance_refresh",
            payload_kind="none",
            payload_bytes=0,
            payload_sha256=hashlib.sha256(b"").hexdigest(),
        )
        started = self._monotonic()
        try:
            with httpx.Client(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                with client.stream(
                    "GET",
                    DEEPSEEK_BALANCE_URL,
                    headers={"Authorization": f"Bearer {secret.get_secret_value()}"},
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        if response.status_code in {401, 403}:
                            raise EgressRejected("deepseek_authorization_failed")
                        if response.status_code == 402:
                            raise EgressRejected("deepseek_balance_unavailable")
                        raise EgressRejected("deepseek_balance_request_failed")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > MAX_BALANCE_RESPONSE_BYTES:
                            raise EgressRejected("deepseek_balance_response_too_large")
                        raw.extend(chunk)
        except EgressRejected as error:
            self._finish_balance_audit(attempt, str(error), started)
            raise
        except (httpx.HTTPError, OSError):
            code = "deepseek_balance_transport_failed"
            self._finish_balance_audit(attempt, code, started)
            raise EgressRejected(code) from None
        try:
            snapshot = parse_balance_response(bytes(raw), refreshed_at=datetime.now(UTC))
        except ValueError:
            code = "invalid_deepseek_balance_response"
            self._finish_balance_audit(attempt, code, started)
            raise EgressRejected(code) from None
        self._audit.finish(
            attempt,
            outcome="succeeded",
            detail_code=None,
            latency_ms=max(0, int((self._monotonic() - started) * 1000)),
        )
        return snapshot

    def _finish_balance_audit(
        self, attempt: AuditAttempt, code: str, started: float
    ) -> None:
        self._audit.finish(
            attempt,
            outcome="failed",
            detail_code=code,
            latency_ms=max(0, int((self._monotonic() - started) * 1000)),
        )

    def _check_total_deadline(
        self,
        attempt: AuditAttempt,
        deadline: float,
        started: float,
    ) -> None:
        if self._monotonic() >= deadline:
            self._fail(attempt, "deepseek_total_timeout", started)

    def _fail(self, attempt: AuditAttempt, code: str, started: float) -> NoReturn:
        latency = max(0, int((self._monotonic() - started) * 1000))
        self._audit.finish(
            attempt,
            outcome="failed",
            detail_code=code,
            latency_ms=latency,
        )
        raise EgressRejected(code)
