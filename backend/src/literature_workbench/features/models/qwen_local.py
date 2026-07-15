from __future__ import annotations

import json
from typing import Any

import httpx

from literature_workbench.features.models.qwen_process import QWEN_ALIAS


QWEN_CHAT_COMPLETIONS_URL = "http://127.0.0.1:18080/v1/chat/completions"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class QwenLocalError(RuntimeError):
    pass


class QwenLocalClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def translate(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": QWEN_ALIAS,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise QwenLocalError("qwen_request_too_large")

        try:
            with httpx.Client(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(120.0, connect=2.0),
            ) as client:
                with client.stream(
                    "POST",
                    QWEN_CHAT_COMPLETIONS_URL,
                    headers={"Content-Type": "application/json"},
                    content=body,
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise QwenLocalError("qwen_http_error")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise QwenLocalError("qwen_response_too_large")
                        raw.extend(chunk)
        except QwenLocalError:
            raise
        except (httpx.HTTPError, OSError):
            raise QwenLocalError("qwen_transport_failed") from None

        try:
            payload: Any = json.loads(raw)
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content:
                raise ValueError
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise QwenLocalError("invalid_qwen_response") from None
        return content
