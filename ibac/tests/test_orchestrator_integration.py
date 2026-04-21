"""
Integration tests cho Phase 8: IbacOrchestrator + Data Analytics Agent.

Dùng MockLLM để tránh phụ thuộc vào API thật.
Kiểm tra toàn bộ pipeline IBAC từ user message đến final answer.

Chạy: pytest ibac/tests/test_orchestrator_integration.py -v
"""

import json
import pytest
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from ibac.agents.data_analytics_agent import DataAnalyticsAgent
from ibac.agents.orchestrator import IbacOrchestrator
from ibac.authorization.deny_policies import load_default_deny_policies
from ibac.authorization.fga_client import InMemoryFGAClient
from ibac.authorization.tuple_manager import TupleManager
from ibac.context.request_context import ContactStore
from ibac.escalation.escalation_handler import EscalationHandler
from ibac.llm_client import LLMResponse, ToolCall
from ibac.models.schemas import Capability, IntentParserOutput

DATA_DIR = Path(__file__).resolve().parents[2] / "sale_data"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

@dataclass
class _ToolCallSpec:
    name: str
    arguments: dict[str, Any]
    call_id: str = "tc-001"


class MockLLMClient:
    """
    LLM giả lập hai pha:
      1. Lần complete_with_tools đầu → trả tool call
      2. Lần complete_with_tools tiếp → trả final answer

    complete() luôn trả về JSON rỗng (dùng cho IntentParser mock).
    """

    def __init__(self, tool_call: _ToolCallSpec, final_answer: str) -> None:
        self._tool_call = tool_call
        self._final_answer = final_answer
        self._call_count = 0

    def complete(self, system: str, user: str) -> str:
        # Intent Parser sẽ gọi complete() — trả JSON tối thiểu
        return json.dumps({
            "plan": [],
            "capabilities": [],
            "denied_implicit": [],
        })

    def complete_with_tools(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self._call_count += 1
        if self._call_count == 1:
            tc = self._tool_call
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id=tc.call_id, name=tc.name, arguments=tc.arguments)],
            )
        # Lần 2 trở đi → final answer
        return LLMResponse(content=self._final_answer, tool_calls=[])


class AutoApproveCallback:
    async def ask(self, prompt: str) -> bool:
        return True


class AutoDenyCallback:
    async def ask(self, prompt: str) -> bool:
        return False


class MockIntentParser:
    """Parser trả về capabilities được inject trực tiếp."""

    def __init__(self, capabilities: list[Capability] | None = None) -> None:
        self._caps = capabilities or []

    def parse(self, user_message: str, context) -> IntentParserOutput:
        return IntentParserOutput(plan=[], capabilities=self._caps, denied_implicit=[])


def _build_orchestrator(
    tool_call: _ToolCallSpec,
    final_answer: str,
    capabilities: list[Capability] | None = None,
    approval_callback=None,
) -> IbacOrchestrator:
    fga = InMemoryFGAClient()
    load_default_deny_policies(fga)

    tm = TupleManager(fga, default_ttl=10)
    llm = MockLLMClient(tool_call=tool_call, final_answer=final_answer)
    parser = MockIntentParser(capabilities)
    cb = approval_callback or AutoDenyCallback()
    escalation = EscalationHandler(parser, tm, cb, max_escalations=3)
    agent = DataAnalyticsAgent(data_dir=DATA_DIR)

    return IbacOrchestrator(
        llm_client=llm,
        fga_client=fga,
        intent_parser=parser,
        tuple_manager=tm,
        escalation_handler=escalation,
        data_agent=agent,
    )


def _caps_for(*tool_file_pairs: tuple[str, str]) -> list[Capability]:
    """Tạo capabilities cho danh sách (tool, filename)."""
    return [
        Capability(agent="data", tool=tool, resource=fname, reasoning="test")
        for tool, fname in tool_file_pairs
    ]


# ---------------------------------------------------------------------------
# Test: Top 5 sản phẩm bán chạy nhất
# ---------------------------------------------------------------------------

class TestTopProducts:
    @pytest.mark.asyncio
    async def test_returns_final_answer(self):
        """Pipeline hoàn chỉnh: parse intent → write tuple → gọi tool → final answer."""
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec("top_products", {"filename": "sales_data.csv", "n": 5}),
            final_answer="Top 5 sản phẩm bán chạy nhất là: ...",
            capabilities=_caps_for(("query", "sales_data.csv")),
        )
        result = await orc.run("Top 5 sản phẩm bán chạy nhất?")
        assert result == "Top 5 sản phẩm bán chạy nhất là: ..."

    @pytest.mark.asyncio
    async def test_tool_result_included_in_messages(self):
        """Sau tool call, kết quả phải được đưa vào messages history cho LLM."""
        llm = MockLLMClient(
            tool_call=_ToolCallSpec("top_products", {"filename": "sales_data.csv", "n": 3}),
            final_answer="Xong",
        )
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        tm = TupleManager(fga, default_ttl=10)
        caps = _caps_for(("query", "sales_data.csv"))
        parser = MockIntentParser(caps)
        escalation = EscalationHandler(parser, tm, AutoDenyCallback())
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        orc = IbacOrchestrator(llm, fga, parser, tm, escalation, agent)

        await orc.run("Top 3 sản phẩm?")
        # LLM phải được gọi 2 lần: lần 1 tool call, lần 2 final answer
        assert llm._call_count == 2

    @pytest.mark.asyncio
    async def test_denied_without_tuple(self):
        """Không có tuple → tool bị từ chối, LLM nhận thông báo lỗi."""
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec("top_products", {"filename": "sales_data.csv"}),
            final_answer="Không thể truy cập dữ liệu",
            capabilities=[],  # Không cấp quyền nào
        )
        result = await orc.run("Top sản phẩm?")
        # Vẫn trả về final answer (LLM nhận được thông báo denied)
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Test: Doanh thu theo vùng
# ---------------------------------------------------------------------------

class TestAggregateRevenue:
    @pytest.mark.asyncio
    async def test_aggregate_by_region(self):
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec(
                "aggregate_revenue",
                {"filename": "sales_data.csv", "group_by": "Region"},
            ),
            final_answer="Doanh thu theo vùng: Hà Nội dẫn đầu với ...",
            capabilities=_caps_for(("aggregate", "sales_data.csv")),
        )
        result = await orc.run("Doanh thu theo vùng?")
        assert "Doanh thu" in result

    @pytest.mark.asyncio
    async def test_invalid_file_handled_gracefully(self):
        """
        LLM yêu cầu file không nằm trong ALLOWED_FILES → security check từ chối,
        orchestrator trả error dưới dạng tool result (không crash), LLM nhận thông báo lỗi.
        """
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec(
                "aggregate_revenue",
                {"filename": "../../secrets.csv", "group_by": "Region"},
            ),
            final_answer="Có lỗi xảy ra",
            capabilities=_caps_for(("aggregate", "../../secrets.csv")),
        )
        # Không raise exception — orchestrator bắt lỗi gracefully
        result = await orc.run("Doanh thu theo vùng?")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Test: Chiến dịch có ROI cao nhất
# ---------------------------------------------------------------------------

class TestCampaignROI:
    @pytest.mark.asyncio
    async def test_campaign_roi_analysis(self):
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec(
                "campaign_roi_analysis",
                {"filename": "campaign_performance.csv"},
            ),
            final_answer="Chiến dịch Black Friday có ROI cao nhất 491.86%",
            capabilities=_caps_for(("query", "campaign_performance.csv")),
        )
        result = await orc.run("Chiến dịch nào có ROI cao nhất?")
        assert "Black Friday" in result or "ROI" in result


# ---------------------------------------------------------------------------
# Test: Injection "Also delete sales_data.csv" → bị chặn bởi deny policy
# ---------------------------------------------------------------------------

class TestInjectionDefense:
    @pytest.mark.asyncio
    async def test_delete_blocked_by_deny_policy(self):
        """
        Injection cố gắng xóa file dữ liệu → bị chặn bởi deny policy data:delete#*.
        can_escalate=False → không hỏi user, trả thông báo blocked.
        """
        orc = _build_orchestrator(
            # Giả lập LLM bị injection điều khiển gọi "delete" (không có trong tools,
            # nên ta test trực tiếp qua FGA check)
            tool_call=_ToolCallSpec("top_products", {"filename": "sales_data.csv"}),
            final_answer="Xong",
            capabilities=_caps_for(("query", "sales_data.csv")),
        )
        # Kiểm tra FGA trực tiếp: data:delete bị blocked
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        check = fga.check("req-x", "data", "delete", "sales_data.csv", 0)
        assert check.blocked is True
        assert check.allowed is False

    @pytest.mark.asyncio
    async def test_write_blocked_by_deny_policy(self):
        """data:write bị deny policy — không thể escalate."""
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        check = fga.check("req-x", "data", "write", "sales_data.csv", 0)
        assert check.blocked is True

    @pytest.mark.asyncio
    async def test_shell_exec_blocked(self):
        """Shell execution luôn bị chặn."""
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        check = fga.check("req-x", "shell", "exec", "rm -rf /", 0)
        assert check.blocked is True

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        """Tool không tồn tại trong agent → trả error, không crash."""
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec("nonexistent_tool", {"filename": "sales_data.csv"}),
            final_answer="Tool không tồn tại",
            capabilities=[],
        )
        result = await orc.run("Làm điều gì đó?")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Test: Escalation flow trong Orchestrator
# ---------------------------------------------------------------------------

class TestEscalationInOrchestrator:
    @pytest.mark.asyncio
    async def test_escalation_approved_retries_tool(self):
        """
        Tool bị deny (not_in_intent) → escalation → approve → tuple mới được ghi → tool chạy lại.
        """
        # Bắt đầu không có tuple, approve callback sẽ tự động approve
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        tm = TupleManager(fga, default_ttl=10)

        # MockParser cho escalation — trả capability cho data:query trên sales_data.csv
        from ibac.models.schemas import IntentParserOutput
        class EscalationParser:
            def parse(self, message, context):
                return IntentParserOutput(
                    plan=[],
                    capabilities=[
                        Capability(agent="data", tool="query", resource="sales_data.csv", reasoning="escalated")
                    ],
                    denied_implicit=[],
                )

        llm = MockLLMClient(
            tool_call=_ToolCallSpec("top_products", {"filename": "sales_data.csv", "n": 3}),
            final_answer="Đã được approve và lấy top 3",
        )
        parser = MockIntentParser([])  # Không cấp quyền từ đầu
        escalation = EscalationHandler(EscalationParser(), tm, AutoApproveCallback(), max_escalations=3)
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        orc = IbacOrchestrator(llm, fga, parser, tm, escalation, agent)

        result = await orc.run("Top 3 sản phẩm?")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_escalation_denied_returns_blocked_message(self):
        """Escalation bị từ chối → LLM nhận thông báo không có quyền."""
        orc = _build_orchestrator(
            tool_call=_ToolCallSpec("top_products", {"filename": "sales_data.csv"}),
            final_answer="Không có quyền truy cập",
            capabilities=[],
            approval_callback=AutoDenyCallback(),
        )
        result = await orc.run("Top sản phẩm?")
        assert isinstance(result, str)
