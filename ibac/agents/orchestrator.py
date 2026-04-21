"""
Component 8b: IbacOrchestrator

Kết nối toàn bộ pipeline IBAC với Data Analytics Agent.
Dùng LLM tool-calling loop (OpenAI function calling format).

Flow:
  1. assemble_request_context
  2. intent_parser.parse → capabilities
  3. tuple_manager.write_tuples
  4. Tool-calling loop (max 10 vòng):
     a. LLM quyết định tool + args
     b. invoke_tool_with_auth (kiểm tra FGA)
     c. Nếu denied+can_escalate → escalation_handler.handle
     d. LLM nhận kết quả → bước tiếp hoặc final answer
  5. tuple_manager.delete_tuples
  6. Trả về final answer
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ibac.context.request_context import ContactStore, assemble_request_context
from ibac.executor.tool_wrapper import invoke_tool_with_auth
from ibac.models.schemas import RequestContext, ToolResult

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "load_dataset",
            "description": "Đọc dataset và trả về preview 10 rows đầu cùng thông tin cột.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Tên file CSV (ví dụ: sales_data.csv)",
                        "enum": [
                            "sales_data.csv", "customer_demographics.csv",
                            "product_catalog.csv", "regional_sales.csv",
                            "sales_channels.csv", "campaign_performance.csv",
                        ],
                    }
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_sales",
            "description": "Lọc sales_data.csv theo Region, Sales_Channel, Campaign_Name, hoặc date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Tên file CSV"},
                    "filters": {
                        "type": "object",
                        "description": "Dict các điều kiện lọc: {Region, Sales_Channel, Campaign_Name, date_from, date_to}",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_revenue",
            "description": "Tổng hợp doanh thu theo cột chỉ định (Region, Product_Name, Sales_Channel, Month).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "group_by": {
                        "type": "string",
                        "description": "Tên cột group by: Region | Product_Name | Sales_Channel | Month",
                    },
                },
                "required": ["filename", "group_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_products",
            "description": "Top N sản phẩm bán chạy nhất theo tổng doanh thu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "n": {"type": "integer", "description": "Số lượng sản phẩm top (mặc định 5)"},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "customer_segment_analysis",
            "description": "Phân tích khách hàng theo Age_Group, Gender, Region, hoặc Income_Range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "segment_by": {
                        "type": "string",
                        "description": "Cột phân nhóm: Age_Group | Gender | Region | Income_Range",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "campaign_roi_analysis",
            "description": "So sánh ROI các chiến dịch marketing, sắp xếp giảm dần theo ROI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regional_performance",
            "description": "Phân tích hiệu suất theo vùng địa lý (Total_Revenue, Customer_Retention_Rate, Marketing_Spend).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "metric": {
                        "type": "string",
                        "description": "Metric cần phân tích: Total_Revenue | Customer_Retention_Rate | Marketing_Spend",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channel_comparison",
            "description": "So sánh hiệu quả các kênh bán hàng (Online, Cửa hàng, v.v.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inventory_alert",
            "description": "Tìm sản phẩm sắp hết hàng (Stock_Quantity <= threshold).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "threshold": {
                        "type": "integer",
                        "description": "Ngưỡng số lượng tồn kho (mặc định 30)",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_dataset",
            "description": "Thống kê mô tả (min, max, mean, std) các cột số của dataset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                },
                "required": ["filename"],
            },
        },
    },
]

# tool_name → (agent, tool, resource_param_name)
_TOOL_AUTH_MAP: dict[str, tuple[str, str, str]] = {
    "load_dataset":              ("data", "read",      "filename"),
    "query_sales":               ("data", "query",     "filename"),
    "aggregate_revenue":         ("data", "aggregate", "filename"),
    "top_products":              ("data", "query",     "filename"),
    "customer_segment_analysis": ("data", "aggregate", "filename"),
    "campaign_roi_analysis":     ("data", "query",     "filename"),
    "regional_performance":      ("data", "aggregate", "filename"),
    "channel_comparison":        ("data", "query",     "filename"),
    "inventory_alert":           ("data", "query",     "filename"),
    "describe_dataset":          ("data", "read",      "filename"),
}

_SYSTEM_PROMPT = """\
Bạn là Data Analytics Agent. Bạn có quyền truy cập các tool phân tích dữ liệu bán hàng.
Hãy trả lời câu hỏi của người dùng bằng cách gọi tool phù hợp, sau đó tổng hợp kết quả thành câu trả lời rõ ràng.
Chỉ gọi tool khi cần thiết. Sau khi có đủ dữ liệu, trả lời trực tiếp.
"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class IbacOrchestrator:
    """
    Orchestrator kết nối pipeline IBAC với Data Analytics Agent.

    Inject:
      - llm_client:        QwenClient (hoặc bất kỳ LLMClient nào có method complete_with_tools)
      - fga_client:        InMemoryFGAClient
      - intent_parser:     IntentParser
      - tuple_manager:     TupleManager
      - escalation_handler: EscalationHandler
      - data_agent:        DataAnalyticsAgent instance
    """

    def __init__(
        self,
        llm_client,
        fga_client,
        intent_parser,
        tuple_manager,
        escalation_handler,
        data_agent,
    ) -> None:
        self._llm = llm_client
        self._fga = fga_client
        self._parser = intent_parser
        self._tm = tuple_manager
        self._escalation = escalation_handler
        self._agent = data_agent

    async def run(
        self,
        user_message: str,
        contact_store: ContactStore | None = None,
        scope_mode: str = "strict",
    ) -> str:
        """
        Chạy full IBAC pipeline cho một user message.
        Trả về final answer string.
        """
        cs = contact_store or ContactStore()
        ctx = assemble_request_context(user_message, cs, scope_mode=scope_mode)

        # Phase 2-3: Parse intent → write tuples
        parser_output = self._parser.parse(user_message, ctx)
        if parser_output.capabilities:
            self._tm.write_tuples(ctx.request_id, parser_output.capabilities, ctx.current_turn)
            logger.info("[IBAC] Wrote %d tuples for %s", len(parser_output.capabilities), ctx.request_id)

        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        final_answer = ""
        try:
            for iteration in range(MAX_ITERATIONS):
                ctx = ctx.advance_turn()

                response = self._llm.complete_with_tools(
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                )

                # No tool calls → final answer
                if not response.tool_calls:
                    final_answer = response.content or ""
                    break

                # Add assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in response.tool_calls
                    ],
                })

                # Execute each tool call
                for tc in response.tool_calls:
                    tool_result_content = await self._execute_tool_call(
                        tc.name, tc.arguments, ctx
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_result_content, ensure_ascii=False, default=str),
                    })

            else:
                final_answer = "Đã vượt quá số vòng lặp tối đa. Kết quả có thể chưa hoàn chỉnh."

        finally:
            self._tm.delete_tuples(ctx.request_id)
            logger.info("[IBAC] Cleaned up tuples for %s", ctx.request_id)

        return final_answer

    async def _execute_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: RequestContext,
    ) -> Any:
        """Thực thi một tool call qua IBAC authorization."""
        auth_info = _TOOL_AUTH_MAP.get(tool_name)
        if auth_info is None:
            return {"error": f"Tool '{tool_name}' không tồn tại"}

        ibac_agent, ibac_tool, resource_param = auth_info
        resource = arguments.get(resource_param, "")

        tool_fn = getattr(self._agent, tool_name, None)
        if tool_fn is None:
            return {"error": f"Tool '{tool_name}' không được implement"}

        # Lấy method gốc (unwrapped) để truyền vào invoke_tool_with_auth
        inner_fn = tool_fn.__wrapped__ if hasattr(tool_fn, "__wrapped__") else tool_fn

        async def execute():
            return inner_fn(self._agent, **arguments)

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
            return {"error": str(exc)}

        if result.success:
            return result.data

        if result.denied:
            if result.can_escalate:
                # Thử escalation
                from ibac.models.schemas import ToolResult as TR
                denied_result = TR.deny_not_in_intent(ibac_agent, ibac_tool, resource)
                new_tuple = await self._escalation.handle(
                    denied_result, ibac_agent, ibac_tool, resource, ctx
                )
                if new_tuple is not None:
                    # Đã được approve → thử lại
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
                        return retry.data
                return {
                    "denied": True,
                    "reason": "not_in_intent",
                    "message": f"Không có quyền thực hiện {ibac_agent}:{ibac_tool} trên '{resource}'",
                }
            else:
                return {
                    "denied": True,
                    "reason": "policy",
                    "message": f"Hành động {ibac_agent}:{ibac_tool} trên '{resource}' bị cấm vĩnh viễn",
                }

        return {"error": "Lỗi không xác định"}
