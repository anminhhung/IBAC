"""
Unit tests cho Phase 1: Pydantic Schemas.

Chạy: pytest ibac/tests/test_schemas.py -v
"""

import pytest
from ibac.models.schemas import (
    Capability,
    DeniedImplicit,
    PlanStep,
    IntentParserOutput,
    AuthorizationTuple,
    DenyPolicy,
    ToolResult,
    RequestContext,
)


# ---------------------------------------------------------------------------
# Tests: Capability
# ---------------------------------------------------------------------------

class TestCapability:
    def test_to_tuple_object_id(self):
        cap = Capability(
            agent="email", tool="send",
            resource="bob@company.com", reasoning="test"
        )
        assert cap.to_tuple_object_id() == "tool_invocation:email:send#bob@company.com"

    def test_matches_exact(self):
        cap = Capability(agent="file", tool="read", resource="/docs/report.pdf", reasoning="test")
        assert cap.matches("file", "read", "/docs/report.pdf")
        assert not cap.matches("file", "read", "/docs/other.pdf")
        assert not cap.matches("file", "write", "/docs/report.pdf")

    def test_matches_wildcard_resource(self):
        cap = Capability(agent="contacts", tool="lookup", resource="*", reasoning="test")
        assert cap.matches("contacts", "lookup", "bob")
        assert cap.matches("contacts", "lookup", "alice@company.com")
        assert not cap.matches("email", "lookup", "bob")

    def test_no_cross_agent_match(self):
        cap = Capability(agent="email", tool="send", resource="*", reasoning="test")
        assert not cap.matches("calendar", "send", "*")


# ---------------------------------------------------------------------------
# Tests: IntentParserOutput
# ---------------------------------------------------------------------------

class TestIntentParserOutput:
    def setup_method(self):
        self.output = IntentParserOutput(
            plan=[
                PlanStep(step=1, action="resolve_contact", detail="Resolve Bob", tool="contacts:lookup#bob"),
                PlanStep(step=2, action="read_file", detail="Read report", tool="file:read#/docs/report.pdf"),
                PlanStep(step=3, action="send_email", detail="Email Bob", tool="email:send#bob@company.com"),
            ],
            capabilities=[
                Capability(agent="contacts", tool="lookup", resource="bob", reasoning="resolve"),
                Capability(agent="file", tool="read", resource="/docs/report.pdf", reasoning="read report"),
                Capability(agent="email", tool="send", resource="bob@company.com", reasoning="send to Bob"),
            ],
            denied_implicit=[
                DeniedImplicit(pattern="email:send#*", reasoning="Chỉ Bob được phép"),
                DeniedImplicit(pattern="file:write#*", reasoning="Không yêu cầu chỉnh sửa"),
            ]
        )

    def test_get_capability_found(self):
        cap = self.output.get_capability("email", "send", "bob@company.com")
        assert cap is not None
        assert cap.agent == "email"

    def test_get_capability_not_found(self):
        cap = self.output.get_capability("email", "send", "attacker@evil.com")
        assert cap is None

    def test_has_wildcard_write_false(self):
        assert not self.output.has_wildcard_write()

    def test_has_wildcard_write_true(self):
        dangerous = IntentParserOutput(
            capabilities=[
                Capability(agent="email", tool="send", resource="*", reasoning="quá rộng"),
            ]
        )
        assert dangerous.has_wildcard_write()

    def test_has_wildcard_delete_true(self):
        dangerous = IntentParserOutput(
            capabilities=[
                Capability(agent="file", tool="delete", resource="*", reasoning="quá rộng"),
            ]
        )
        assert dangerous.has_wildcard_write()


# ---------------------------------------------------------------------------
# Tests: AuthorizationTuple
# ---------------------------------------------------------------------------

class TestAuthorizationTuple:
    def test_is_valid_within_ttl(self):
        t = AuthorizationTuple(request_id="req_abc", agent="email", tool="send",
                               resource="bob@company.com", created_turn=1, ttl=3)
        assert t.is_valid(current_turn=1)  # turn 1: delta=0
        assert t.is_valid(current_turn=3)  # turn 3: delta=2
        assert t.is_valid(current_turn=4)  # turn 4: delta=3 = ttl

    def test_is_valid_expired(self):
        t = AuthorizationTuple(request_id="req_abc", agent="email", tool="send",
                               resource="bob@company.com", created_turn=1, ttl=3)
        assert not t.is_valid(current_turn=5)  # turn 5: delta=4 > ttl

    def test_to_object_id(self):
        t = AuthorizationTuple(request_id="req_abc", agent="file", tool="read",
                               resource="/docs/report.pdf", created_turn=0, ttl=3)
        assert t.to_object_id() == "tool_invocation:file:read#/docs/report.pdf"

    def test_to_user_id(self):
        t = AuthorizationTuple(request_id="req_abc", agent="email", tool="send",
                               resource="bob@company.com", created_turn=0, ttl=3)
        assert t.to_user_id() == "user:req_abc"

    def test_ttl_boundary_exact(self):
        # Đúng tại biên TTL: current_turn - created_turn == ttl → vẫn hợp lệ
        t = AuthorizationTuple(request_id="req", agent="email", tool="send",
                               resource="*", created_turn=2, ttl=2)
        assert t.is_valid(current_turn=4)   # delta=2 == ttl → valid
        assert not t.is_valid(current_turn=5)  # delta=3 > ttl → expired


# ---------------------------------------------------------------------------
# Tests: DenyPolicy
# ---------------------------------------------------------------------------

class TestDenyPolicy:
    def test_exact_match(self):
        policy = DenyPolicy(agent="shell", tool="exec", resource="rm", reason="nguy hiểm")
        assert policy.matches("shell", "exec", "rm")
        assert not policy.matches("shell", "exec", "ls")

    def test_wildcard_agent(self):
        policy = DenyPolicy(agent="*", tool="exec", resource="*", reason="cấm tất cả exec")
        assert policy.matches("shell", "exec", "anything")
        assert policy.matches("system", "exec", "cmd")

    def test_wildcard_resource(self):
        policy = DenyPolicy(agent="shell", tool="exec", resource="*", reason="cấm shell")
        assert policy.matches("shell", "exec", "rm")
        assert policy.matches("shell", "exec", "cat /etc/passwd")

    def test_prefix_wildcard_etc(self):
        policy = DenyPolicy(agent="*", tool="*", resource="/etc/*", reason="cấm đọc /etc/")
        assert policy.matches("file", "read", "/etc/passwd")
        assert policy.matches("file", "read", "/etc/shadow")
        assert not policy.matches("file", "read", "/home/user/.config")

    def test_prefix_wildcard_ssh(self):
        policy = DenyPolicy(agent="*", tool="*", resource="~/.ssh/*", reason="cấm đọc SSH")
        assert policy.matches("file", "read", "~/.ssh/id_rsa")
        assert not policy.matches("file", "read", "~/.bashrc")

    def test_no_cross_match(self):
        policy = DenyPolicy(agent="shell", tool="exec", resource="*", reason="shell")
        assert not policy.matches("email", "send", "attacker@evil.com")


# ---------------------------------------------------------------------------
# Tests: ToolResult
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_allow(self):
        result = ToolResult.allow(data={"content": "hello"})
        assert result.success is True
        assert result.denied is False
        assert result.data == {"content": "hello"}
        assert result.can_escalate is False

    def test_deny_not_in_intent(self):
        result = ToolResult.deny_not_in_intent("email", "send", "attacker@evil.com")
        assert result.denied is True
        assert result.success is False
        assert result.reason == "not_in_intent"
        assert result.can_escalate is True
        assert result.escalation_prompt is not None
        assert "attacker@evil.com" in result.escalation_prompt

    def test_deny_policy(self):
        result = ToolResult.deny_policy("shell", "exec", "rm")
        assert result.denied is True
        assert result.success is False
        assert result.reason == "deny_policy"
        assert result.can_escalate is False
        assert result.escalation_prompt is None

    def test_escalation_prompt_contains_resource(self):
        result = ToolResult.deny_not_in_intent("file", "read", "/sensitive/data.csv")
        assert "/sensitive/data.csv" in result.escalation_prompt
        assert "file" in result.escalation_prompt
        assert "read" in result.escalation_prompt


# ---------------------------------------------------------------------------
# Tests: RequestContext
# ---------------------------------------------------------------------------

class TestRequestContext:
    def setup_method(self):
        self.ctx = RequestContext(
            request_id="req-test-001",
            contacts={
                "Bob": "bob@company.com",
                "Alice": "alice@corp.org",
                "the team": "eng-team@company.com",
            },
            current_turn=0,
            scope_mode="strict",
        )

    def test_resolve_contact_exact(self):
        assert self.ctx.resolve_contact("Bob") == "bob@company.com"
        assert self.ctx.resolve_contact("Alice") == "alice@corp.org"

    def test_resolve_contact_case_insensitive(self):
        assert self.ctx.resolve_contact("bob") == "bob@company.com"
        assert self.ctx.resolve_contact("BOB") == "bob@company.com"
        assert self.ctx.resolve_contact("ALICE") == "alice@corp.org"

    def test_resolve_contact_not_found(self):
        # Tên không tồn tại trong danh bạ → trả về None, không resolve từ nguồn khác
        assert self.ctx.resolve_contact("attacker") is None
        assert self.ctx.resolve_contact("unknown@evil.com") is None

    def test_resolve_contact_cannot_be_injected(self):
        # Injection attempt: tên có chứa địa chỉ attacker không được resolve
        assert self.ctx.resolve_contact("bob@company.com OR attacker@evil.com") is None

    def test_advance_turn(self):
        ctx2 = self.ctx.advance_turn()
        assert ctx2.current_turn == 1
        assert self.ctx.current_turn == 0  # immutable — original không đổi

    def test_advance_turn_multiple(self):
        ctx = self.ctx
        for i in range(1, 6):
            ctx = ctx.advance_turn()
            assert ctx.current_turn == i

    def test_scope_mode_default_strict(self):
        ctx = RequestContext(request_id="req-002", contacts={})
        assert ctx.scope_mode == "strict"

    def test_contacts_not_loaded_from_untrusted(self):
        # Contact Store chỉ chứa những gì được truyền vào lúc khởi tạo
        # Không thể tự động thêm từ email history hay calendar
        ctx = RequestContext(
            request_id="req-003",
            contacts={"Bob": "bob@company.com"},
        )
        assert len(ctx.contacts) == 1
        assert ctx.resolve_contact("Eve") is None
