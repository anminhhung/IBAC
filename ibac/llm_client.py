"""
LLM Client thực tế dùng OpenAI-compatible API.

Implement LLMClient Protocol từ intent_parser.py.
Dùng cho cả Intent Parser và Agent chính.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


# ---------------------------------------------------------------------------
# Response types cho complete_with_tools
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """Một tool call từ LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Response từ LLM, có thể chứa tool_calls hoặc text thuần."""
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


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

    def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        """
        Gửi messages + tool definitions, trả về LLMResponse.
        Nếu LLM quyết định gọi tool → LLMResponse.tool_calls có giá trị.
        Nếu LLM trả lời trực tiếp → LLMResponse.content có giá trị.
        """
        import json as _json
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            extra_body={
                "cache": {"no-cache": True},
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        msg = response.choices[0].message
        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = _json.loads(tc.function.arguments)
                except (_json.JSONDecodeError, AttributeError):
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return LLMResponse(content=msg.content, tool_calls=tool_calls)
