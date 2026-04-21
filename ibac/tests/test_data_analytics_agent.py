"""
Unit tests cho Phase 8: Data Analytics Agent.

Chạy: pytest ibac/tests/test_data_analytics_agent.py -v
"""

import pytest
import pandas as pd
from pathlib import Path

from ibac.agents.data_analytics_agent import DataAnalyticsAgent, _resolve_path, ALLOWED_FILES
from ibac.authorization.fga_client import InMemoryFGAClient
from ibac.authorization.deny_policies import load_default_deny_policies
from ibac.authorization.tuple_manager import TupleManager
from ibac.models.schemas import Capability, RequestContext, ToolResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parents[2] / "sale_data"


def _ctx() -> RequestContext:
    return RequestContext(request_id="req-test", contacts={}, current_turn=0)


def _agent_with_auth() -> tuple[DataAnalyticsAgent, InMemoryFGAClient]:
    """Agent + FGA đã được cấp quyền đọc/query/aggregate tất cả files."""
    fga = InMemoryFGAClient()
    load_default_deny_policies(fga)
    tm = TupleManager(fga, default_ttl=10)

    caps = []
    for fname in ALLOWED_FILES:
        for tool in ("read", "query", "aggregate"):
            caps.append(Capability(agent="data", tool=tool, resource=fname, reasoning="test"))

    tm.write_tuples("req-test", caps, current_turn=0)
    agent = DataAnalyticsAgent(data_dir=DATA_DIR)
    return agent, fga


# ---------------------------------------------------------------------------
# Security: path traversal
# ---------------------------------------------------------------------------

class TestPathSecurity:
    def test_allowed_files_accepted(self):
        for fname in ALLOWED_FILES:
            path = _resolve_path(fname)
            assert path.name == fname

    def test_traversal_blocked(self):
        with pytest.raises(ValueError, match="không được phép"):
            _resolve_path("../../.env")

    def test_arbitrary_file_blocked(self):
        with pytest.raises(ValueError):
            _resolve_path("secret.csv")

    def test_absolute_path_blocked(self):
        with pytest.raises(ValueError):
            _resolve_path("/etc/passwd")


# ---------------------------------------------------------------------------
# Tools — chạy trực tiếp (bypassed auth wrapper để test logic thuần)
# ---------------------------------------------------------------------------

class TestDescribeDataset:
    def test_describe_sales(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        # Gọi inner function trực tiếp để bypass auth trong unit test
        result = agent.describe_dataset.__wrapped__(agent, filename="sales_data.csv")
        assert result["shape"]["rows"] == 150
        assert "Order_ID" in result["columns"]
        assert "numeric_stats" in result

    def test_describe_product_catalog(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.describe_dataset.__wrapped__(agent, filename="product_catalog.csv")
        assert result["shape"]["rows"] == 20
        assert "Profit_Margin_Percent" in result["columns"]


class TestTopProducts:
    def test_top5_default(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.top_products.__wrapped__(agent, filename="sales_data.csv", n=5)
        assert len(result) == 5
        # Sắp xếp giảm dần
        revenues = [r["total_revenue"] for r in result]
        assert revenues == sorted(revenues, reverse=True)

    def test_top3(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.top_products.__wrapped__(agent, filename="sales_data.csv", n=3)
        assert len(result) == 3

    def test_result_has_required_keys(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.top_products.__wrapped__(agent, filename="sales_data.csv")
        assert all("product_name" in r and "total_revenue" in r for r in result)


class TestAggregateRevenue:
    def test_by_region(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.aggregate_revenue.__wrapped__(agent, filename="sales_data.csv", group_by="Region")
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_by_channel(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.aggregate_revenue.__wrapped__(agent, filename="sales_data.csv", group_by="Sales_Channel")
        assert isinstance(result, dict)

    def test_invalid_column(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        with pytest.raises(ValueError, match="không tồn tại"):
            agent.aggregate_revenue.__wrapped__(agent, filename="sales_data.csv", group_by="NonExistent")


class TestQuerySales:
    def test_filter_by_region(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.query_sales.__wrapped__(
            agent, filename="sales_data.csv", filters={"Region": "Hà Nội"}
        )
        assert all(r["Region"] == "Hà Nội" for r in result)

    def test_empty_filters_returns_all(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.query_sales.__wrapped__(agent, filename="sales_data.csv", filters={})
        assert len(result) == 150

    def test_date_filter(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.query_sales.__wrapped__(
            agent, filename="sales_data.csv",
            filters={"date_from": "2023-06-01", "date_to": "2023-06-30"}
        )
        assert isinstance(result, list)


class TestCustomerSegment:
    def test_by_age_group(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.customer_segment_analysis.__wrapped__(
            agent, filename="customer_demographics.csv", segment_by="Age_Group"
        )
        assert isinstance(result, dict)
        # Phải có ít nhất 1 nhóm tuổi
        assert len(result) > 0
        first = next(iter(result.values()))
        assert "count" in first

    def test_by_gender(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.customer_segment_analysis.__wrapped__(
            agent, filename="customer_demographics.csv", segment_by="Gender"
        )
        assert "Nam" in result or "Nữ" in result


class TestCampaignROI:
    def test_sorted_by_roi(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.campaign_roi_analysis.__wrapped__(agent, filename="campaign_performance.csv")
        assert len(result) == 10
        rois = [r["ROI_Percent"] for r in result]
        assert rois == sorted(rois, reverse=True)


class TestRegionalPerformance:
    def test_total_revenue(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.regional_performance.__wrapped__(
            agent, filename="regional_sales.csv", metric="Total_Revenue"
        )
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_invalid_metric(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        with pytest.raises(ValueError, match="không tồn tại"):
            agent.regional_performance.__wrapped__(
                agent, filename="regional_sales.csv", metric="FakeMetric"
            )


class TestInventoryAlert:
    def test_default_threshold(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.inventory_alert.__wrapped__(agent, filename="product_catalog.csv", threshold=30)
        # Tất cả kết quả phải có Stock_Quantity <= 30
        assert all(r["Stock_Quantity"] <= 30 for r in result)

    def test_high_threshold_returns_all(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.inventory_alert.__wrapped__(agent, filename="product_catalog.csv", threshold=999999)
        assert len(result) == 20

    def test_zero_threshold_returns_empty(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.inventory_alert.__wrapped__(agent, filename="product_catalog.csv", threshold=0)
        assert result == []


class TestChannelComparison:
    def test_returns_records(self):
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = agent.channel_comparison.__wrapped__(agent, filename="sales_channels.csv")
        assert isinstance(result, list)
        assert len(result) > 0
        assert "Sales_Channel" in result[0]


# ---------------------------------------------------------------------------
# Authorization: IBAC-protected method calls
# ---------------------------------------------------------------------------

class TestAuthProtection:
    @pytest.mark.asyncio
    async def test_denied_without_tuple(self):
        """Không có tuple → bị từ chối."""
        fga = InMemoryFGAClient()
        agent = DataAnalyticsAgent(data_dir=DATA_DIR)
        result = await agent.describe_dataset(
            filename="sales_data.csv",
            _fga_client=fga,
            _request_id="req-no-perm",
            _current_turn=0,
        )
        assert isinstance(result, ToolResult)
        assert result.denied is True
        assert result.can_escalate is True

    @pytest.mark.asyncio
    async def test_allowed_with_tuple(self):
        """Có tuple → được phép."""
        agent, fga = _agent_with_auth()
        result = await agent.describe_dataset(
            filename="sales_data.csv",
            _fga_client=fga,
            _request_id="req-test",
            _current_turn=0,
        )
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_delete_blocked_by_policy(self):
        """data:delete bị deny policy — can_escalate=False."""
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        result = fga.check("req-x", "data", "delete", "sales_data.csv", 0)
        assert result.blocked is True
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_write_blocked_by_policy(self):
        """data:write bị deny policy — cannot escalate."""
        fga = InMemoryFGAClient()
        load_default_deny_policies(fga)
        result = fga.check("req-x", "data", "write", "sales_data.csv", 0)
        assert result.blocked is True
