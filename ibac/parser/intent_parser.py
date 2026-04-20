"""
Component 2: Intent Parser

Một LLM call riêng biệt, CÔ LẬP hoàn toàn với agent chính.
Phân tích yêu cầu người dùng và tạo ra bộ quyền tối thiểu (IntentParserOutput).

Tại sao phải cô lập?
  Nếu dùng cùng LLM call với agent, injection có thể ảnh hưởng
  đến cả bước phân tích quyền. Intent Parser chỉ nhận 2 input:
    1. user_message (trusted — từ người dùng trực tiếp)
    2. contacts map (trusted — từ ContactStore)
  Không bao giờ nhận output từ tool call hay document content.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from ibac.models.schemas import (
    Capability,
    DeniedImplicit,
    IntentParserOutput,
    PlanStep,
    RequestContext,
)


# ---------------------------------------------------------------------------
# LLM Client Protocol — dễ mock trong tests
# ---------------------------------------------------------------------------

class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str:
        """Gửi một LLM call và trả về text response."""
        ...


# ---------------------------------------------------------------------------
# System Prompts
# ---------------------------------------------------------------------------

_STRICT_SYSTEM_PROMPT = """You are a security-focused intent parser for an AI agent authorization system.

Your job: analyze the user's request and extract the MINIMUM set of capabilities needed.

STRICT MODE rules:
- Only authorize actions EXPLICITLY stated in the user's request.
- Do NOT infer prerequisites. Do NOT add "helpful" extras.
- If the user says "email Bob the report", grant ONLY:
    email:send to Bob's address, file:read for the named report.
  Do NOT add contacts:lookup, file:search, or anything else.
- Resource must be SPECIFIC (a named file, a specific email address).
  Use "*" only when the user explicitly says "any" or "all".
- When the user names a person, resolve them using the contacts map provided.
  If a name is not in the contacts map, use the name as-is in the resource field.

Available agents and tools:
  email:   send, read, search, delete
  file:    read, write, search, delete
  calendar: read, create, search, delete
  contacts: lookup, search
  web:     search

Output valid JSON only, no markdown, no explanation. Schema:
{
  "plan": [{"step": int, "action": str, "detail": str, "tool": "agent:tool#resource"}],
  "capabilities": [{"agent": str, "tool": str, "resource": str, "reasoning": str}],
  "denied_implicit": [{"pattern": str, "reasoning": str}]
}"""

_PERMISSIVE_SYSTEM_PROMPT = """You are an intent parser for an AI agent authorization system.

Your job: analyze the user's request and extract capabilities — including reasonable prerequisites.

PERMISSIVE MODE rules:
- Authorize actions EXPLICITLY stated AND their direct prerequisites.
- A prerequisite is something the agent MUST do to fulfill the request
  (e.g., "email Bob" requires contacts:lookup to resolve Bob's address).
- Include clearly implied supporting actions (e.g., "prepare for my meeting"
  implies calendar:read, contacts:lookup for attendees).
- Still be conservative: do NOT grant write/send/delete permissions unless
  clearly needed. Avoid wildcard "*" for write/send/delete resources.
- When the user names a person, resolve them using the contacts map provided.

Available agents and tools:
  email:   send, read, search, delete
  file:    read, write, search, delete
  calendar: read, create, search, delete
  contacts: lookup, search
  web:     search

Output valid JSON only, no markdown, no explanation. Schema:
{
  "plan": [{"step": int, "action": str, "detail": str, "tool": "agent:tool#resource"}],
  "capabilities": [{"agent": str, "tool": str, "resource": str, "reasoning": str}],
  "denied_implicit": [{"pattern": str, "reasoning": str}]
}"""


def _build_user_prompt(user_message: str, context: RequestContext) -> str:
    contacts_str = json.dumps(context.contacts, ensure_ascii=False, indent=2)
    return f"""Contacts map (trusted — use these to resolve names to addresses):
{contacts_str}

User request:
{user_message}"""


# ---------------------------------------------------------------------------
# Intent Parser
# ---------------------------------------------------------------------------

class IntentParser:
    """
    Phân tích yêu cầu người dùng và tạo IntentParserOutput.

    Đây là LLM call DUY NHẤT được phép chạy trên trusted input.
    Mọi LLM call khác (agent chính) đều chạy trên untrusted content.

    Ví dụ:
        parser = IntentParser(llm_client, scope_mode="strict")
        output = parser.parse("Gửi báo cáo cho Bob", ctx)
        # output.capabilities = [email:send#bob@company.com, file:read#/docs/report.pdf]
    """

    def __init__(self, llm_client: LLMClient, scope_mode: str = "strict") -> None:
        if scope_mode not in ("strict", "permissive"):
            raise ValueError(f"scope_mode phải là 'strict' hoặc 'permissive', nhận được: '{scope_mode}'")
        self._llm = llm_client
        self.scope_mode = scope_mode
        self._system_prompt = (
            _STRICT_SYSTEM_PROMPT if scope_mode == "strict" else _PERMISSIVE_SYSTEM_PROMPT
        )

    def parse(self, user_message: str, context: RequestContext) -> IntentParserOutput:
        """
        Phân tích user_message và trả về IntentParserOutput.

        Pipeline:
          1. Gọi LLM với system prompt cô lập + user_message + contacts map
          2. Parse JSON response
          3. Resolve tên người → địa chỉ email từ ContactStore
          4. Trả về IntentParserOutput đã validated
        """
        user_prompt = _build_user_prompt(user_message, context)
        raw_response = self._llm.complete(system=self._system_prompt, user=user_prompt)
        raw_output = self._parse_json(raw_response)
        output = self._build_output(raw_output)
        return self._resolve_contacts_in_output(output, context)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_json(self, response: str) -> dict:
        """Extract và parse JSON từ LLM response."""
        text = response.strip()
        # Strip markdown code fences nếu LLM vẫn bọc trong ```json ... ```
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            text = match.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Intent Parser trả về JSON không hợp lệ: {e}\nResponse: {response!r}")
        if not isinstance(data, dict):
            raise ValueError(f"Intent Parser phải trả về JSON object, nhận được: {type(data)}")
        return data

    def _build_output(self, data: dict) -> IntentParserOutput:
        """Chuyển raw dict thành IntentParserOutput, bỏ qua field lạ an toàn."""
        plan = [PlanStep(**s) for s in data.get("plan", [])]
        capabilities = [Capability(**c) for c in data.get("capabilities", [])]
        denied_implicit = [DeniedImplicit(**d) for d in data.get("denied_implicit", [])]
        return IntentParserOutput(
            plan=plan,
            capabilities=capabilities,
            denied_implicit=denied_implicit,
        )

    def _resolve_contacts_in_output(
        self, output: IntentParserOutput, context: RequestContext
    ) -> IntentParserOutput:
        """
        Thay thế tên người trong capabilities bằng địa chỉ đã xác minh từ ContactStore.

        Ví dụ: resource="bob" → resource="bob@company.com"

        Chỉ thay thế khi resource trông giống tên người (không có '@' và không phải path).
        Nếu không resolve được, giữ nguyên giá trị gốc từ LLM.
        """
        resolved_caps = []
        for cap in output.capabilities:
            resource = cap.resource
            if self._looks_like_name(resource):
                resolved = context.resolve_contact(resource)
                if resolved:
                    resource = resolved
            resolved_caps.append(cap.model_copy(update={"resource": resource}))

        return output.model_copy(update={"capabilities": resolved_caps})

    @staticmethod
    def _looks_like_name(resource: str) -> bool:
        """
        Heuristic: resource trông như tên người nếu không chứa '@', '/', '#', '*'.
        Ví dụ: "bob" → True, "bob@company.com" → False, "/docs/file.pdf" → False
        """
        return bool(resource) and not any(c in resource for c in ("@", "/", "#", "*", ":"))
