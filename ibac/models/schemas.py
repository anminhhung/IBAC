"""
Pydantic schemas cho hệ thống IBAC (Intent-Based Access Control).

Các model này là nền tảng dữ liệu xuyên suốt toàn bộ pipeline:
  RequestContext → IntentParserOutput → AuthorizationTuple → ToolResult
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Intent Parser Schemas
# ---------------------------------------------------------------------------

class Capability(BaseModel):
    """
    Một khả năng cụ thể được cấp cho request, ánh xạ 1-1 với một authorization tuple.

    Ví dụ:
        Capability(agent="email", tool="send", resource="bob@company.com",
                   reasoning="Người dùng yêu cầu gửi email cho Bob")
    """
    agent: str = Field(..., description="Tên agent: 'email', 'file', 'calendar', 'contacts'")
    tool: str = Field(..., description="Tên tool: 'send', 'read', 'write', 'lookup', 'search'")
    resource: str = Field(..., description="Resource cụ thể: email address, file path, '*' cho wildcard")
    reasoning: str = Field(..., description="Lý do tại sao capability này được cấp")

    def to_tuple_object_id(self) -> str:
        """Chuyển thành object ID dùng trong OpenFGA tuple."""
        return f"tool_invocation:{self.agent}:{self.tool}#{self.resource}"

    def matches(self, agent: str, tool: str, resource: str) -> bool:
        """
        Kiểm tra capability có khớp với tool call không.
        Hỗ trợ wildcard '*' ở resource.
        """
        if self.agent != agent or self.tool != tool:
            return False
        if self.resource == "*":
            return True
        return self.resource == resource


class DeniedImplicit(BaseModel):
    """Ghi lại các pattern bị từ chối ngầm định (không có trong intent)."""
    pattern: str = Field(..., description="Pattern bị từ chối, ví dụ: 'email:send#*'")
    reasoning: str = Field(..., description="Lý do từ chối")


class PlanStep(BaseModel):
    """
    Một bước trong kế hoạch thực thi được Intent Parser tạo ra.

    Giúp audit trail: người dùng và operator có thể thấy
    agent sẽ làm gì trước khi thực thi.
    """
    step: int = Field(..., ge=1, description="Số thứ tự bước, bắt đầu từ 1")
    action: str = Field(..., description="Loại hành động: 'resolve_contact', 'read_file', 'send_email'...")
    detail: str = Field(..., description="Mô tả chi tiết bước này")
    tool: str = Field(..., description="Tool sẽ dùng, format: 'agent:tool#resource'")


class IntentParserOutput(BaseModel):
    """
    Output đầy đủ từ Intent Parser sau khi phân tích yêu cầu người dùng.

    Ví dụ với yêu cầu "Gửi tóm tắt báo cáo cho Bob":
        plan: [resolve_contact(Bob), read_file(report.pdf), send_email(bob@company.com)]
        capabilities: [contacts:lookup#bob, file:read#/docs/report.pdf, email:send#bob@company.com]
        denied_implicit: [email:send#*, file:write#*]
    """
    plan: list[PlanStep] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    denied_implicit: list[DeniedImplicit] = Field(default_factory=list)

    def get_capability(self, agent: str, tool: str, resource: str) -> Capability | None:
        """Tìm capability khớp với (agent, tool, resource)."""
        for cap in self.capabilities:
            if cap.matches(agent, tool, resource):
                return cap
        return None

    def has_wildcard_write(self) -> bool:
        """
        Cảnh báo nếu có wildcard write/send — đây là nguyên nhân gây ra
        3 vụ vi phạm trong permissive mode của bài báo.
        """
        dangerous = {"write", "send", "delete", "exec"}
        return any(
            cap.tool in dangerous and cap.resource == "*"
            for cap in self.capabilities
        )


# ---------------------------------------------------------------------------
# Authorization Tuple Schemas
# ---------------------------------------------------------------------------

class AuthorizationTuple(BaseModel):
    """
    Một authorization tuple được lưu trong FGA store.

    Format trong OpenFGA:
        (user:{request_id}, can_invoke, tool_invocation:{agent}:{tool}#{resource})

    TTL được tính theo số turns để ngăn permission accumulation:
        tuple hết hạn khi: current_turn - created_turn > ttl
    """
    request_id: str = Field(..., description="ID của request, scope các quyền trong request này")
    agent: str
    tool: str
    resource: str
    created_turn: int = Field(..., ge=0, description="Turn number lúc tuple được tạo")
    ttl: int = Field(default=3, ge=1, description="Số turns tuple còn hiệu lực")

    def is_valid(self, current_turn: int) -> bool:
        """Kiểm tra tuple còn trong TTL không."""
        return (current_turn - self.created_turn) <= self.ttl

    def to_object_id(self) -> str:
        return f"tool_invocation:{self.agent}:{self.tool}#{self.resource}"

    def to_user_id(self) -> str:
        return f"user:{self.request_id}"


class DenyPolicy(BaseModel):
    """
    Deny policy được cấu hình lúc deploy — cấm tuyệt đối, không thể escalate.

    Ví dụ mặc định:
        DenyPolicy(agent="shell", tool="exec", resource="*",
                   reason="Không bao giờ cho phép thực thi shell command")
    """
    agent: str = Field(..., description="Agent bị cấm, '*' cho tất cả")
    tool: str = Field(..., description="Tool bị cấm, '*' cho tất cả")
    resource: str = Field(..., description="Resource bị cấm, '*' cho tất cả")
    reason: str = Field(..., description="Lý do deny policy này tồn tại")

    def matches(self, agent: str, tool: str, resource: str) -> bool:
        """Kiểm tra deny policy có áp dụng cho (agent, tool, resource) không."""
        agent_match = self.agent == "*" or self.agent == agent
        tool_match = self.tool == "*" or self.tool == tool
        resource_match = self.resource == "*" or self._resource_match(resource)
        return agent_match and tool_match and resource_match

    def _resource_match(self, resource: str) -> bool:
        if self.resource == resource:
            return True
        # Hỗ trợ prefix wildcard: "/etc/*" khớp "/etc/passwd"
        if self.resource.endswith("*"):
            prefix = self.resource[:-1]
            return resource.startswith(prefix)
        return False


# ---------------------------------------------------------------------------
# Tool Execution Result Schemas
# ---------------------------------------------------------------------------

class ToolResult(BaseModel):
    """
    Kết quả sau khi gọi invoke_tool_with_auth().

    Có 3 trạng thái:
        1. success=True  → tool chạy thành công, data chứa kết quả
        2. denied=True, can_escalate=True  → bị từ chối vì không có trong intent,
                                              người dùng có thể approve
        3. denied=True, can_escalate=False → bị cấm bởi deny policy, không thể override
    """
    success: bool = False
    denied: bool = False
    reason: Literal["deny_policy", "not_in_intent"] | None = None
    can_escalate: bool = False
    escalation_prompt: str | None = None
    data: Any = None

    @classmethod
    def allow(cls, data: Any = None) -> "ToolResult":
        return cls(success=True, data=data)

    @classmethod
    def deny_not_in_intent(cls, agent: str, tool: str, resource: str) -> "ToolResult":
        prompt = (
            f"Agent muốn thực hiện '{agent}:{tool}' trên '{resource}'. "
            f"Hành động này không nằm trong yêu cầu ban đầu. Cho phép không?"
        )
        return cls(
            denied=True,
            reason="not_in_intent",
            can_escalate=True,
            escalation_prompt=prompt,
        )

    @classmethod
    def deny_policy(cls, agent: str, tool: str, resource: str) -> "ToolResult":
        return cls(
            denied=True,
            reason="deny_policy",
            can_escalate=False,
            escalation_prompt=None,
        )


# ---------------------------------------------------------------------------
# Request Context Schema
# ---------------------------------------------------------------------------

class RequestContext(BaseModel):
    """
    Ngữ cảnh được tạo ra lúc bắt đầu mỗi request, TRƯỚC khi agent chạy.

    contacts là nguồn dữ liệu DUY NHẤT được tin tưởng cho phân quyền:
        - Không load từ email history (có thể bị attacker kiểm soát)
        - Không load từ calendar entries (có thể chứa phishing invite)
        - Chỉ load từ address book đã xác minh của người dùng
    """
    request_id: str = Field(..., description="UUID duy nhất cho request này")
    contacts: dict[str, str] = Field(
        default_factory=dict,
        description="Ánh xạ tên → địa chỉ đã xác minh, ví dụ: {'Bob': 'bob@company.com'}"
    )
    current_turn: int = Field(default=0, ge=0, description="Turn number hiện tại trong hội thoại")
    scope_mode: Literal["strict", "permissive"] = Field(
        default="strict",
        description="Chế độ phân quyền: strict=tối thiểu, permissive=bao gồm prerequisites"
    )

    def resolve_contact(self, name: str) -> str | None:
        """
        Tra cứu tên người dùng → địa chỉ email từ trusted contact store.
        Tìm kiếm case-insensitive.
        """
        name_lower = name.lower()
        for key, address in self.contacts.items():
            if key.lower() == name_lower:
                return address
        return None

    def advance_turn(self) -> "RequestContext":
        """Trả về context mới với turn tăng thêm 1."""
        return self.model_copy(update={"current_turn": self.current_turn + 1})
