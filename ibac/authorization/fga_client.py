"""
Component 4: Unified Authorization với Deny Policies — InMemoryFGAClient

Implement FGAStore Protocol (từ tuple_manager.py) + check() để authorize tool calls.

Mô hình OpenFGA từ bài báo:
    define can_invoke: [user with within_ttl] but not blocked
    define blocked: [user]

Logic check():
    1. Có deny tuple khớp?  → DENIED (permanent, canEscalate=False)
    2. Có allow tuple hợp lệ (trong TTL)?  → ALLOWED
    3. Không có gì khớp?  → DENIED (canEscalate=True)

Deny tuples được ghi một lần lúc deploy — không thể override bởi user hay agent.
Allow tuples được ghi per-request bởi TupleManager — có TTL.
"""

from __future__ import annotations

from ibac.models.schemas import AuthorizationTuple, DenyPolicy


class InMemoryFGAClient:
    """
    FGA store in-memory: dùng cho prototype và testing.
    Implement cả FGAStore Protocol (cho TupleManager) và check() (cho ToolWrapper).

    Deny policies được load một lần lúc khởi tạo.
    Allow tuples được ghi/xóa per-request qua TupleManager.
    """

    def __init__(self) -> None:
        # Allow tuples: key = (request_id, agent, tool, resource)
        self._allow: dict[tuple, AuthorizationTuple] = {}
        # Deny policies: list vì cần kiểm tra wildcard matching
        self._deny: list[DenyPolicy] = []

    # ------------------------------------------------------------------
    # FGAStore Protocol (dùng bởi TupleManager)
    # ------------------------------------------------------------------

    def write_allow(self, tuple_: AuthorizationTuple) -> None:
        key = (tuple_.request_id, tuple_.agent, tuple_.tool, tuple_.resource)
        self._allow[key] = tuple_

    def delete_allow(self, request_id: str, agent: str, tool: str, resource: str) -> None:
        self._allow.pop((request_id, agent, tool, resource), None)

    def list_by_request(self, request_id: str) -> list[AuthorizationTuple]:
        if request_id == "*":
            return list(self._allow.values())
        return [t for t in self._allow.values() if t.request_id == request_id]

    # ------------------------------------------------------------------
    # Deny Policies (ghi một lần lúc deploy)
    # ------------------------------------------------------------------

    def add_deny_policy(self, policy: DenyPolicy) -> None:
        self._deny.append(policy)

    def list_deny_policies(self) -> list[DenyPolicy]:
        return list(self._deny)

    # ------------------------------------------------------------------
    # Authorization Check (dùng bởi ToolWrapper)
    # ------------------------------------------------------------------

    def check(
        self,
        request_id: str,
        agent: str,
        tool: str,
        resource: str,
        current_turn: int,
    ) -> "CheckResult":
        """
        Kiểm tra tool call có được phép không.

        Thứ tự ưu tiên (đúng theo bài báo):
          1. Deny policy khớp → BLOCKED (canEscalate=False)
          2. Allow tuple hợp lệ tồn tại → ALLOWED
          3. Không có allow tuple → DENIED (canEscalate=True)
        """
        # Bước 1: Kiểm tra deny policies — blocked là hard boundary
        for policy in self._deny:
            if policy.matches(agent, tool, resource):
                return CheckResult(allowed=False, blocked=True)

        # Bước 2: Tìm allow tuple còn trong TTL
        for t in self.list_by_request(request_id):
            if t.agent == agent and t.tool == tool and t.resource == resource:
                if t.is_valid(current_turn):
                    return CheckResult(allowed=True, blocked=False)
                # Tuple tồn tại nhưng đã hết hạn → coi như không có
                return CheckResult(allowed=False, blocked=False)

        # Bước 3: Không tìm thấy → từ chối, có thể escalate
        return CheckResult(allowed=False, blocked=False)


class CheckResult:
    """Kết quả từ InMemoryFGAClient.check()."""

    __slots__ = ("allowed", "blocked")

    def __init__(self, allowed: bool, blocked: bool) -> None:
        self.allowed = allowed
        self.blocked = blocked  # True → deny policy, False → not in intent

    def __repr__(self) -> str:
        if self.allowed:
            return "CheckResult(ALLOWED)"
        status = "BLOCKED" if self.blocked else "DENIED"
        return f"CheckResult({status})"
