from __future__ import annotations

import json
import time
from threading import Event, enumerate as enumerate_threads

import httpx
import pytest

from literature_workbench.features.models.qwen_local import (
    QWEN_CHAT_COMPLETIONS_URL,
    QwenLocalClient,
    QwenLocalError,
)


def test_translation_uses_one_fixed_loopback_non_streaming_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert str(request.url) == QWEN_CHAT_COMPLETIONS_URL
        assert body == {
            "model": "literature-workbench-qwen",
            "messages": [{"role": "user", "content": "translate this"}],
            "stream": False,
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "译文"}}]},
            request=request,
        )

    assert QwenLocalClient(transport=httpx.MockTransport(handler)).translate("translate this") == "译文"
    assert len(calls) == 1


def test_client_ignores_proxy_environment_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    from literature_workbench.features.models import qwen_local

    original = qwen_local.httpx.Client
    captured: dict[str, object] = {}

    def recording_client(**kwargs: object) -> httpx.Client:
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(qwen_local.httpx, "Client", recording_client)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )
    )
    assert QwenLocalClient(transport=transport).translate("fixed") == "ok"
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_request_over_64_kib_is_rejected_before_transport() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, request=request)

    with pytest.raises(QwenLocalError, match="qwen_request_too_large"):
        QwenLocalClient(transport=httpx.MockTransport(handler)).translate("x" * 65_536)
    assert calls == []


def test_response_over_2_mib_is_sanitized() -> None:
    sentinel = "private-model-output"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(sentinel + "x" * (2 * 1024 * 1024)).encode(),
            request=request,
        )

    with pytest.raises(QwenLocalError) as caught:
        QwenLocalClient(transport=httpx.MockTransport(handler)).translate("fixed")
    assert str(caught.value) == "qwen_response_too_large"
    assert sentinel not in str(caught.value)


def test_slow_drip_response_is_closed_at_monotonic_total_deadline() -> None:
    closed = Event()

    class SlowDrip(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            while not closed.wait(0.2):
                yield b" "

        def close(self) -> None:
            closed.set()

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=SlowDrip(), request=request)
    )
    started = time.monotonic()

    with pytest.raises(QwenLocalError, match="qwen_total_timeout"):
        QwenLocalClient(transport=transport, total_timeout_seconds=0.02).translate("fixed")

    assert time.monotonic() - started < 0.15
    assert closed.wait(0.1)
    assert not any(thread.name == "qwen-http-deadline" for thread in enumerate_threads())


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(500, content=b"secret diagnostic", request=request),
        lambda request: httpx.Response(200, content=b"not json", request=request),
    ],
)
def test_transport_and_response_failures_are_sanitized(response: object) -> None:
    transport = httpx.MockTransport(response)  # type: ignore[arg-type]
    with pytest.raises(QwenLocalError) as caught:
        QwenLocalClient(transport=transport).translate("fixed")
    assert "secret diagnostic" not in str(caught.value)
    assert "not json" not in str(caught.value)
