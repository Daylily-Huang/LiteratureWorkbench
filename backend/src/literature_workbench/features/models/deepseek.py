from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class InvalidDeepSeekModel(ValueError):
    pass


class InvalidDeepSeekResponse(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DeepSeekTokenUsage:
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class DeepSeekResponse:
    content: str
    usage: DeepSeekTokenUsage | None


class DeepSeekAdapter:
    def build_request(
        self,
        *,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
    ) -> dict[str, object]:
        if _SAFE_MODEL.fullmatch(model) is None:
            raise InvalidDeepSeekModel("invalid_deepseek_model")
        messages: list[dict[str, str]] = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return {
            "model": model,
            "messages": messages,
            "stream": False,
        }

    def parse_response(self, raw: bytes) -> DeepSeekResponse:
        try:
            payload: Any = json.loads(raw)
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
            if not isinstance(content, str) or not content:
                raise ValueError
            usage_payload = payload.get("usage")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise InvalidDeepSeekResponse("invalid_deepseek_response") from None
        usage = None
        if usage_payload is not None:
            try:
                usage = self._parse_usage(usage_payload)
            except (TypeError, ValueError):
                pass
        return DeepSeekResponse(content=content, usage=usage)

    def _parse_usage(self, value: object) -> DeepSeekTokenUsage:
        if not isinstance(value, dict):
            raise ValueError

        def token(name: str, *, default: int | None = None) -> int:
            raw = value.get(name, default)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError
            return raw

        prompt_tokens = token("prompt_tokens")
        cache_hit = token("prompt_cache_hit_tokens", default=0)
        cache_miss = token(
            "prompt_cache_miss_tokens",
            default=max(0, prompt_tokens - cache_hit),
        )
        completion = token("completion_tokens")
        total = token("total_tokens")
        if cache_hit + cache_miss != prompt_tokens or total < prompt_tokens + completion:
            raise ValueError
        return DeepSeekTokenUsage(
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=cache_miss,
            completion_tokens=completion,
            total_tokens=total,
        )
