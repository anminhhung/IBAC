"""
Component 6: Escalation Protocol

Khi agent gặp tool call bị từ chối (not_in_intent), EscalationHandler hỏi
người dùng và — nếu được approve — ghi tuple mới qua Intent Parser.

Hai đảm bảo bảo mật quan trọng từ bài báo:

  1. Escalation prompt được tạo từ PARAMS của tool call bị từ chối,
     KHÔNG phải từ text mà agent viết ra.
     → Injection không thể kiểm soát nội dung prompt hiển thị cho user.

  2. User approval đi qua Intent Parser (LLM cô lập),
     không đi thẳng vào FGA store.
     → Intent Parser có thể từ chối nếu approval không hợp lý.
"""

from __future__ import annotations

from typing import Protocol

from ibac.models.schemas import AuthorizationTuple, Capability, RequestContext, ToolResult
from ibac.authorization.tuple_manager import TupleManager


# ---------------------------------------------------------------------------
# User Approval Callback Protocol
# ---------------------------------------------------------------------------

class UserApprovalCallback(Protocol):
    """Interface nhận Yes/No từ người dùng."""
    async def ask(self, prompt: str) -> bool: ...


class CliApprovalCallback:
    """Callback CLI đơn giản — hỏi trực tiếp trên terminal."""
    async def ask(self, prompt: str) -> bool:
        answer = input(f"\n[IBAC] {prompt} (y/n): ").strip().lower()
        return answer in ("y", "yes", "có", "co")


# ---------------------------------------------------------------------------
# Escalation Handler
# ---------------------------------------------------------------------------

class EscalationHandler:
    """
    Xử lý escalation khi tool call bị từ chối vì không có trong intent.

    Flow:
      1. Kiểm tra escalation_count < max_escalations
      2. Hiển thị prompt (tạo từ tool params, không từ agent text)
      3. Nhận Yes/No từ user qua callback
      4. Nếu Yes → Intent Parser tạo capability từ approval
      5. Ghi tuple mới vào FGA qua TupleManager
    """

    def __init__(
        self,
        intent_parser,
        tuple_manager: TupleManager,
        approval_callback: UserApprovalCallback,
        max_escalations: int = 5,
    ) -> None:
        if max_escalations < 1:
            raise ValueError(f"max_escalations phải >= 1, nhận: {max_escalations}")
        self._parser = intent_parser
        self._tuple_manager = tuple_manager
        self._callback = approval_callback
        self.max_escalations = max_escalations
        self._count: int = 0

    @property
    def escalation_count(self) -> int:
        return self._count

    def reset(self) -> None:
        """Reset counter — gọi lúc bắt đầu request mới."""
        self._count = 0

    async def handle(
        self,
        tool_result: ToolResult,
        agent: str,
        tool: str,
        resource: str,
        context: RequestContext,
    ) -> AuthorizationTuple | None:
        """
        Xử lý một denied tool call.

        Args:
            tool_result: ToolResult với denied=True, can_escalate=True
            agent/tool/resource: params của tool call bị từ chối
            context: RequestContext hiện tại (chứa request_id, current_turn)

        Returns:
            AuthorizationTuple mới nếu user approve, None nếu từ chối hoặc hết quota.
        """
        if not tool_result.can_escalate:
            return None  # Deny policy — không escalate được

        if self._count >= self.max_escalations:
            raise EscalationLimitReached(
                f"Đã đạt giới hạn {self.max_escalations} lần escalation trong request này."
            )

        # Prompt được tạo từ params của tool call — không từ agent text
        prompt = _build_escalation_prompt(agent, tool, resource)

        approved = await self._callback.ask(prompt)
        self._count += 1

        if not approved:
            return None

        # User approve → Intent Parser tạo capability từ approval
        # Input cho parser: chỉ là prompt + "yes" — không có agent reasoning
        approval_message = f"Approved: {prompt}"
        parser_output = self._parser.parse(approval_message, context)

        if not parser_output.capabilities:
            return None

        # Ghi tuple đầu tiên phù hợp (capability mới nhất)
        cap = _find_matching_capability(parser_output.capabilities, agent, tool, resource)
        if cap is None:
            cap = parser_output.capabilities[0]

        written = self._tuple_manager.write_tuples(
            request_id=context.request_id,
            capabilities=[cap],
            current_turn=context.current_turn,
        )
        return written[0] if written else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_escalation_prompt(agent: str, tool: str, resource: str) -> str:
    """
    Tạo escalation prompt từ params của tool call.
    KHÔNG dùng text từ agent hay document content.
    """
    action_map = {
        ("email",    "send"):   f"gửi email tới '{resource}'",
        ("email",    "read"):   f"đọc email",
        ("email",    "search"): f"tìm kiếm email",
        ("file",     "read"):   f"đọc file '{resource}'",
        ("file",     "write"):  f"ghi file '{resource}'",
        ("file",     "search"): f"tìm kiếm file",
        ("file",     "delete"): f"xóa file '{resource}'",
        ("calendar", "read"):   f"đọc lịch",
        ("calendar", "create"): f"tạo sự kiện với '{resource}'",
        ("calendar", "search"): f"tìm kiếm lịch",
        ("contacts", "lookup"): f"tra cứu thông tin của '{resource}'",
        ("contacts", "search"): f"tìm kiếm danh bạ",
        ("web",      "search"): f"tìm kiếm web cho '{resource}'",
    }
    action = action_map.get((agent, tool), f"thực hiện {agent}:{tool} trên '{resource}'")
    return f"Agent muốn {action}. Cho phép không?"


def _find_matching_capability(
    capabilities: list[Capability], agent: str, tool: str, resource: str
) -> Capability | None:
    """Tìm capability khớp nhất với tool call bị từ chối."""
    for cap in capabilities:
        if cap.agent == agent and cap.tool == tool:
            return cap
    return None


# ---------------------------------------------------------------------------
# Custom Exception
# ---------------------------------------------------------------------------

class EscalationLimitReached(Exception):
    """Raised khi vượt quá max_escalations trong một request."""
    pass
