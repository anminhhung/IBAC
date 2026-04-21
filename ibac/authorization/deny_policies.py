"""
Deny Policies mặc định — ghi một lần lúc deploy, áp dụng vĩnh viễn.

Deny tuples đại diện cho chính sách bảo mật của tổ chức:
các thao tác không bao giờ được phép dù user có yêu cầu.

Theo bài báo: deny policies không thể bị override bởi user approval hay escalation.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from ibac.models.schemas import DenyPolicy
from ibac.authorization.fga_client import InMemoryFGAClient

# ---------------------------------------------------------------------------
# Default policies — áp dụng cho mọi deployment
# ---------------------------------------------------------------------------

DEFAULT_POLICIES: list[DenyPolicy] = [
    DenyPolicy(agent="shell",  tool="exec",   resource="*",        reason="Không bao giờ cho phép thực thi shell"),
    DenyPolicy(agent="*",      tool="*",       resource="/etc/*",   reason="Cấm đọc/ghi file hệ thống /etc/"),
    DenyPolicy(agent="*",      tool="*",       resource="~/.ssh/*", reason="Cấm truy cập SSH keys"),
    DenyPolicy(agent="*",      tool="*",       resource="~/.env*",  reason="Cấm đọc file .env chứa secrets"),
    DenyPolicy(agent="*",      tool="delete",  resource="/*",       reason="Cấm xóa file ở root path"),
    # Data agent: không bao giờ cho phép xóa hoặc ghi đè dữ liệu gốc
    DenyPolicy(agent="data",   tool="delete",  resource="*",        reason="Cấm xóa file dữ liệu"),
    DenyPolicy(agent="data",   tool="write",   resource="*",        reason="Cấm ghi đè file dữ liệu gốc"),
]


def load_default_deny_policies(fga_client: InMemoryFGAClient) -> int:
    """
    Ghi default deny policies vào FGA client.
    Trả về số policies đã load.
    """
    for policy in DEFAULT_POLICIES:
        fga_client.add_deny_policy(policy)
    return len(DEFAULT_POLICIES)


def load_deny_policies_from_yaml(fga_client: InMemoryFGAClient, path: str) -> int:
    """
    Load thêm custom deny policies từ YAML file.

    Format YAML:
        deny_policies:
          - agent: shell
            tool: exec
            resource: "*"
            reason: "Cấm shell"
          - agent: "*"
            tool: "*"
            resource: "/secrets/*"
            reason: "Cấm đọc secrets"

    Trả về số policies đã load.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file config: {path}")

    with open(file_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    policies_data = data.get("deny_policies", [])
    count = 0
    for item in policies_data:
        policy = DenyPolicy(**item)
        fga_client.add_deny_policy(policy)
        count += 1
    return count
