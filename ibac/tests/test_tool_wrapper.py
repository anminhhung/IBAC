"""
Unit tests cho Phase 6: Tool Execution Wrapper.

Chạy: pytest ibac/tests/test_tool_wrapper.py -v
"""

import pytest
from ibac.executor.tool_wrapper import invoke_tool_with_auth, require_auth
from ibac.authorization.fga_client import InMemoryFGAClient, CheckResult
from ibac.authorization.deny_policies import load_default_deny_policies
from ibac.models.schemas import AuthorizationTuple, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fga(allow: list[tuple] = None, deny_defaults: bool = False) -> InMemoryFGAClient:
    fga = InMemoryFGAClient()
    if deny_defaults:
        load_default_deny_policies(fga)
    for req_id, agent, tool, resource in (allow or []):
        fga.write_allow(AuthorizationTuple(
            request_id=req_id, agent=agent, tool=tool,
            resource=resource, created_turn=0, ttl=3,
        ))
    return fga


async def _noop():
    return "tool_output"


# ---------------------------------------------------------------------------
# Tests: invoke_tool_with_auth — 3 kết quả
# ---------------------------------------------------------------------------

class TestInvokeAllowed:
    @pytest.mark.asyncio
    async def test_allowed_executes_tool(self):
        fga = _make_fga([("req_1", "email", "send", "bob@company.com")])
        result = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "bob@company.com", _noop, current_turn=0
        )
        assert result.success is True
        assert result.data == "tool_output"
        assert result.denied is False

    @pytest.mark.asyncio
    async def test_allowed_returns_tool_data(self):
        fga = _make_fga([("req_1", "file", "read", "/docs/report.pdf")])
        result = await invoke_tool_with_auth(
            fga, "req_1", "file", "read", "/docs/report.pdf",
            lambda: {"content": "hello"}, current_turn=0
        )
        assert result.data == {"content": "hello"}

    @pytest.mark.asyncio
    async def test_allowed_async_execute(self):
        fga = _make_fga([("req_1", "calendar", "read", "*")])
        called = []

        async def async_tool():
            called.append(True)
            return "calendar_data"

        result = await invoke_tool_with_auth(
            fga, "req_1", "calendar", "read", "*", async_tool, current_turn=0
        )
        assert result.success is True
        assert called == [True]

    @pytest.mark.asyncio
    async def test_allowed_sync_execute(self):
        fga = _make_fga([("req_1", "contacts", "lookup", "bob")])
        result = await invoke_tool_with_auth(
            fga, "req_1", "contacts", "lookup", "bob",
            lambda: "bob@company.com", current_turn=0
        )
        assert result.success is True
        assert result.data == "bob@company.com"


class TestInvokeDeniedNotInIntent:
    @pytest.mark.asyncio
    async def test_denied_no_tuple(self):
        fga = _make_fga()  # store rỗng
        result = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "attacker@evil.com", _noop, current_turn=0
        )
        assert result.denied is True
        assert result.success is False
        assert result.reason == "not_in_intent"
        assert result.can_escalate is True
        assert result.escalation_prompt is not None

    @pytest.mark.asyncio
    async def test_denied_wrong_resource(self):
        fga = _make_fga([("req_1", "email", "send", "bob@company.com")])
        result = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "attacker@evil.com", _noop, current_turn=0
        )
        assert result.denied is True
        assert result.can_escalate is True

    @pytest.mark.asyncio
    async def test_denied_execute_not_called(self):
        fga = _make_fga()
        called = []
        await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "attacker@evil.com",
            lambda: called.append(True), current_turn=0
        )
        assert called == []

    @pytest.mark.asyncio
    async def test_escalation_prompt_contains_resource(self):
        fga = _make_fga()
        result = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "attacker@evil.com", _noop, current_turn=0
        )
        assert "attacker@evil.com" in result.escalation_prompt

    @pytest.mark.asyncio
    async def test_denied_expired_ttl(self):
        fga = InMemoryFGAClient()
        fga.write_allow(AuthorizationTuple(
            request_id="req_1", agent="email", tool="send",
            resource="bob@company.com", created_turn=0, ttl=2,
        ))
        result = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "bob@company.com", _noop, current_turn=5
        )
        assert result.denied is True
        assert result.can_escalate is True  # expired ≠ blocked


class TestInvokeDeniedByPolicy:
    @pytest.mark.asyncio
    async def test_blocked_shell_exec(self):
        fga = _make_fga(deny_defaults=True)
        result = await invoke_tool_with_auth(
            fga, "req_1", "shell", "exec", "rm -rf /", _noop, current_turn=0
        )
        assert result.denied is True
        assert result.reason == "deny_policy"
        assert result.can_escalate is False
        assert result.escalation_prompt is None

    @pytest.mark.asyncio
    async def test_blocked_etc_passwd(self):
        fga = _make_fga(deny_defaults=True)
        result = await invoke_tool_with_auth(
            fga, "req_1", "file", "read", "/etc/passwd", _noop, current_turn=0
        )
        assert result.denied is True
        assert result.can_escalate is False

    @pytest.mark.asyncio
    async def test_blocked_even_with_allow_tuple(self):
        """Deny policy thắng tuyệt đối ngay cả khi có allow tuple."""
        fga = _make_fga(
            allow=[("req_1", "shell", "exec", "rm")],
            deny_defaults=True,
        )
        result = await invoke_tool_with_auth(
            fga, "req_1", "shell", "exec", "rm", _noop, current_turn=0
        )
        assert result.denied is True
        assert result.can_escalate is False

    @pytest.mark.asyncio
    async def test_blocked_execute_not_called(self):
        fga = _make_fga(deny_defaults=True)
        called = []
        await invoke_tool_with_auth(
            fga, "req_1", "shell", "exec", "ls",
            lambda: called.append(True), current_turn=0
        )
        assert called == []


# ---------------------------------------------------------------------------
# Tests: @require_auth decorator
# ---------------------------------------------------------------------------

class TestRequireAuthDecorator:
    @pytest.mark.asyncio
    async def test_decorator_allows_when_permitted(self):
        fga = _make_fga([("req_1", "email", "send", "bob@company.com")])

        @require_auth(agent="email", tool="send", resource_param="recipient")
        async def send_email(recipient: str, body: str):
            return f"sent to {recipient}"

        result = await send_email(
            recipient="bob@company.com",
            body="Hello",
            _fga_client=fga,
            _request_id="req_1",
            _current_turn=0,
        )
        assert result.success is True
        assert result.data == "sent to bob@company.com"

    @pytest.mark.asyncio
    async def test_decorator_denies_when_not_permitted(self):
        fga = _make_fga()

        @require_auth(agent="email", tool="send", resource_param="recipient")
        async def send_email(recipient: str, body: str):
            return f"sent to {recipient}"

        result = await send_email(
            recipient="attacker@evil.com",
            body="exfil",
            _fga_client=fga,
            _request_id="req_1",
            _current_turn=0,
        )
        assert result.denied is True
        assert result.can_escalate is True

    @pytest.mark.asyncio
    async def test_decorator_blocks_denied_policy(self):
        fga = _make_fga(deny_defaults=True)

        @require_auth(agent="shell", tool="exec", resource_param="cmd")
        async def run_shell(cmd: str):
            return f"ran {cmd}"

        result = await run_shell(
            cmd="rm -rf /",
            _fga_client=fga,
            _request_id="req_1",
            _current_turn=0,
        )
        assert result.denied is True
        assert result.can_escalate is False

    def test_decorator_sets_protected_flag(self):
        @require_auth(agent="email", tool="send", resource_param="recipient")
        async def send_email(recipient: str):
            pass

        assert send_email._ibac_protected is True
        assert send_email._ibac_agent == "email"
        assert send_email._ibac_tool == "send"

    def test_decorator_preserves_function_name(self):
        @require_auth(agent="file", tool="read", resource_param="path")
        async def read_file(path: str):
            """Đọc file."""
            pass

        assert read_file.__name__ == "read_file"
        assert read_file.__doc__ == "Đọc file."

    @pytest.mark.asyncio
    async def test_decorator_passes_args_to_tool(self):
        fga = _make_fga([("req_1", "file", "read", "/docs/report.pdf")])
        received = {}

        @require_auth(agent="file", tool="read", resource_param="path")
        async def read_file(path: str, encoding: str = "utf-8"):
            received["path"] = path
            received["encoding"] = encoding
            return "content"

        await read_file(
            path="/docs/report.pdf",
            encoding="latin-1",
            _fga_client=fga,
            _request_id="req_1",
            _current_turn=0,
        )
        assert received["path"] == "/docs/report.pdf"
        assert received["encoding"] == "latin-1"


# ---------------------------------------------------------------------------
# Tests: Kịch bản từ bài báo
# ---------------------------------------------------------------------------

class TestPaperScenarios:
    @pytest.mark.asyncio
    async def test_injection_create_calendar_event_blocked(self):
        """
        Bài báo: Agent bị injection, thử create_calendar_event 10 lần.
        IBAC từ chối tất cả vì không có calendar:write tuple.
        """
        fga = _make_fga([("req_1", "calendar", "read", "*")])
        injection_attempts = 10
        for _ in range(injection_attempts):
            result = await invoke_tool_with_auth(
                fga, "req_1", "calendar", "create",
                "mark.black-2134@gmail.com", _noop, current_turn=0
            )
            assert result.denied is True
            assert result.can_escalate is True

    @pytest.mark.asyncio
    async def test_injection_exfiltrate_email_blocked(self):
        """
        Agent bị injection, thử gửi email ra attacker@evil.com.
        Chỉ có tuple cho bob@company.com → bị từ chối.
        """
        fga = _make_fga([("req_1", "email", "send", "bob@company.com")])
        result = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "attacker@evil.com", _noop, current_turn=0
        )
        assert result.denied is True
        assert result.can_escalate is True
        assert "attacker@evil.com" in result.escalation_prompt

    @pytest.mark.asyncio
    async def test_legitimate_task_completes(self):
        """Task hợp lệ vẫn hoàn thành dù có injection xung quanh."""
        fga = _make_fga([
            ("req_1", "calendar", "read", "*"),
            ("req_1", "email", "send", "bob@company.com"),
        ])

        # Đọc calendar → hợp lệ
        r1 = await invoke_tool_with_auth(
            fga, "req_1", "calendar", "read", "*", lambda: "The Gourmet Restaurant", current_turn=0
        )
        assert r1.success is True
        assert r1.data == "The Gourmet Restaurant"

        # Injection gửi cho attacker → bị chặn
        r2 = await invoke_tool_with_auth(
            fga, "req_1", "email", "send", "attacker@evil.com", _noop, current_turn=0
        )
        assert r2.denied is True
