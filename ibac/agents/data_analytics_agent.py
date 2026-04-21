"""
Component 8a: Data Analytics Agent Tools

Mỗi tool được wrap với @require_auth — không tool nào chạy mà không qua IBAC.
Resource là tên file CSV trong sale_data/.

Security:
  - Path traversal bị chặn: chỉ file nằm trong ALLOWED_FILES
  - data:delete và data:write bị deny policy vĩnh viễn
  - data_dir có thể override (phục vụ test), mặc định là sale_data/ kạnh repo root
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

from ibac.executor.tool_wrapper import require_auth

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALLOWED_FILES = {
    "sales_data.csv",
    "customer_demographics.csv",
    "product_catalog.csv",
    "regional_sales.csv",
    "sales_channels.csv",
    "campaign_performance.csv",
}

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "sale_data"


def _resolve_path(filename: str, data_dir: Path | None = None) -> Path:
    """Trả về absolute path, chặn path traversal."""
    if filename not in ALLOWED_FILES:
        raise ValueError(f"File '{filename}' không được phép. Chỉ cho phép: {sorted(ALLOWED_FILES)}")
    return (data_dir or _DEFAULT_DATA_DIR) / filename


def _load(filename: str, data_dir: Path | None = None) -> pd.DataFrame:
    path = _resolve_path(filename, data_dir)
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy file: {path}")
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class DataAnalyticsAgent:
    """
    Wrapper class giữ data_dir, để Orchestrator inject khi khởi tạo.
    Mỗi method là một tool IBAC-protected.
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else None

    # --- describe_dataset ---------------------------------------------------

    @require_auth(agent="data", tool="read", resource_param="filename")
    def describe_dataset(self, filename: str) -> dict[str, Any]:
        """Thống kê mô tả các cột số của dataset."""
        df = _load(filename, self._data_dir)
        desc = df.describe().to_dict()
        return {
            "filename": filename,
            "shape": {"rows": len(df), "columns": len(df.columns)},
            "columns": list(df.columns),
            "numeric_stats": desc,
        }

    # --- load_dataset -------------------------------------------------------

    @require_auth(agent="data", tool="read", resource_param="filename")
    def load_dataset(self, filename: str) -> dict[str, Any]:
        """Đọc dataset và trả về preview (10 rows đầu)."""
        df = _load(filename, self._data_dir)
        return {
            "filename": filename,
            "shape": {"rows": len(df), "columns": len(df.columns)},
            "columns": list(df.columns),
            "preview": df.head(10).to_dict(orient="records"),
        }

    # --- query_sales --------------------------------------------------------

    @require_auth(agent="data", tool="query", resource_param="filename")
    def query_sales(self, filename: str, filters: dict[str, Any] | None = None) -> list[dict]:
        """
        Lọc sales_data.csv theo filters.
        Filters hỗ trợ: Region, Sales_Channel, Campaign_Name,
                        date_from (YYYY-MM-DD), date_to (YYYY-MM-DD).
        """
        df = _load(filename, self._data_dir)
        filters = filters or {}

        for col in ("Region", "Sales_Channel", "Campaign_Name"):
            if col in filters:
                df = df[df[col] == filters[col]]

        if "date_from" in filters and "Order_Date" in df.columns:
            df = df[pd.to_datetime(df["Order_Date"]) >= pd.to_datetime(filters["date_from"])]
        if "date_to" in filters and "Order_Date" in df.columns:
            df = df[pd.to_datetime(df["Order_Date"]) <= pd.to_datetime(filters["date_to"])]

        return df.to_dict(orient="records")

    # --- aggregate_revenue --------------------------------------------------

    @require_auth(agent="data", tool="aggregate", resource_param="filename")
    def aggregate_revenue(self, filename: str, group_by: str) -> dict[str, float]:
        """
        Tổng hợp doanh thu theo cột chỉ định.
        group_by: "Region" | "Product_Name" | "Sales_Channel" | "Month"
        """
        df = _load(filename, self._data_dir)

        revenue_col = _pick_revenue_col(df)
        if revenue_col is None:
            raise ValueError(f"Không tìm thấy cột revenue trong {filename}")
        if group_by not in df.columns:
            raise ValueError(f"Cột '{group_by}' không tồn tại trong {filename}. Cột có: {list(df.columns)}")

        result = df.groupby(group_by)[revenue_col].sum().sort_values(ascending=False)
        return result.to_dict()

    # --- top_products -------------------------------------------------------

    @require_auth(agent="data", tool="query", resource_param="filename")
    def top_products(self, filename: str, n: int = 5) -> list[dict]:
        """Top N sản phẩm theo tổng doanh thu."""
        df = _load(filename, self._data_dir)
        if "Product_Name" not in df.columns or "Total_Amount" not in df.columns:
            raise ValueError(f"File {filename} thiếu cột Product_Name hoặc Total_Amount")
        result = (
            df.groupby("Product_Name")["Total_Amount"]
            .sum()
            .sort_values(ascending=False)
            .head(n)
            .reset_index()
        )
        result.columns = ["product_name", "total_revenue"]
        return result.to_dict(orient="records")

    # --- customer_segment_analysis ------------------------------------------

    @require_auth(agent="data", tool="aggregate", resource_param="filename")
    def customer_segment_analysis(self, filename: str, segment_by: str = "Age_Group") -> dict[str, Any]:
        """
        Phân tích khách hàng theo segment.
        segment_by: "Age_Group" | "Gender" | "Region" | "Income_Range"
        """
        df = _load(filename, self._data_dir)
        if segment_by not in df.columns:
            raise ValueError(f"Cột '{segment_by}' không tồn tại trong {filename}")

        agg: dict[str, Any] = {}
        for seg, group in df.groupby(segment_by):
            entry: dict[str, Any] = {"count": len(group)}
            if "Total_Amount_Spent" in df.columns:
                entry["avg_spent"] = round(float(group["Total_Amount_Spent"].mean()), 2)
                entry["total_spent"] = float(group["Total_Amount_Spent"].sum())
            if "Total_Purchase_Count" in df.columns:
                entry["avg_purchases"] = round(float(group["Total_Purchase_Count"].mean()), 2)
            if "Loyalty_Points" in df.columns:
                entry["avg_loyalty"] = round(float(group["Loyalty_Points"].mean()), 2)
            agg[str(seg)] = entry
        return agg

    # --- campaign_roi_analysis ----------------------------------------------

    @require_auth(agent="data", tool="query", resource_param="filename")
    def campaign_roi_analysis(self, filename: str) -> list[dict]:
        """So sánh ROI các chiến dịch, sắp xếp giảm dần."""
        df = _load(filename, self._data_dir)
        cols = ["Campaign_Name", "Budget", "Revenue", "ROI_Percent", "Conversions", "Clicks"]
        available = [c for c in cols if c in df.columns]
        result = df[available].sort_values("ROI_Percent", ascending=False)
        return result.to_dict(orient="records")

    # --- regional_performance -----------------------------------------------

    @require_auth(agent="data", tool="aggregate", resource_param="filename")
    def regional_performance(self, filename: str, metric: str = "Total_Revenue") -> dict[str, Any]:
        """
        Phân tích theo vùng/tháng.
        metric: "Total_Revenue" | "Customer_Retention_Rate" | "Marketing_Spend"
        """
        df = _load(filename, self._data_dir)
        if metric not in df.columns:
            raise ValueError(f"Metric '{metric}' không tồn tại trong {filename}")
        if "Region" not in df.columns:
            raise ValueError(f"Cột 'Region' không tồn tại trong {filename}")

        result = df.groupby("Region")[metric].sum().sort_values(ascending=False)
        return result.to_dict()

    # --- channel_comparison -------------------------------------------------

    @require_auth(agent="data", tool="query", resource_param="filename")
    def channel_comparison(self, filename: str) -> list[dict]:
        """So sánh hiệu quả các kênh bán hàng."""
        df = _load(filename, self._data_dir)
        key_cols = [
            "Sales_Channel", "Quarter", "Total_Revenue", "ROI_Percent",
            "Conversion_Rate_Percent", "Customer_Satisfaction",
        ]
        available = [c for c in key_cols if c in df.columns]
        return df[available].to_dict(orient="records")

    # --- inventory_alert ----------------------------------------------------

    @require_auth(agent="data", tool="query", resource_param="filename")
    def inventory_alert(self, filename: str, threshold: int = 30) -> list[dict]:
        """Sản phẩm sắp hết hàng (Stock_Quantity <= threshold)."""
        df = _load(filename, self._data_dir)
        if "Stock_Quantity" not in df.columns:
            raise ValueError(f"File {filename} thiếu cột Stock_Quantity")
        low_stock = df[df["Stock_Quantity"] <= threshold].copy()
        low_stock = low_stock.sort_values("Stock_Quantity")
        key_cols = ["Product_Name", "SKU", "Stock_Quantity", "Reorder_Level", "Category", "Brand"]
        available = [c for c in key_cols if c in df.columns]
        return low_stock[available].to_dict(orient="records")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _pick_revenue_col(df: pd.DataFrame) -> str | None:
    for candidate in ("Total_Amount", "Total_Revenue", "Revenue"):
        if candidate in df.columns:
            return candidate
    return None
