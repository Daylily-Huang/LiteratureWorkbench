from __future__ import annotations

import hashlib
import json
import time
from typing import NoReturn
from uuid import uuid4

import httpx

from literature_workbench.db.database import Database
from literature_workbench.features.egress.audit_repository import (
    AuditAttempt,
    EgressAuditRepository,
)
from literature_workbench.features.egress.models import VerifiedSelectionProof
from literature_workbench.features.models.deepseek import (
    DeepSeekAdapter,
    InvalidDeepSeekModel,
    InvalidDeepSeekResponse,
)
from literature_workbench.platform.windows.credentials import SecretStore


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOTAL_REQUEST_TIMEOUT_SECONDS = 30.0
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
    ) -> None:
        self._database = database
        self._secret_store = secret_store
        self._audit = EgressAuditRepository(database, process_generation)
        self._adapter = DeepSeekAdapter()
        self._transport = transport

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

    def _verify_selection(self, proof: VerifiedSelectionProof) -> str:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.display_name,p.normalized_text
                FROM documents AS d
                JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
                JOIN document_versions AS v
                  ON v.id=? AND v.document_id=d.id AND v.content_sha256=d.content_sha256
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
        if row is None or proof.verified_text not in str(row["normalized_text"]):
            raise EgressRejected("selection_not_current")
        return str(row["display_name"])

    def _serialize(self, model: str, prompt: str) -> bytes:
        try:
            payload = self._adapter.build_request(model=model, prompt=prompt)
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
        started = time.monotonic()
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
        if expected_response is not None and result.strip() != expected_response:
            self._fail(attempt, "configuration_probe_failed", started)
        latency = max(0, int((time.monotonic() - started) * 1000))
        self._audit.finish(
            attempt,
            outcome="succeeded",
            detail_code=None,
            latency_ms=latency,
        )
        return expected_response if expected_response is not None else result

    def _check_total_deadline(
        self,
        attempt: AuditAttempt,
        deadline: float,
        started: float,
    ) -> None:
        if time.monotonic() >= deadline:
            self._fail(attempt, "deepseek_total_timeout", started)

    def _fail(self, attempt: AuditAttempt, code: str, started: float) -> NoReturn:
        latency = max(0, int((time.monotonic() - started) * 1000))
        self._audit.finish(
            attempt,
            outcome="failed",
            detail_code=code,
            latency_ms=latency,
        )
        raise EgressRejected(code)
