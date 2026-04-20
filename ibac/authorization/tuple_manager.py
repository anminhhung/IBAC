"""
Component 3: Tuple Construction & Lifecycle

Chuyển đổi capabilities từ Intent Parser thành authorization tuples
và quản lý vòng đời của chúng trong FGA store.

Vòng đời một tuple:
  write_tuples()  →  [agent dùng quyền]  →  expire / delete_tuples()

Tại sao cần TTL?
  Ngăn "permission accumulation" — quyền được cấp ở turn 1 không nên
  còn hiệu lực ở turn 10 khi context hội thoại đã hoàn toàn thay đổi.
  Bài báo khuyến nghị TTL = 2-3 turns cho conversational agents.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from ibac.models.schemas import AuthorizationTuple, Capability


# ---------------------------------------------------------------------------
# FGA Store Protocol — để TupleManager không phụ thuộc vào implementation cụ thể
# ---------------------------------------------------------------------------

class FGAStore(Protocol):
    def write_allow(self, tuple_: AuthorizationTuple) -> None: ...
    def delete_allow(self, request_id: str, agent: str, tool: str, resource: str) -> None: ...
    def list_by_request(self, request_id: str) -> list[AuthorizationTuple]: ...


# ---------------------------------------------------------------------------
# Tuple Manager
# ---------------------------------------------------------------------------

class TupleManager:
    """
    Quản lý authorization tuples: ghi, xóa, và expire theo TTL.

    Chỉ Intent Parser (qua Orchestrator) được phép gọi write_tuples().
    Agent chính chỉ được phép đọc qua FGA check — không ghi trực tiếp.
    """

    def __init__(self, fga_store: FGAStore, default_ttl: int = 3) -> None:
        if default_ttl < 1:
            raise ValueError(f"TTL phải >= 1, nhận: {default_ttl}")
        self._store = fga_store
        self.default_ttl = default_ttl

    def write_tuples(
        self,
        request_id: str,
        capabilities: list[Capability],
        current_turn: int,
        ttl: int | None = None,
    ) -> list[AuthorizationTuple]:
        """
        Chuyển capabilities thành tuples và ghi vào FGA store.

        Trả về danh sách tuples đã ghi để caller có thể audit.
        """
        effective_ttl = ttl if ttl is not None else self.default_ttl
        written: list[AuthorizationTuple] = []
        for cap in capabilities:
            t = AuthorizationTuple(
                request_id=request_id,
                agent=cap.agent,
                tool=cap.tool,
                resource=cap.resource,
                created_turn=current_turn,
                ttl=effective_ttl,
            )
            self._store.write_allow(t)
            written.append(t)
        return written

    def delete_tuples(self, request_id: str) -> int:
        """
        Xóa tất cả tuples của một request (gọi khi request kết thúc).
        Trả về số tuples đã xóa.
        """
        tuples = self._store.list_by_request(request_id)
        for t in tuples:
            self._store.delete_allow(t.request_id, t.agent, t.tool, t.resource)
        return len(tuples)

    def expire_old_tuples(self, current_turn: int) -> int:
        """
        Quét và xóa tất cả tuples đã vượt quá TTL tính đến current_turn.
        Trả về số tuples đã expire.

        Trong OpenFGA thật, TTL được enforce bằng conditional tuples tại check-time.
        Method này dùng cho InMemoryFGAStore để dọn dẹp chủ động.
        """
        # Lấy tất cả request_id đang có, kiểm tra từng tuple
        all_tuples = self._store.list_by_request("*")
        expired_count = 0
        for t in all_tuples:
            if not t.is_valid(current_turn):
                self._store.delete_allow(t.request_id, t.agent, t.tool, t.resource)
                expired_count += 1
        return expired_count


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def capability_to_object_id(agent: str, tool: str, resource: str) -> str:
    """
    Chuyển (agent, tool, resource) thành object ID dùng trong FGA.

    Format: tool_invocation:{agent}:{tool}#{resource}
    Ví dụ: tool_invocation:email:send#bob@company.com
    """
    return f"tool_invocation:{agent}:{tool}#{resource}"
