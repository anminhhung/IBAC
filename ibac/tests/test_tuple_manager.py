"""
Unit tests cho Phase 4: Tuple Manager.

Chạy: pytest ibac/tests/test_tuple_manager.py -v
"""

import pytest
from ibac.authorization.tuple_manager import TupleManager, capability_to_object_id
from ibac.models.schemas import AuthorizationTuple, Capability


# ---------------------------------------------------------------------------
# Minimal InMemoryFGAStore dùng cho test
# ---------------------------------------------------------------------------

class InMemoryFGAStore:
    """FGA store in-memory đơn giản — chỉ dùng trong tests."""

    def __init__(self) -> None:
        # key: (request_id, agent, tool, resource)
        self._tuples: dict[tuple, AuthorizationTuple] = {}

    def write_allow(self, tuple_: AuthorizationTuple) -> None:
        key = (tuple_.request_id, tuple_.agent, tuple_.tool, tuple_.resource)
        self._tuples[key] = tuple_

    def delete_allow(self, request_id: str, agent: str, tool: str, resource: str) -> None:
        self._tuples.pop((request_id, agent, tool, resource), None)

    def list_by_request(self, request_id: str) -> list[AuthorizationTuple]:
        if request_id == "*":
            return list(self._tuples.values())
        return [t for t in self._tuples.values() if t.request_id == request_id]

    def count(self) -> int:
        return len(self._tuples)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    return InMemoryFGAStore()


@pytest.fixture
def manager(store):
    return TupleManager(store, default_ttl=3)


def _caps(*specs) -> list[Capability]:
    """Helper: tạo list Capability từ tuples (agent, tool, resource)."""
    return [Capability(agent=a, tool=t, resource=r, reasoning="test") for a, t, r in specs]


# ---------------------------------------------------------------------------
# Tests: Khởi tạo
# ---------------------------------------------------------------------------

class TestTupleManagerInit:
    def test_default_ttl(self, store):
        m = TupleManager(store)
        assert m.default_ttl == 3

    def test_custom_ttl(self, store):
        m = TupleManager(store, default_ttl=5)
        assert m.default_ttl == 5

    def test_invalid_ttl(self, store):
        with pytest.raises(ValueError, match="TTL"):
            TupleManager(store, default_ttl=0)


# ---------------------------------------------------------------------------
# Tests: write_tuples
# ---------------------------------------------------------------------------

class TestWriteTuples:
    def test_writes_single_capability(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        written = manager.write_tuples("req_1", caps, current_turn=0)

        assert len(written) == 1
        assert store.count() == 1

    def test_writes_multiple_capabilities(self, manager, store):
        caps = _caps(
            ("email", "send", "bob@company.com"),
            ("file", "read", "/docs/report.pdf"),
            ("contacts", "lookup", "bob"),
        )
        written = manager.write_tuples("req_1", caps, current_turn=0)

        assert len(written) == 3
        assert store.count() == 3

    def test_written_tuple_has_correct_fields(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        written = manager.write_tuples("req_abc", caps, current_turn=2)

        t = written[0]
        assert t.request_id == "req_abc"
        assert t.agent == "email"
        assert t.tool == "send"
        assert t.resource == "bob@company.com"
        assert t.created_turn == 2
        assert t.ttl == 3  # default

    def test_uses_default_ttl(self, manager, store):
        caps = _caps(("calendar", "read", "*"))
        written = manager.write_tuples("req_1", caps, current_turn=0)
        assert written[0].ttl == 3

    def test_uses_override_ttl(self, manager, store):
        caps = _caps(("calendar", "read", "*"))
        written = manager.write_tuples("req_1", caps, current_turn=0, ttl=10)
        assert written[0].ttl == 10

    def test_empty_capabilities_writes_nothing(self, manager, store):
        written = manager.write_tuples("req_1", [], current_turn=0)
        assert written == []
        assert store.count() == 0

    def test_different_requests_scoped_separately(self, manager, store):
        caps_a = _caps(("email", "send", "alice@corp.org"))
        caps_b = _caps(("file", "read", "/docs/b.pdf"))
        manager.write_tuples("req_A", caps_a, current_turn=0)
        manager.write_tuples("req_B", caps_b, current_turn=0)

        assert len(store.list_by_request("req_A")) == 1
        assert len(store.list_by_request("req_B")) == 1
        assert store.list_by_request("req_A")[0].resource == "alice@corp.org"
        assert store.list_by_request("req_B")[0].resource == "/docs/b.pdf"

    def test_returns_authorization_tuple_objects(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        written = manager.write_tuples("req_1", caps, current_turn=0)
        assert all(isinstance(t, AuthorizationTuple) for t in written)


# ---------------------------------------------------------------------------
# Tests: delete_tuples
# ---------------------------------------------------------------------------

class TestDeleteTuples:
    def test_deletes_all_tuples_for_request(self, manager, store):
        caps = _caps(
            ("email", "send", "bob@company.com"),
            ("file", "read", "/docs/report.pdf"),
        )
        manager.write_tuples("req_1", caps, current_turn=0)
        assert store.count() == 2

        deleted = manager.delete_tuples("req_1")
        assert deleted == 2
        assert store.count() == 0

    def test_delete_only_affects_target_request(self, manager, store):
        manager.write_tuples("req_A", _caps(("email", "send", "a@a.com")), current_turn=0)
        manager.write_tuples("req_B", _caps(("file", "read", "/b.pdf")), current_turn=0)

        manager.delete_tuples("req_A")

        assert store.count() == 1
        assert store.list_by_request("req_B")[0].resource == "/b.pdf"

    def test_delete_nonexistent_request_returns_zero(self, manager, store):
        deleted = manager.delete_tuples("req_nonexistent")
        assert deleted == 0

    def test_delete_idempotent(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        manager.write_tuples("req_1", caps, current_turn=0)

        manager.delete_tuples("req_1")
        deleted_again = manager.delete_tuples("req_1")
        assert deleted_again == 0


# ---------------------------------------------------------------------------
# Tests: expire_old_tuples (TTL)
# ---------------------------------------------------------------------------

class TestExpireOldTuples:
    def test_valid_tuples_not_expired(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        manager.write_tuples("req_1", caps, current_turn=0)  # TTL=3

        expired = manager.expire_old_tuples(current_turn=3)  # delta=3 == ttl → valid
        assert expired == 0
        assert store.count() == 1

    def test_expired_tuples_removed(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        manager.write_tuples("req_1", caps, current_turn=0)  # TTL=3

        expired = manager.expire_old_tuples(current_turn=4)  # delta=4 > ttl → expired
        assert expired == 1
        assert store.count() == 0

    def test_only_expired_tuples_removed(self, manager, store):
        manager.write_tuples("req_1", _caps(("email", "send", "a@a.com")), current_turn=0)
        manager.write_tuples("req_2", _caps(("file", "read", "/b.pdf")), current_turn=3)

        # Turn 6: req_1 (created=0, TTL=3) → delta=6 > 3 → expired
        #         req_2 (created=3, TTL=3) → delta=3 == 3 → valid
        expired = manager.expire_old_tuples(current_turn=6)
        assert expired == 1
        assert store.count() == 1
        assert store.list_by_request("req_2")[0].resource == "/b.pdf"

    def test_expire_multiple_tuples(self, manager, store):
        caps = _caps(
            ("email", "send", "a@a.com"),
            ("file", "read", "/b.pdf"),
            ("calendar", "read", "*"),
        )
        manager.write_tuples("req_1", caps, current_turn=0)

        expired = manager.expire_old_tuples(current_turn=10)
        assert expired == 3
        assert store.count() == 0

    def test_expire_empty_store(self, manager, store):
        expired = manager.expire_old_tuples(current_turn=99)
        assert expired == 0

    def test_ttl_boundary_turn_exactly_at_limit(self, manager, store):
        caps = _caps(("email", "send", "bob@company.com"))
        manager.write_tuples("req_1", caps, current_turn=1, ttl=2)

        # current_turn=3: delta = 3-1 = 2 == ttl → còn valid
        expired = manager.expire_old_tuples(current_turn=3)
        assert expired == 0

        # current_turn=4: delta = 4-1 = 3 > ttl → expired
        expired = manager.expire_old_tuples(current_turn=4)
        assert expired == 1


# ---------------------------------------------------------------------------
# Tests: capability_to_object_id
# ---------------------------------------------------------------------------

class TestCapabilityToObjectId:
    def test_email_send(self):
        assert capability_to_object_id("email", "send", "bob@company.com") == \
               "tool_invocation:email:send#bob@company.com"

    def test_file_read(self):
        assert capability_to_object_id("file", "read", "/docs/report.pdf") == \
               "tool_invocation:file:read#/docs/report.pdf"

    def test_wildcard_resource(self):
        assert capability_to_object_id("calendar", "read", "*") == \
               "tool_invocation:calendar:read#*"

    def test_contacts_lookup(self):
        assert capability_to_object_id("contacts", "lookup", "bob") == \
               "tool_invocation:contacts:lookup#bob"


# ---------------------------------------------------------------------------
# Tests: Agent không thể tự ghi tuple
# ---------------------------------------------------------------------------

class TestAgentCannotWriteTuples:
    def test_write_requires_explicit_call(self, store):
        """
        TupleManager.write_tuples() chỉ được gọi bởi Orchestrator (trusted).
        Agent không có reference đến TupleManager.
        Test này xác nhận store chỉ chứa những gì được ghi qua manager.
        """
        manager = TupleManager(store, default_ttl=3)
        # Store bắt đầu rỗng
        assert store.count() == 0

        # Chỉ sau khi write_tuples() được gọi mới có tuple
        manager.write_tuples("req_1", _caps(("email", "send", "bob@company.com")), current_turn=0)
        assert store.count() == 1

    def test_store_is_not_exposed_on_manager(self, manager):
        """_store là private attribute — agent không thể lấy reference qua public API."""
        public_attrs = [a for a in dir(manager) if not a.startswith("_")]
        assert "store" not in public_attrs
        assert "_store" not in public_attrs
