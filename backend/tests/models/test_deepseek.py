from __future__ import annotations

import json

import pytest

from literature_workbench.features.models.deepseek import (
    DeepSeekAdapter,
    InvalidDeepSeekModel,
    InvalidDeepSeekResponse,
)


def test_adapter_builds_only_a_non_streaming_chat_completion_body() -> None:
    body = DeepSeekAdapter().build_request(
        model="deepseek-chat",
        prompt="Translate the verified selection.",
    )

    assert body == {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "Translate the verified selection."}],
        "stream": False,
    }


@pytest.mark.parametrize(
    "model",
    ["", "../unsafe", "deepseek model", "x" * 129, "deepseek\nchat"],
)
def test_adapter_rejects_unsafe_or_unbounded_model_ids(model: str) -> None:
    with pytest.raises(InvalidDeepSeekModel, match="invalid_deepseek_model"):
        DeepSeekAdapter().build_request(model=model, prompt="fixed")


def test_adapter_extracts_only_the_assistant_text() -> None:
    raw = json.dumps(
        {"id": "secret-provider-id", "choices": [{"message": {"content": "译文"}}]}
    ).encode()

    assert DeepSeekAdapter().parse_response(raw) == "译文"


@pytest.mark.parametrize(
    "raw",
    [b"not-json", b"{}", b'{"choices":[]}', b'{"choices":[{"message":{}}]}'],
)
def test_adapter_failures_do_not_echo_provider_responses(raw: bytes) -> None:
    with pytest.raises(InvalidDeepSeekResponse) as caught:
        DeepSeekAdapter().parse_response(raw)

    assert str(caught.value) == "invalid_deepseek_response"
    assert raw.decode(errors="ignore") not in str(caught.value)
