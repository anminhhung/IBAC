"""
Component 5: Tool Execution Wrapper

invoke_tool_with_auth() là cổng duy nhất để chạy bất kỳ tool nào.
Không tool nào được phép chạy mà không qua hàm này.

Flow theo bài báo:
    async function invokeToolWithAuth(fga, requestId, agent, tool, resource, execute):
        auth = await fga.check(...)
        if !auth.allowed:
            isBlocked = await fga.check(..., "blocked")
            return { denied, reason, canEscalate, escalationPrompt }
        return { success, data: await execute() }
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from ibac.models.schemas import ToolResult


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

async def invoke_tool_with_auth(
    fga_client,
    request_id: str,
    agent: str,
    tool: str,
    resource: str,
    execute: Callable,
    current_turn: int,
) -> ToolResult:
    """
    Kiểm tra quyền rồi chạy tool.

    Args:
        fga_client:   InMemoryFGAClient (hoặc bất kỳ client nào có .check())
        request_id:   ID của request hiện tại
        agent:        Tên agent: "email", "file", "calendar", "contacts"
        tool:         Tên tool: "send", "read", "write", "search", "lookup"
        resource:     Resource cụ thể: "bob@company.com", "/docs/report.pdf"
        execute:      Callable sync hoặc async — hàm tool thực sự
        current_turn: Turn number hiện tại (dùng để check TTL)

    Returns:
        ToolResult.allow(data)        nếu được phép và chạy thành công
        ToolResult.deny_not_in_intent nếu không có tuple (canEscalate=True)
        ToolResult.deny_policy        nếu bị deny policy (canEscalate=False)
    """
    result = fga_client.check(request_id, agent, tool, resource, current_turn)

    if not result.allowed:
        if result.blocked:
            return ToolResult.deny_policy(agent, tool, resource)
        return ToolResult.deny_not_in_intent(agent, tool, resource)

    # Được phép — chạy tool, hỗ trợ cả sync, async function, và coroutine object
    import asyncio
    ret = execute()
    if asyncio.iscoroutine(ret):
        data = await ret
    else:
        data = ret

    return ToolResult.allow(data)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def require_auth(agent: str, tool: str, resource_param: str):
    """
    Decorator bọc một async tool function với invoke_tool_with_auth.

    Tool được wrap phải nhận thêm 3 keyword args từ caller:
        _fga_client, _request_id, _current_turn

    Ví dụ:
        @require_auth(agent="email", tool="send", resource_param="recipient")
        async def send_email(recipient: str, subject: str, body: str):
            ...  # implementation không cần biết về auth

        # Gọi từ agent:
        result = await send_email(
            recipient="bob@company.com",
            subject="Report",
            body="...",
            _fga_client=fga,
            _request_id="req_abc",
            _current_turn=2,
        )
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> ToolResult:
            fga_client   = kwargs.pop("_fga_client")
            request_id   = kwargs.pop("_request_id")
            current_turn = kwargs.pop("_current_turn")
            resource     = kwargs.get(resource_param, "")

            return await invoke_tool_with_auth(
                fga_client=fga_client,
                request_id=request_id,
                agent=agent,
                tool=tool,
                resource=resource,
                execute=lambda: func(*args, **kwargs),
                current_turn=current_turn,
            )
        # Đánh dấu để phân biệt wrapped vs unwrapped
        wrapper._ibac_protected = True
        wrapper._ibac_agent = agent
        wrapper._ibac_tool = tool
        return wrapper
    return decorator
