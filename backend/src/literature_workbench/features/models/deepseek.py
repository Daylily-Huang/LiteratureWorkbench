from __future__ import annotations

import json
import re
from typing import Any


_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class InvalidDeepSeekModel(ValueError):
    pass


class InvalidDeepSeekResponse(ValueError):
    pass


class DeepSeekAdapter:
    def build_request(self, *, model: str, prompt: str) -> dict[str, object]:
        if _SAFE_MODEL.fullmatch(model) is None:
            raise InvalidDeepSeekModel("invalid_deepseek_model")
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }

    def parse_response(self, raw: bytes) -> str:
        try:
            payload: Any = json.loads(raw)
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
            if not isinstance(content, str) or not content:
                raise ValueError
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise InvalidDeepSeekResponse("invalid_deepseek_response") from None
        return content
