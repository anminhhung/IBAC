"""
Component 1: Request Context

Chuẩn bị ngữ cảnh tin cậy TRƯỚC khi Intent Parser chạy.

Tại sao cần làm điều này trước?
  Intent Parser cần biết "Bob" là ai để cấp quyền cụ thể
  email:send#bob@company.com thay vì email:send#*.
  Nhưng nếu để agent tự resolve (gọi tool search_contacts),
  ta rơi vào chicken-and-egg: cần quyền để resolve, cần resolve để cấp quyền.

  → ContactStore giải quyết bằng cách load danh bạ tin cậy TRƯỚC,
    hoàn toàn ngoài vòng kiểm soát của agent.

Nguồn dữ liệu được tin cậy:
  ✅ ContactStore (address book đã xác minh)
  ❌ Email history        (attacker có thể gửi email chứa địa chỉ giả)
  ❌ Calendar entries     (phishing invite có thể đặt attacker là attendee)
  ❌ Document content     (tài liệu có thể chứa "colleague: attacker@evil.com")
"""

from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path
from typing import Literal

from ibac.models.schemas import RequestContext


class ContactStore:
    """
    Kho danh bạ tin cậy — nguồn dữ liệu DUY NHẤT được dùng để
    resolve tên người dùng thành địa chỉ email cho mục đích phân quyền.

    Dữ liệu chỉ được nạp từ file đã xác minh (JSON hoặc CSV),
    không bao giờ từ runtime data như email, calendar, hay web content.

    Ví dụ nội dung file JSON:
        {
          "Bob": "bob@company.com",
          "Alice": "alice@corp.org",
          "the team": "eng-team@company.com"
        }

    Ví dụ nội dung file CSV:
        name,address
        Bob,bob@company.com
        Alice,alice@corp.org
    """

    def __init__(self) -> None:
        # Lưu dạng lowercase key để resolve case-insensitive
        self._store: dict[str, str] = {}
        # Giữ lại key gốc để debug/audit
        self._original_keys: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Load từ file
    # ------------------------------------------------------------------

    def load_from_file(self, path: str) -> "ContactStore":
        """
        Nạp danh bạ từ file JSON hoặc CSV.

        JSON format: {"Tên": "email@domain.com", ...}
        CSV format:  cột 'name' và 'address' (có header)

        Trả về self để cho phép chaining:
            store = ContactStore().load_from_file("contacts.json")
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file danh bạ: {path}")

        suffix = file_path.suffix.lower()
        if suffix == ".json":
            self._load_json(file_path)
        elif suffix == ".csv":
            self._load_csv(file_path)
        else:
            raise ValueError(f"Định dạng file không hỗ trợ: {suffix}. Chỉ hỗ trợ .json và .csv")

        return self

    def _load_json(self, path: Path) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("File JSON danh bạ phải là object {name: address, ...}")
        for name, address in data.items():
            self._add_entry(str(name), str(address))

    def _load_csv(self, path: Path) -> None:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "name" not in reader.fieldnames or "address" not in reader.fieldnames:
                raise ValueError("File CSV phải có header 'name' và 'address'")
            for row in reader:
                name = row.get("name", "").strip()
                address = row.get("address", "").strip()
                if name and address:
                    self._add_entry(name, address)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_contact(self, name: str, address: str) -> None:
        """
        Thêm contact mới vào store (chỉ dùng cho trusted sources).

        Không dùng method này để thêm contact từ:
        - Tool response của agent
        - Nội dung email hay tài liệu
        - Bất kỳ dữ liệu runtime nào
        """
        if not name or not name.strip():
            raise ValueError("Tên contact không được để trống")
        if not address or "@" not in address:
            raise ValueError(f"Địa chỉ email không hợp lệ: '{address}'")
        self._add_entry(name.strip(), address.strip())

    def _add_entry(self, name: str, address: str) -> None:
        key = name.lower()
        self._store[key] = address
        self._original_keys[key] = name

    def resolve(self, name: str) -> str | None:
        """
        Tra cứu tên → địa chỉ email đã xác minh.

        Tìm kiếm case-insensitive.
        Trả về None nếu không tìm thấy (KHÔNG fallback hay infer).

        Ví dụ:
            store.resolve("Bob")  → "bob@company.com"
            store.resolve("bob")  → "bob@company.com"  (case-insensitive)
            store.resolve("Eve")  → None
        """
        if not name:
            return None
        return self._store.get(name.strip().lower())

    def remove_contact(self, name: str) -> bool:
        """Xóa contact. Trả về True nếu tìm thấy và xóa, False nếu không có."""
        key = name.strip().lower()
        if key in self._store:
            del self._store[key]
            del self._original_keys[key]
            return True
        return False

    def all_contacts(self) -> dict[str, str]:
        """Trả về bản sao toàn bộ danh bạ với key gốc (không lowercase)."""
        return {self._original_keys[k]: v for k, v in self._store.items()}

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, name: str) -> bool:
        return name.strip().lower() in self._store

    def __repr__(self) -> str:
        return f"ContactStore({len(self._store)} contacts)"


# ---------------------------------------------------------------------------
# Request Context Assembly
# ---------------------------------------------------------------------------

def assemble_request_context(
    user_message: str,
    contact_store: ContactStore,
    scope_mode: Literal["strict", "permissive"] = "strict",
    ttl: int = 3,
) -> RequestContext:
    """
    Tạo RequestContext cho một request mới.

    Đây là bước đầu tiên trong pipeline IBAC, chạy TRƯỚC Intent Parser.
    Context được tạo ra hoàn toàn từ trusted sources — không có LLM call,
    không có tool call, không có dữ liệu runtime.

    Args:
        user_message:  Yêu cầu gốc của người dùng (chỉ dùng để log, không parse ở đây)
        contact_store: ContactStore đã được load từ trusted address book
        scope_mode:    "strict" (tối thiểu) hoặc "permissive" (bao gồm prerequisites)
        ttl:           Số turns mỗi authorization tuple còn hiệu lực

    Returns:
        RequestContext với request_id duy nhất, contacts từ trusted store,
        current_turn=0, sẵn sàng đưa vào Intent Parser.

    Ví dụ:
        store = ContactStore().load_from_file("contacts.json")
        ctx = assemble_request_context("Gửi báo cáo cho Bob", store, scope_mode="strict")
        ctx.request_id   # "3f2a1b4c-..."
        ctx.contacts     # {"Bob": "bob@company.com", ...}
        ctx.current_turn # 0
    """
    return RequestContext(
        request_id=str(uuid.uuid4()),
        contacts=contact_store.all_contacts(),
        current_turn=0,
        scope_mode=scope_mode,
    )
