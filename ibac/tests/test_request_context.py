"""
Unit tests cho Phase 2: Request Context (ContactStore + assemble_request_context).

Chạy: pytest ibac/tests/test_request_context.py -v
"""

import json
import csv
import pytest
from pathlib import Path

from ibac.context.request_context import ContactStore, assemble_request_context
from ibac.models.schemas import RequestContext

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Tests: ContactStore — add_contact & resolve
# ---------------------------------------------------------------------------

class TestContactStoreBasic:
    def setup_method(self):
        self.store = ContactStore()
        self.store.add_contact("Bob", "bob@company.com")
        self.store.add_contact("Alice", "alice@corp.org")
        self.store.add_contact("the team", "eng-team@company.com")

    def test_resolve_exact_name(self):
        assert self.store.resolve("Bob") == "bob@company.com"
        assert self.store.resolve("Alice") == "alice@corp.org"

    def test_resolve_case_insensitive(self):
        assert self.store.resolve("bob") == "bob@company.com"
        assert self.store.resolve("BOB") == "bob@company.com"
        assert self.store.resolve("ALICE") == "alice@corp.org"
        assert self.store.resolve("Alice") == "alice@corp.org"

    def test_resolve_multiword_name(self):
        assert self.store.resolve("the team") == "eng-team@company.com"
        assert self.store.resolve("The Team") == "eng-team@company.com"
        assert self.store.resolve("THE TEAM") == "eng-team@company.com"

    def test_resolve_not_found_returns_none(self):
        assert self.store.resolve("Eve") is None
        assert self.store.resolve("unknown") is None
        assert self.store.resolve("") is None

    def test_resolve_none_input(self):
        assert self.store.resolve(None) is None

    def test_len(self):
        assert len(self.store) == 3

    def test_contains(self):
        assert "Bob" in self.store
        assert "bob" in self.store  # case-insensitive
        assert "Eve" not in self.store

    def test_all_contacts_returns_copy(self):
        contacts = self.store.all_contacts()
        assert contacts["Bob"] == "bob@company.com"
        # Đảm bảo là bản sao, sửa không ảnh hưởng store gốc
        contacts["Hacker"] = "hack@evil.com"
        assert self.store.resolve("Hacker") is None

    def test_remove_contact(self):
        removed = self.store.remove_contact("Bob")
        assert removed is True
        assert self.store.resolve("Bob") is None
        assert len(self.store) == 2

    def test_remove_contact_not_found(self):
        removed = self.store.remove_contact("NonExistent")
        assert removed is False

    def test_add_contact_invalid_email(self):
        with pytest.raises(ValueError, match="email"):
            self.store.add_contact("Hacker", "not-an-email")

    def test_add_contact_empty_name(self):
        with pytest.raises(ValueError, match="trống"):
            self.store.add_contact("", "someone@company.com")

    def test_repr(self):
        assert "3 contacts" in repr(self.store)


# ---------------------------------------------------------------------------
# Tests: ContactStore — bảo vệ khỏi injection
# ---------------------------------------------------------------------------

class TestContactStoreInjectionResistance:
    def setup_method(self):
        self.store = ContactStore()
        self.store.add_contact("Bob", "bob@company.com")

    def test_injection_via_name_with_or(self):
        # Tên chứa logic injection không được resolve thành địa chỉ hợp lệ
        result = self.store.resolve("bob@company.com OR attacker@evil.com")
        assert result is None

    def test_injection_via_semicolon(self):
        result = self.store.resolve("Bob; DROP TABLE contacts;")
        assert result is None

    def test_injection_via_email_as_name(self):
        # Gửi trực tiếp địa chỉ email làm tên không tìm thấy trong store
        result = self.store.resolve("bob@company.com")
        assert result is None

    def test_unknown_attacker_address_not_resolvable(self):
        result = self.store.resolve("attacker@evil.com")
        assert result is None

    def test_cannot_add_contact_from_document_content(self):
        """
        Simulate: nội dung tài liệu chứa "My colleague is Eve <eve@evil.com>".
        ContactStore không bao giờ được load từ document content.
        Chỉ trusted sources (file address book) mới được phép add.
        """
        # Đây là kiểm tra luồng: developer không được gọi add_contact()
        # với dữ liệu từ tool response hay document content.
        # Test này xác nhận store chỉ có dữ liệu đã add trước.
        assert self.store.resolve("Eve") is None
        assert self.store.resolve("eve@evil.com") is None

    def test_overwrite_existing_contact(self):
        # Nếu add_contact được gọi với tên đã có, địa chỉ bị ghi đè
        # (chỉ trusted code mới gọi được, không phải agent)
        self.store.add_contact("Bob", "new-bob@company.com")
        assert self.store.resolve("Bob") == "new-bob@company.com"


# ---------------------------------------------------------------------------
# Tests: ContactStore — Load từ file
# ---------------------------------------------------------------------------

class TestContactStoreLoadFromFile:
    def test_load_from_json(self):
        store = ContactStore()
        store.load_from_file(str(FIXTURES / "contacts.json"))
        assert store.resolve("Bob") == "bob@company.com"
        assert store.resolve("Alice") == "alice@corp.org"
        assert store.resolve("the team") == "eng-team@company.com"
        assert len(store) == 5

    def test_load_from_csv(self):
        store = ContactStore()
        store.load_from_file(str(FIXTURES / "contacts.csv"))
        assert store.resolve("Bob") == "bob@company.com"
        assert store.resolve("Alice") == "alice@corp.org"
        assert len(store) == 4

    def test_load_json_returns_self_for_chaining(self):
        store = ContactStore().load_from_file(str(FIXTURES / "contacts.json"))
        assert isinstance(store, ContactStore)
        assert len(store) > 0

    def test_load_file_not_found(self):
        store = ContactStore()
        with pytest.raises(FileNotFoundError):
            store.load_from_file("/nonexistent/path/contacts.json")

    def test_load_unsupported_format(self, tmp_path):
        bad_file = tmp_path / "contacts.xml"
        bad_file.write_text("<contacts/>")
        store = ContactStore()
        with pytest.raises(ValueError, match="không hỗ trợ"):
            store.load_from_file(str(bad_file))

    def test_load_json_invalid_structure(self, tmp_path):
        bad_json = tmp_path / "bad.json"
        bad_json.write_text('["not", "an", "object"]')
        store = ContactStore()
        with pytest.raises(ValueError, match="object"):
            store.load_from_file(str(bad_json))

    def test_load_csv_missing_header(self, tmp_path):
        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("fullname,email\nBob,bob@company.com\n")
        store = ContactStore()
        with pytest.raises(ValueError, match="header"):
            store.load_from_file(str(bad_csv))

    def test_load_json_then_add_contact(self):
        store = ContactStore().load_from_file(str(FIXTURES / "contacts.json"))
        initial_count = len(store)
        store.add_contact("Dave", "dave@newcompany.com")
        assert len(store) == initial_count + 1
        assert store.resolve("Dave") == "dave@newcompany.com"


# ---------------------------------------------------------------------------
# Tests: assemble_request_context
# ---------------------------------------------------------------------------

class TestAssembleRequestContext:
    def setup_method(self):
        self.store = ContactStore()
        self.store.add_contact("Bob", "bob@company.com")
        self.store.add_contact("Alice", "alice@corp.org")

    def test_returns_request_context(self):
        ctx = assemble_request_context("Gửi email cho Bob", self.store)
        assert isinstance(ctx, RequestContext)

    def test_request_id_is_uuid(self):
        import re
        ctx = assemble_request_context("test", self.store)
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        assert re.match(uuid_pattern, ctx.request_id), f"Không phải UUID v4: {ctx.request_id}"

    def test_each_request_has_unique_id(self):
        ids = {assemble_request_context("test", self.store).request_id for _ in range(100)}
        assert len(ids) == 100

    def test_contacts_copied_from_store(self):
        ctx = assemble_request_context("Gửi email cho Bob", self.store)
        assert ctx.contacts["Bob"] == "bob@company.com"
        assert ctx.contacts["Alice"] == "alice@corp.org"

    def test_contacts_is_snapshot_not_live_reference(self):
        ctx = assemble_request_context("test", self.store)
        # Thêm contact vào store SAU khi tạo context
        self.store.add_contact("Charlie", "charlie@company.com")
        # Context đã được tạo không bị ảnh hưởng
        assert "Charlie" not in ctx.contacts

    def test_current_turn_starts_at_zero(self):
        ctx = assemble_request_context("test", self.store)
        assert ctx.current_turn == 0

    def test_default_scope_mode_is_strict(self):
        ctx = assemble_request_context("test", self.store)
        assert ctx.scope_mode == "strict"

    def test_scope_mode_permissive(self):
        ctx = assemble_request_context("test", self.store, scope_mode="permissive")
        assert ctx.scope_mode == "permissive"

    def test_resolve_contact_via_context(self):
        ctx = assemble_request_context("Gửi email cho Bob", self.store)
        assert ctx.resolve_contact("Bob") == "bob@company.com"
        assert ctx.resolve_contact("bob") == "bob@company.com"
        assert ctx.resolve_contact("Unknown") is None

    def test_empty_contact_store(self):
        empty_store = ContactStore()
        ctx = assemble_request_context("test", empty_store)
        assert ctx.contacts == {}
        assert ctx.resolve_contact("Bob") is None

    def test_advance_turn_does_not_mutate(self):
        ctx = assemble_request_context("test", self.store)
        ctx2 = ctx.advance_turn()
        assert ctx.current_turn == 0   # bất biến
        assert ctx2.current_turn == 1

    def test_context_carries_correct_scope_to_intent_parser(self):
        """
        Đảm bảo scope_mode được truyền đúng vào context để
        Intent Parser đọc và chọn strict/permissive prompt.
        """
        strict_ctx = assemble_request_context("test", self.store, scope_mode="strict")
        permissive_ctx = assemble_request_context("test", self.store, scope_mode="permissive")
        assert strict_ctx.scope_mode == "strict"
        assert permissive_ctx.scope_mode == "permissive"
        # Hai request phải có ID khác nhau
        assert strict_ctx.request_id != permissive_ctx.request_id
