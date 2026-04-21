"""
Factory và logging wrappers cho IBAC demo.

Capture toàn bộ pipeline: request → intent parse → FGA → agent loop → cleanup.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from ibac.agents.data_analytics_agent import DataAnalyticsAgent
from ibac.agents.orchestrator import (
    IbacOrchestrator,
    MAX_ITERATIONS,
    TOOL_DEFINITIONS,
    _SYSTEM_PROMPT,
)
from ibac.authorization.deny_policies import load_default_deny_policies
from ibac.authorization.fga_client import InMemoryFGAClient
from ibac.authorization.tuple_manager import TupleManager
from ibac.context.request_context import ContactStore, assemble_request_context
from ibac.escalation.escalation_handler import EscalationHandler
from ibac.executor.tool_wrapper import invoke_tool_with_auth
from ibac.llm_client import QwenClient
from ibac.models.schemas import RequestContext, ToolResult
from ibac.parser.intent_parser import IntentParser

DATA_DIR = Path(__file__).parent.parent / "sale_data"


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

@dataclass
class IBACEvent:
    type: str
    ts: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    # ── visual metadata ──────────────────────────────────────────────────
    ICONS = {
        "request_start":      "💬",
        "intent_parsing":     "🧠",
        "intent_parsed":      "🔍",
        "tuples_written":     "✅",
        "agent_turn":         "🔄",
        "llm_tool_decision":  "🤖",
        "llm_final_answer":   "💡",
        "fga_check":          "🔐",
        "tool_call":          "🔧",
        "tool_success":       "📊",
        "tool_denied":        "🚫",
        "tool_error":         "❌",
        "escalation":         "⚠️",
        "cleanup":            "🧹",
    }
    COLORS = {
        "request_start":      "#5C6BC0",
        "intent_parsing":     "#8E44AD",
        "intent_parsed":      "#4A90D9",
        "tuples_written":     "#27AE60",
        "agent_turn":         "#E67E22",
        "llm_tool_decision":  "#D35400",
        "llm_final_answer":   "#27AE60",
        "fga_check":          "#7F8C8D",
        "tool_call":          "#E67E22",
        "tool_success":       "#27AE60",
        "tool_denied":        "#E74C3C",
        "tool_error":         "#E74C3C",
        "escalation":         "#F39C12",
        "cleanup":            "#95A5A6",
    }
    # phases: nhóm event vào phase để dễ đọc trong UI
    PHASES = {
        "request_start":     "ibac",
        "intent_parsing":    "ibac",
        "intent_parsed":     "ibac",
        "tuples_written":    "ibac",
        "fga_check":         "ibac",
        "agent_turn":        "agent",
        "llm_tool_decision": "agent",
        "llm_final_answer":  "agent",
        "tool_call":         "agent",
        "tool_success":      "agent",
        "tool_denied":       "agent",
        "tool_error":        "agent",
        "escalation":        "agent",
        "cleanup":           "ibac",
    }

    @property
    def icon(self) -> str:
        return self.ICONS.get(self.type, "ℹ️")

    @property
    def color(self) -> str:
        return self.COLORS.get(self.type, "#95A5A6")

    @property
    def phase(self) -> str:
        return self.PHASES.get(self.type, "ibac")

    @property
    def label(self) -> str:
        d = self.data
        if self.type == "request_start":
            msg = str(d.get("message", ""))[:80]
            rid = d.get("request_id", "")[:8]
            return f'**Request** `{rid}…` | scope=`{d.get("scope_mode","strict")}` | _{msg}_'

        if self.type == "intent_parsing":
            return "**Intent Parser** — gửi yêu cầu lên LLM để phân tích…"

        if self.type == "intent_parsed":
            caps = d.get("capabilities", [])
            if not caps:
                return "**Intent parsed** — không tìm thấy capability nào"
            strs = [f'`{c["agent"]}:{c["tool"]}#{c["resource"]}`' for c in caps[:4]]
            extra = f" +{len(caps)-4}" if len(caps) > 4 else ""
            return f"**{len(caps)} capabilities**: {', '.join(strs)}{extra}"

        if self.type == "tuples_written":
            return f"**{d.get('count', 0)} tuples** ghi vào FGA store"

        if self.type == "agent_turn":
            return (
                f"**Agent turn {d.get('turn',0)}** "
                f"(iteration {d.get('iteration',0)}/{MAX_ITERATIONS}) "
                f"— gọi LLM để quyết định bước tiếp…"
            )

        if self.type == "llm_tool_decision":
            tools = d.get("tools", [])
            return f"**LLM quyết định** gọi {len(tools)} tool: {', '.join(f'`{t}`' for t in tools)}"

        if self.type == "llm_final_answer":
            preview = str(d.get("preview", ""))[:80]
            return f"**LLM trả lời cuối** — _{preview}…_"

        if self.type == "fga_check":
            agent  = d.get("agent", "")
            tool   = d.get("tool", "")
            res    = d.get("resource", "")
            turn   = d.get("turn", 0)
            if d.get("blocked"):
                status = "🚫 BLOCKED (deny policy)"
            elif d.get("allowed"):
                status = "✓ allowed"
            else:
                status = "✗ no tuple"
            return f"**FGA check** `{agent}:{tool}#{res}` turn={turn} → {status}"

        if self.type == "tool_call":
            args = ", ".join(
                f"{k}=`{v}`" for k, v in (d.get("args") or {}).items()
            )
            return f"**Gọi tool** `{d.get('tool')}({args})`"

        if self.type == "tool_success":
            return f"**`{d.get('tool')}`** — thực thi thành công ✓"

        if self.type == "tool_denied":
            reason = d.get("reason", "not_in_intent")
            suffix = " _(có thể escalate)_" if d.get("can_escalate") else " _(chặn vĩnh viễn)_"
            return f"**`{d.get('tool')}`** bị từ chối — `{reason}`{suffix}"

        if self.type == "tool_error":
            err = str(d.get("error", ""))[:120]
            return f"**`{d.get('tool')}`** lỗi: `{err}`"

        if self.type == "escalation":
            status = "✅ approved" if d.get("approved") else "❌ denied"
            auto   = " _(auto)_" if d.get("auto") else ""
            prompt = str(d.get("prompt", ""))[:80]
            return f"**Escalation {status}**{auto}: _{prompt}_"

        if self.type == "cleanup":
            return f"**Cleanup** — xoá {d.get('count', 0)} tuples sau request"

        return self.type


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------

class _LoggingParser:
    def __init__(self, parser, events: list[IBACEvent]) -> None:
        self._inner = parser
        self._events = events

    def parse(self, message: str, context):
        self._events.append(IBACEvent(type="intent_parsing"))
        result = self._inner.parse(message, context)
        self._events.append(IBACEvent(
            type="intent_parsed",
            data={
                "count": len(result.capabilities),
                "capabilities": [
                    {"agent": c.agent, "tool": c.tool, "resource": c.resource}
                    for c in result.capabilities
                ],
            },
        ))
        return result


class _LoggingTupleManager:
    def __init__(self, tm, events: list[IBACEvent]) -> None:
        self._inner = tm
        self._events = events

    def write_tuples(self, request_id, capabilities, current_turn, ttl=None):
        result = self._inner.write_tuples(request_id, capabilities, current_turn, ttl)
        self._events.append(IBACEvent(
            type="tuples_written", data={"count": len(result)}
        ))
        return result

    def delete_tuples(self, request_id):
        n = self._inner.delete_tuples(request_id)
        self._events.append(IBACEvent(type="cleanup", data={"count": n}))
        return n

    def expire_old_tuples(self, current_turn):
        return self._inner.expire_old_tuples(current_turn)


class _LoggingFGAClient:
    """Wrap InMemoryFGAClient để log từng FGA check."""
    def __init__(self, fga, events: list[IBACEvent]) -> None:
        self._inner = fga
        self._events = events

    def check(self, request_id, agent, tool, resource, current_turn):
        result = self._inner.check(request_id, agent, tool, resource, current_turn)
        self._events.append(IBACEvent(
            type="fga_check",
            data={
                "agent": agent, "tool": tool, "resource": resource,
                "turn": current_turn,
                "allowed": result.allowed,
                "blocked": result.blocked,
            },
        ))
        return result

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class DemoApprovalCallback:
    def __init__(self, auto_approve: bool, events: list[IBACEvent]) -> None:
        self.auto_approve = auto_approve
        self._events = events

    async def ask(self, prompt: str) -> bool:
        approved = self.auto_approve
        self._events.append(IBACEvent(
            type="escalation",
            data={"prompt": prompt, "approved": approved, "auto": True},
        ))
        return approved


# ---------------------------------------------------------------------------
# LoggingOrchestrator — override run() & _execute_tool_call
# ---------------------------------------------------------------------------

class LoggingOrchestrator(IbacOrchestrator):
    def __init__(self, events: list[IBACEvent], **kwargs) -> None:
        super().__init__(**kwargs)
        self._events = events
        # Wrap all three components for full visibility
        self._parser = _LoggingParser(self._parser, events)
        self._tm     = _LoggingTupleManager(self._tm, events)
        self._fga    = _LoggingFGAClient(self._fga, events)

    # ── Full override of run() to log every step ─────────────────────────

    async def run(
        self,
        user_message: str,
        contact_store: ContactStore | None = None,
        scope_mode: str = "strict",
    ) -> str:
        cs  = contact_store or ContactStore()
        ctx = assemble_request_context(user_message, cs, scope_mode=scope_mode)

        # Step 1: request received
        self._events.append(IBACEvent(
            type="request_start",
            data={
                "message":    user_message[:100],
                "request_id": ctx.request_id,
                "scope_mode": scope_mode,
            },
        ))

        # Step 2-3: parse intent → write tuples  (_LoggingParser emits events)
        parser_output = self._parser.parse(user_message, ctx)
        if parser_output.capabilities:
            self._tm.write_tuples(
                ctx.request_id, parser_output.capabilities, ctx.current_turn
            )

        # Build initial message history
        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ]

        final_answer = ""
        try:
            for iteration in range(MAX_ITERATIONS):
                ctx = ctx.advance_turn()

                # Step 4: agent turn
                self._events.append(IBACEvent(
                    type="agent_turn",
                    data={"turn": ctx.current_turn, "iteration": iteration + 1},
                ))

                response = self._llm.complete_with_tools(
                    messages=messages, tools=TOOL_DEFINITIONS
                )

                # Step 5a: LLM produced final answer
                if not response.tool_calls:
                    preview = (response.content or "")[:120]
                    self._events.append(IBACEvent(
                        type="llm_final_answer", data={"preview": preview}
                    ))
                    final_answer = response.content or ""
                    break

                # Step 5b: LLM decided to call tools
                self._events.append(IBACEvent(
                    type="llm_tool_decision",
                    data={"tools": [tc.name for tc in response.tool_calls]},
                ))

                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                })

                # Step 6: execute each tool call (FGA check + run)
                for tc in response.tool_calls:
                    result_content = await self._execute_tool_call(
                        tc.name, tc.arguments, ctx
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            result_content, ensure_ascii=False, default=str
                        ),
                    })

            else:
                final_answer = "Đã vượt quá số vòng lặp tối đa."

        finally:
            # Step 7: cleanup
            self._tm.delete_tuples(ctx.request_id)

        return final_answer

    # ── _execute_tool_call: log tool call + result ────────────────────────

    async def _execute_tool_call(
        self, tool_name: str, arguments: dict[str, Any], ctx: RequestContext
    ) -> Any:
        from ibac.agents.orchestrator import _TOOL_AUTH_MAP

        auth_info = _TOOL_AUTH_MAP.get(tool_name)
        if auth_info is None:
            self._events.append(IBACEvent(
                type="tool_error",
                data={"tool": tool_name, "error": "tool không tồn tại"},
            ))
            return {"error": f"Tool '{tool_name}' không tồn tại"}

        ibac_agent, ibac_tool, resource_param = auth_info
        resource = arguments.get(resource_param, "")

        # Log tool call intent
        self._events.append(IBACEvent(
            type="tool_call",
            data={"tool": tool_name, "args": arguments},
        ))

        tool_fn  = getattr(self._agent, tool_name, None)
        if tool_fn is None:
            return {"error": f"Tool '{tool_name}' không được implement"}

        inner_fn = tool_fn.__wrapped__ if hasattr(tool_fn, "__wrapped__") else tool_fn

        async def execute():
            return inner_fn(self._agent, **arguments)

        # FGA check is logged by _LoggingFGAClient
        try:
            result: ToolResult = await invoke_tool_with_auth(
                fga_client=self._fga,
                request_id=ctx.request_id,
                agent=ibac_agent,
                tool=ibac_tool,
                resource=resource,
                execute=execute,
                current_turn=ctx.current_turn,
            )
        except Exception as exc:
            self._events.append(IBACEvent(
                type="tool_error", data={"tool": tool_name, "error": str(exc)}
            ))
            return {"error": str(exc)}

        if result.success:
            self._events.append(IBACEvent(
                type="tool_success", data={"tool": tool_name}
            ))
            return result.data

        if result.denied:
            can_escalate = result.can_escalate
            self._events.append(IBACEvent(
                type="tool_denied",
                data={
                    "tool": tool_name,
                    "reason": result.reason or "unknown",
                    "can_escalate": can_escalate,
                },
            ))

            if can_escalate:
                denied_result = ToolResult.deny_not_in_intent(ibac_agent, ibac_tool, resource)
                new_tuple = await self._escalation.handle(
                    denied_result, ibac_agent, ibac_tool, resource, ctx
                )
                if new_tuple is not None:
                    # Retry after approval
                    try:
                        retry = await invoke_tool_with_auth(
                            fga_client=self._fga,
                            request_id=ctx.request_id,
                            agent=ibac_agent,
                            tool=ibac_tool,
                            resource=resource,
                            execute=execute,
                            current_turn=ctx.current_turn,
                        )
                        if retry.success:
                            self._events.append(IBACEvent(
                                type="tool_success",
                                data={"tool": tool_name},
                            ))
                            return retry.data
                    except Exception as exc:
                        return {"error": str(exc)}

                return {
                    "denied": True,
                    "reason": "not_in_intent",
                    "message": f"Không có quyền: {ibac_agent}:{ibac_tool} trên '{resource}'",
                }
            else:
                return {
                    "denied": True,
                    "reason": "policy",
                    "message": f"Bị cấm vĩnh viễn: {ibac_agent}:{ibac_tool} trên '{resource}'",
                }

        return {"error": "Lỗi không xác định"}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_orchestrator(
    events: list[IBACEvent],
    auto_approve: bool = True,
    scope_mode: str = "strict",
) -> LoggingOrchestrator:
    llm = QwenClient()
    fga = InMemoryFGAClient()
    load_default_deny_policies(fga)

    tm       = TupleManager(fga, default_ttl=5)
    parser   = IntentParser(llm, scope_mode=scope_mode)
    approval = DemoApprovalCallback(auto_approve, events)
    escalation = EscalationHandler(parser, tm, approval, max_escalations=3)
    agent    = DataAnalyticsAgent(data_dir=DATA_DIR)

    return LoggingOrchestrator(
        events=events,
        llm_client=llm,
        fga_client=fga,
        intent_parser=parser,
        tuple_manager=tm,
        escalation_handler=escalation,
        data_agent=agent,
    )
