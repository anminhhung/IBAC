"""
LLM Client thực tế dùng OpenAI-compatible API.

Implement LLMClient Protocol từ intent_parser.py.
Dùng cho cả Intent Parser và Agent chính.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


class QwenClient:
    """
    OpenAI-compatible client trỏ đến proxy Qwen.
    Implement LLMClient Protocol — drop-in thay thế MockLLM trong tests.
    Đọc config từ .env: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        _api_key = api_key or os.environ["LLM_API_KEY"]
        _base_url = base_url or os.environ["LLM_BASE_URL"]
        self._model = model or os.environ["LLM_MODEL"]
        self._client = OpenAI(api_key=_api_key, base_url=_base_url)

    def complete(self, system: str, user: str) -> str:
        """Gửi system + user message, trả về text response."""
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body={
                "cache": {"no-cache": True},
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        return response.choices[0].message.content
