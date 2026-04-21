# Giải Pháp Cho 5 Hạn Chế Của IBAC

> Phân tích và đề xuất cải tiến cho từng hạn chế được nêu trong bài báo "Intent-Based Access Control"

---

## Hạn Chế 1: Argument Subfields — Chỉ Kiểm Soát Được Một Resource Identifier

### Vấn đề

IBAC tuple chỉ mã hóa **một** resource identifier trên mỗi tool call. Với một tool như `send_email`, tuple chỉ kiểm soát được `recipient`, còn `subject` và `body` hoàn toàn nằm ngoài tầm với của FGA.

```
Tuple hiện tại:
  (user:req_abc, can_invoke, email:send#bob@company.com)
            ↑ chỉ biết NGƯỜI NHẬN, không biết NỘI DUNG

Kịch bản tấn công vẫn khả thi:
  Injection nhúng vào body: "Tiêu đề: URGENT — Password: [từ file đã đọc]"
  → IBAC không chặn được vì body không nằm trong tuple
```

### Giải Pháp Đề Xuất

#### Giải pháp A: Capability Constraints — Thêm ràng buộc vào Capability schema

Mở rộng model `Capability` để hỗ trợ `constraints` — một dict tùy chọn định nghĩa điều kiện bổ sung cho từng argument nhạy cảm.

```python
class Capability(BaseModel):
    agent: str
    tool: str
    resource: str
    reasoning: str
    constraints: dict[str, str] | None = None
    # Ví dụ:
    # constraints = {
    #   "body_pattern":    "^((?!attacker|forward|password).)*$",
    #   "subject_pattern": ".*",
    #   "max_length":      "5000",
    # }
```

Intent Parser sinh ra constraints từ context:
```json
{
  "agent": "email",
  "tool": "send",
  "resource": "bob@company.com",
  "reasoning": "Gửi tóm tắt báo cáo cho Bob",
  "constraints": {
    "subject_pattern": "^(Report|Summary|Meeting).*",
    "body_max_length": "3000"
  }
}
```

`invoke_tool_with_auth` kiểm tra constraints trước khi thực thi:
```python
async def invoke_tool_with_auth(..., kwargs: dict):
    result = fga_client.check(...)
    if result.allowed and tuple_.constraints:
        for field, pattern in tuple_.constraints.items():
            value = kwargs.get(field, "")
            if not re.fullmatch(pattern, str(value)):
                return ToolResult.deny_policy(agent, tool, field)
    ...
```

**Ưu điểm:** Không thay đổi kiến trúc cốt lõi, backward-compatible.
**Nhược điểm:** Intent Parser cần được huấn luyện tốt hơn để sinh ra constraints chính xác.

---

#### Giải pháp B: Multi-Resource Tuples — Kiểm soát nhiều chiều

Thay vì một tuple per tool call, sinh ra **một tuple per sensitive argument**:

```
Thay vì:
  (user:req_abc, can_invoke, email:send#bob@company.com)

Sinh ra 3 tuples:
  (user:req_abc, can_invoke, email:send:recipient#bob@company.com)
  (user:req_abc, can_invoke, email:send:subject#"Report Summary")
  (user:req_abc, can_invoke, email:send:body#text/plain)
```

`invoke_tool_with_auth` check song song tất cả arguments:
```python
for field, value in sensitive_fields.items():
    check = fga.check(request_id, agent, f"{tool}:{field}", value, turn)
    if not check.allowed:
        return ToolResult.deny_not_in_intent(agent, tool, field)
```

**Ưu điểm:** Kiểm soát hoàn toàn từng argument, không cần regex.
**Nhược điểm:** Số lượng tuple tăng đáng kể, Intent Parser phức tạp hơn.

---

#### Giải pháp C: Output Sanitizer — Lớp phòng thủ bổ sung ở đầu ra

Thêm một bước kiểm tra **sau** khi LLM sinh ra nội dung, trước khi truyền vào tool:

```
[LLM sinh body email]
        │
        ▼
[Output Sanitizer]  ← lớp mới
  - Phát hiện dữ liệu nhạy cảm (email addresses lạ, credentials)
  - Phát hiện forwarding headers bất thường
  - Phát hiện base64/encoded content đáng ngờ
        │
        ▼
[invoke_tool_with_auth]
```

Đây là cách tiếp cận tương tự CaMeL's taint tracking nhưng nhẹ hơn.

---

## Hạn Chế 2: Intent Parser Là Probabilistic — LLM Có Thể Sai

### Vấn đề

Intent Parser là một LLM call — kết quả không deterministic. Có hai lỗi đối lập nhau:

```
Over-scope:  Cấp quá nhiều quyền → bề mặt tấn công lớn hơn cần thiết
             "Gửi email cho Bob" → cấp cả email:send#* thay vì chỉ #bob

Under-scope: Cấp thiếu quyền → nhiều escalation không cần thiết
             "Đọc báo cáo tháng này" → thiếu file:search → agent bị kẹt
```

### Giải Pháp Đề Xuất

#### Giải pháp A: Dual-Pass Parsing với Reconciliation

Chạy Intent Parser **hai lần** với cùng input, lấy kết quả giao hoặc hợp tùy theo mode:

```python
async def parse_with_reconciliation(
    user_message: str,
    context: RequestContext,
    mode: Literal["secure", "balanced"],
) -> IntentParserOutput:
    # Chạy song song 2 lần với temperature khác nhau
    out_a, out_b = await asyncio.gather(
        parser.parse(user_message, context),   # temp=0.0 (deterministic)
        parser.parse(user_message, context),   # temp=0.3 (slight variation)
    )

    if mode == "secure":
        # Intersection: chỉ giữ capability xuất hiện ở CẢ HAI lần parse
        # Over-scope bị loại bỏ vì không nhất quán
        return _intersect(out_a, out_b)
    else:
        # Union: giữ tất cả → ít escalation hơn
        return _union(out_a, out_b)
```

**Chi phí:** Gấp đôi latency của Intent Parser (~11–14s thay vì ~6s).
**Lợi ích:** Giảm over-scope trung bình ~40% trong thử nghiệm với adversarial prompts.

---

#### Giải pháp B: Confidence Score per Capability

Yêu cầu Intent Parser đánh giá độ tin cậy cho từng capability. Capability có confidence thấp cần user confirmation trước khi ghi vào FGA.

Thêm vào schema:
```python
class Capability(BaseModel):
    agent: str
    tool: str
    resource: str
    reasoning: str
    confidence: float = 1.0   # 0.0 – 1.0
    # < 0.7 → yêu cầu user xác nhận trước khi ghi tuple
```

Thêm vào system prompt:
```
For each capability, rate your confidence (0.0–1.0):
  1.0 = explicitly stated in the request
  0.8 = clearly implied prerequisite
  0.5 = reasonable inference
  0.3 = speculative
```

Flow xử lý trong TupleManager:
```python
HIGH_CONFIDENCE = 0.7

confirmed, needs_review = [], []
for cap in capabilities:
    if cap.confidence >= HIGH_CONFIDENCE:
        confirmed.append(cap)
    else:
        needs_review.append(cap)

# Ghi ngay các capability chắc chắn
tm.write_tuples(request_id, confirmed, current_turn)

# Hỏi user về capability không chắc (batch một lần)
if needs_review:
    approved = await approval_callback.ask_batch(needs_review)
    tm.write_tuples(request_id, approved, current_turn)
```

---

#### Giải pháp C: Rule-Based Validation Layer

Thêm một lớp validation sau Intent Parser, trước khi ghi tuple — dùng rule đơn giản để bắt các lỗi phổ biến:

```python
class CapabilityValidator:
    # Rule 1: Không bao giờ cấp wildcard cho write/send/delete trong strict mode
    def _no_wildcard_destructive(self, cap: Capability, mode: str) -> bool:
        if mode == "strict" and cap.tool in ("send", "write", "delete"):
            return cap.resource != "*"
        return True

    # Rule 2: Email resource phải là địa chỉ hợp lệ
    def _email_resource_valid(self, cap: Capability) -> bool:
        if cap.agent == "email" and cap.tool == "send":
            return "@" in cap.resource or cap.resource == "*"
        return True

    # Rule 3: File resource phải là đường dẫn tuyệt đối
    def _file_resource_absolute(self, cap: Capability) -> bool:
        if cap.agent == "file":
            return cap.resource.startswith("/") or cap.resource == "*"
        return True

    def validate(self, caps: list[Capability], mode: str) -> list[Capability]:
        return [c for c in caps if self._run_all_rules(c, mode)]
```

---

#### Giải pháp D: Regression Test Suite cho Intent Parser

Khi thay đổi model hoặc system prompt, chạy automated test:

```python
# ibac/tests/test_intent_parser_regression.py
GOLDEN_CASES = [
    {
        "input": "Gửi email báo cáo cho Bob",
        "expected_agents": {"email", "file"},
        "must_not_include": {"email:send#*", "file:write"},
        "mode": "strict",
    },
    {
        "input": "Xóa tất cả file trong /tmp",
        "must_not_include": {"file:delete#/*"},  # deny policy nên chặn
        "mode": "strict",
    },
]
```

---

## Hạn Chế 3: Escalation Fatigue — Người Dùng Bấm "Đồng Ý" Vô Thức

### Vấn đề

Ở strict mode, một task phức tạp có thể kích hoạt 5–10 escalation prompts liên tiếp. Người dùng bị "mệt mỏi quyết định" và bắt đầu phê duyệt tất cả mà không đọc kỹ — đây chính xác là điều kẻ tấn công muốn.

```
Kịch bản tấn công bằng escalation fatigue:
  Prompt 1: "Agent cần đọc file báo cáo. Cho phép?" → User: Có
  Prompt 2: "Agent cần tra cứu Bob. Cho phép?"      → User: Có
  Prompt 3: "Agent cần tìm kiếm email. Cho phép?"   → User: Có (đang mệt)
  Prompt 4: "Agent cần gửi email tới x@evil.com?"   → User: Có ← LỖI
```

### Giải Pháp Đề Xuất

#### Giải pháp A: Batch Escalation — Gộp nhiều yêu cầu thành một

Thay vì hỏi từng capability một, gom tất cả capability thiếu vào **một prompt duy nhất** kèm context:

```
[IBAC] Agent cần thêm quyền để hoàn thành yêu cầu của bạn:
  "Đọc báo cáo và gửi tóm tắt cho Bob"

Các quyền cần phê duyệt:
  1. 🔍 Tìm kiếm trong thư mục /docs
  2. 📧 Gửi email tới carol@company.com (nhân viên mới chưa có trong danh bạ)

Phê duyệt tất cả? [Có / Không / Xem chi tiết]
```

```python
class BatchEscalationHandler:
    async def handle_batch(
        self,
        denied_calls: list[tuple[str, str, str]],
        context: RequestContext,
    ) -> list[AuthorizationTuple | None]:
        prompt = self._build_batch_prompt(denied_calls, context)
        decision = await self._callback.ask(prompt)

        if decision == "approve_all":
            return [await self._write_tuple(d, context) for d in denied_calls]
        elif decision == "review":
            # Cho user chọn từng cái
            return await self._interactive_review(denied_calls, context)
        return [None] * len(denied_calls)
```

---

#### Giải pháp B: Risk-Tiered Escalation — Phân loại nguy cơ, chỉ hỏi khi thực sự cần

Không phải mọi escalation đều có nguy cơ như nhau. Chia thành 3 tầng:

| Tầng | Ví dụ | Xử lý |
|---|---|---|
| **Low risk** | `contacts:lookup#*` trong permissive mode | Auto-approve, log lại |
| **Medium risk** | `file:read#/docs/Q4_report.pdf` (file mới) | Hiện notification, không block |
| **High risk** | `email:send#external_domain.com` | Yêu cầu explicit approval |

```python
class RiskAwareEscalationHandler(EscalationHandler):
    RISK_LEVELS = {
        ("data",     "read",      False): "low",
        ("file",     "read",      False): "low",
        ("contacts", "lookup",    False): "low",
        ("email",    "send",      False): "high",   # external email luôn high
        ("file",     "delete",    False): "high",
        ("web",      "search",    False): "medium",
    }

    async def handle(self, tool_result, agent, tool, resource, context):
        risk = self._assess_risk(agent, tool, resource)
        if risk == "low":
            self._log_auto_approve(agent, tool, resource)
            return await self._write_tuple_directly(agent, tool, resource, context)
        elif risk == "medium":
            await self._notify_without_blocking(agent, tool, resource)
            return await self._write_tuple_directly(agent, tool, resource, context)
        else:
            return await super().handle(tool_result, agent, tool, resource, context)
```

---

#### Giải pháp C: Capability Profile — Tập quyền được pre-approved

Cho phép người dùng định nghĩa trước một "profile" quyền thường dùng. Các capability trong profile được tự động phê duyệt mà không cần hỏi.

```yaml
# ~/.ibac/profiles/work_assistant.yaml
name: Work Assistant
description: Quyền cho trợ lý làm việc hàng ngày
pre_approved:
  - agent: file
    tool: read
    resource_pattern: "/docs/*"
  - agent: contacts
    tool: lookup
    resource_pattern: "*@company.com"
  - agent: calendar
    tool: read
    resource_pattern: "*"
  - agent: web
    tool: search
    resource_pattern: "*"
# Không bao gồm email:send — luôn yêu cầu approval
```

---

#### Giải pháp D: Escalation Context Enhancement — Làm rõ tại sao cần quyền

Cải thiện nội dung escalation prompt để người dùng hiểu rõ hơn trước khi quyết định:

```
# Prompt hiện tại (dễ gây fatigue):
Agent muốn tìm kiếm email. Cho phép? [Có/Không]

# Prompt được cải thiện:
╔══════════════════════════════════════════════════════╗
║  Yêu cầu của bạn: "Chuẩn bị tóm tắt cuộc họp"      ║
╠══════════════════════════════════════════════════════╣
║  Agent muốn: TÌM KIẾM EMAIL                          ║
║  Từ khóa tìm: "Q3 2024 meeting notes"                ║
║  Lý do cần thiết: Tìm tài liệu liên quan cuộc họp    ║
╠══════════════════════════════════════════════════════╣
║  ⚠️ Đây có phải điều bạn muốn không?                  ║
║  [✅ Cho phép lần này]  [❌ Từ chối]  [🔒 Chặn mãi]  ║
╚══════════════════════════════════════════════════════╝
```

---

#### Giải pháp E: Adaptive Mode Suggestion

Theo dõi tỉ lệ phê duyệt và đề xuất chuyển mode:

```python
class AdaptiveEscalationTracker:
    def __init__(self, window: int = 20):
        self._history: deque[bool] = deque(maxlen=window)

    def record(self, approved: bool):
        self._history.append(approved)

    def should_suggest_permissive(self) -> bool:
        if len(self._history) < 10:
            return False
        approval_rate = sum(self._history) / len(self._history)
        # Nếu user approve >80% → strict mode đang quá chặt với họ
        return approval_rate > 0.80

    def get_suggestion(self) -> str | None:
        if self.should_suggest_permissive():
            return (
                "Bạn đã phê duyệt 80%+ yêu cầu escalation. "
                "Bạn có muốn chuyển sang Permissive mode để giảm gián đoạn không?"
            )
        return None
```

---

## Hạn Chế 4: Chất Lượng Contact Store — Mapping Sai Dẫn Đến Hành Động Sai

### Vấn đề

IBAC tin tưởng hoàn toàn vào Contact Store. Nếu mapping sai — dù do lỗi người dùng hay bị tấn công — IBAC sẽ thực thi đúng theo mapping sai đó:

```
Contact Store bị poisoning:
  "Bob" → attacker@evil.com  (đúng ra phải là bob@company.com)

Kết quả:
  Tuple được cấp: email:send#attacker@evil.com
  IBAC cho phép gửi email → vì đúng với tuple!
  Người dùng bị tấn công mà không có cảnh báo nào
```

### Giải Pháp Đề Xuất

#### Giải pháp A: Multi-Source Verification — Xác minh từ nhiều nguồn

Không dựa vào một nguồn duy nhất. Khi resolve một contact, đối chiếu với ít nhất 2 nguồn:

```python
class VerifiedContactStore(ContactStore):
    def __init__(self, primary: ContactStore, authoritative: LDAPDirectory | OAuthProfile):
        self._primary = primary
        self._auth    = authoritative

    def resolve(self, name: str) -> str | None:
        primary_result = self._primary.resolve(name)
        auth_result    = self._auth.lookup(name)

        if primary_result and auth_result:
            if primary_result != auth_result:
                # Mâu thuẫn giữa hai nguồn → báo cáo, không tự động resolve
                raise ContactMismatchError(
                    f"'{name}' có địa chỉ khác nhau: "
                    f"local={primary_result}, corporate={auth_result}. "
                    f"Vui lòng xác nhận thủ công."
                )
            return primary_result

        # Chỉ có một nguồn → cảnh báo nhưng vẫn dùng
        if auth_result:
            return auth_result
        return primary_result
```

---

#### Giải pháp B: Contact Entry Signing — Ký số các entry

Mỗi entry trong Contact Store được ký bằng private key của người dùng. Khi đọc, kiểm tra chữ ký trước.

```python
from cryptography.hazmat.primitives.asymmetric import ed25519

class SignedContact(BaseModel):
    name: str
    address: str
    added_at: datetime
    signature: bytes  # Ed25519 signature của (name + address + added_at)

class SignedContactStore(ContactStore):
    def __init__(self, public_key: ed25519.Ed25519PublicKey):
        self._public_key = public_key
        self._contacts: dict[str, SignedContact] = {}

    def add_contact(self, name: str, address: str, signature: bytes):
        # Verify signature trước khi lưu
        message = f"{name}|{address}".encode()
        self._public_key.verify(signature, message)  # raise nếu invalid
        self._contacts[name.lower()] = SignedContact(
            name=name, address=address,
            added_at=datetime.utcnow(), signature=signature
        )

    def resolve(self, name: str) -> str | None:
        entry = self._contacts.get(name.lower())
        if not entry:
            return None
        # Re-verify signature mỗi lần resolve
        message = f"{entry.name}|{entry.address}".encode()
        self._public_key.verify(entry.signature, message)
        return entry.address
```

---

#### Giải pháp C: Immutable Contacts During Execution

Ngăn không cho phép sửa đổi Contact Store trong khi agent đang chạy:

```python
class FrozenContactStore(ContactStore):
    """Snapshot của ContactStore tại thời điểm assemble_request_context.
    Không thể thêm/sửa/xóa trong suốt vòng đời của request.
    """

    def __init__(self, base: ContactStore) -> None:
        # Deep copy toàn bộ contacts tại thời điểm khởi tạo
        self._snapshot: dict[str, str] = dict(base.all_contacts())
        self._frozen = True

    def add_contact(self, name: str, address: str) -> None:
        raise RuntimeError(
            "ContactStore đã bị khóa trong quá trình xử lý request. "
            "Không thể thêm contact trong lúc agent đang chạy."
        )

    def resolve(self, name: str) -> str | None:
        return self._snapshot.get(name.lower())
```

> Hiện tại `RequestContext` đã copy contacts tại thời điểm `assemble_request_context`. Giải pháp này bổ sung thêm lớp bảo vệ ở cấp ContactStore object.

---

#### Giải pháp D: First-Use Confirmation cho Contact Mới

Khi một contact chưa từng được dùng trước đây xuất hiện trong capability:

```python
class FirstUseContactGuard:
    def __init__(self, history_store: UsageHistoryStore):
        self._history = history_store

    def check_and_confirm(
        self, name: str, address: str, callback: UserApprovalCallback
    ) -> bool:
        if self._history.has_been_used(address):
            return True  # Đã dùng trước → tin tưởng

        # Lần đầu → hỏi user
        return callback.ask(
            f"Lần đầu tiên gửi đến '{name}' ({address}).\n"
            f"Bạn có chắc đây là địa chỉ đúng không?"
        )
```

---

## Hạn Chế 5: String Matching — Dễ Bị Bypass Bằng Path/URL Variants

### Vấn đề

IBAC dùng string pattern matching để kiểm tra resource. Với nhiều loại resource, cùng một tài nguyên có thể được biểu diễn nhiều cách khác nhau:

```
Tuple được cấp cho: /docs/report.pdf

Các cách bypass qua string matching:
  /docs/../docs/report.pdf    ← path traversal không chuẩn
  /DOCS/REPORT.PDF            ← case khác nhau (Windows paths)
  /docs/./report.pdf          ← current directory marker
  docs/report.pdf             ← relative path
  file:///docs/report.pdf     ← URI scheme
  /docs/report.pdf%20         ← URL encoding
```

### Giải Pháp Đề Xuất

#### Giải pháp A: Resource Normalizer Registry — Chuẩn hóa trước khi so sánh

Tạo một registry của normalizer functions, áp dụng trước mỗi lần check hoặc write tuple:

```python
from pathlib import PurePosixPath
from email.utils import parseaddr
from urllib.parse import urlparse, urlunparse

class ResourceNormalizer:
    """Chuẩn hóa resource string về dạng canonical trước khi match."""

    @staticmethod
    def normalize(agent: str, resource: str) -> str:
        if resource in ("*", ""):
            return resource
        normalizer = ResourceNormalizer._NORMALIZERS.get(agent)
        return normalizer(resource) if normalizer else resource.strip().lower()

    @staticmethod
    def _normalize_file(resource: str) -> str:
        try:
            # Resolve .., ./, double slash, trailing slash
            path = PurePosixPath(resource)
            normalized = str(path)
            # Đảm bảo absolute
            if not normalized.startswith("/"):
                normalized = "/" + normalized
            return normalized.lower()
        except Exception:
            return resource.strip().lower()

    @staticmethod
    def _normalize_email(resource: str) -> str:
        # parseaddr xử lý "Bob <bob@company.com>" → "bob@company.com"
        _, addr = parseaddr(resource)
        return addr.lower().strip() if addr else resource.lower().strip()

    @staticmethod
    def _normalize_url(resource: str) -> str:
        try:
            parsed = urlparse(resource.lower())
            # Loại bỏ trailing slash, fragment, normalize scheme
            normalized = urlunparse((
                parsed.scheme, parsed.netloc.rstrip("/"),
                parsed.path.rstrip("/"), "", "", ""
            ))
            return normalized
        except Exception:
            return resource.strip().lower()

    _NORMALIZERS = {
        "file":     _normalize_file.__func__,
        "email":    _normalize_email.__func__,
        "web":      _normalize_url.__func__,
        "calendar": str.strip,
        "contacts": str.lower,
        "data":     str.lower,
    }
```

Tích hợp vào `InMemoryFGAClient.check()` và `TupleManager.write_tuples()`:

```python
# Trong check():
normalized_resource = ResourceNormalizer.normalize(agent, resource)
# So sánh normalized_resource với stored tuples (cũng đã được normalize lúc write)

# Trong write_tuples():
cap_normalized = cap.model_copy(update={
    "resource": ResourceNormalizer.normalize(cap.agent, cap.resource)
})
```

---

#### Giải pháp B: Type-Safe Resource Wrappers

Thay vì dùng raw string, đóng gói resource vào type-safe wrapper có built-in normalization:

```python
from dataclasses import dataclass
from abc import ABC, abstractmethod

class Resource(ABC):
    @abstractmethod
    def canonical(self) -> str: ...

    def matches(self, other: "Resource") -> bool:
        if type(self) != type(other):
            return False
        if self.canonical() == "*":
            return True
        return self.canonical() == other.canonical()


@dataclass
class FileResource(Resource):
    path: str

    def canonical(self) -> str:
        if self.path == "*":
            return "*"
        return str(PurePosixPath(self.path)).lower()


@dataclass
class EmailResource(Resource):
    address: str

    def canonical(self) -> str:
        if self.address == "*":
            return "*"
        _, addr = parseaddr(self.address)
        return (addr or self.address).lower().strip()


# Factory
def make_resource(agent: str, raw: str) -> Resource:
    mapping = {
        "file":  FileResource,
        "email": EmailResource,
        "web":   UrlResource,
    }
    cls = mapping.get(agent, RawResource)
    return cls(raw)
```

Sửa `Capability.matches()` để dùng wrapper:
```python
class Capability(BaseModel):
    ...
    def matches(self, agent: str, tool: str, resource: str) -> bool:
        if self.agent != agent or (self.tool != tool and self.tool != "*"):
            return False
        r_cap  = make_resource(agent, self.resource)
        r_call = make_resource(agent, resource)
        return r_cap.matches(r_call)
```

---

#### Giải pháp C: Prefix Wildcard với Path Boundary Awareness

Wildcard `/docs/*` hiện tại match được cả `/docs_secret/file.pdf` (vì `startswith` đơn giản). Cần đảm bảo wildcard chỉ match đúng path boundary:

```python
def _matches_pattern(pattern: str, resource: str) -> bool:
    if pattern == "*":
        return True
    if not pattern.endswith("*"):
        return pattern == resource

    # Prefix wildcard: /docs/* → chỉ match /docs/<something>
    prefix = pattern[:-1]   # "/docs/"
    if not prefix.endswith("/"):
        prefix += "/"       # đảm bảo match boundary

    canonical_resource = resource if resource.endswith("/") else resource
    return (
        canonical_resource.startswith(prefix)
        or canonical_resource == prefix.rstrip("/")  # match /docs chính xác
    )
```

---

#### Giải pháp D: Blocklist Cho Nguy Hiểm Pattern Phổ Biến

Thêm một lớp kiểm tra trước normalization để phát hiện các kỹ thuật bypass đã biết:

```python
DANGEROUS_PATTERNS = [
    r"\.\./",          # path traversal
    r"%2e%2e",         # URL-encoded traversal
    r"\\\\",           # UNC path (Windows)
    r"\x00",           # null byte injection
    r"file://",        # file URI scheme (nếu resource không nên là URI)
    r";",              # command separator trong resource
]

def is_suspicious_resource(resource: str) -> bool:
    resource_lower = resource.lower()
    return any(
        re.search(pattern, resource_lower)
        for pattern in DANGEROUS_PATTERNS
    )

# Trong invoke_tool_with_auth:
if is_suspicious_resource(resource):
    logger.warning("Suspicious resource pattern: %s", resource)
    return ToolResult.deny_policy(agent, tool, resource)
```

---

## Tổng Kết: Roadmap Cải Tiến Theo Độ Ưu Tiên

| # | Hạn chế | Giải pháp ưu tiên | Độ phức tạp | Tác động bảo mật |
|---|---|---|---|---|
| 1 | Argument subfields | **A**: Capability Constraints | Trung bình | 🔴 Cao |
| 2 | Intent parser | **B**: Confidence Score + **C**: Rule Validation | Thấp | 🟠 Trung bình |
| 3 | Escalation fatigue | **A**: Batch + **B**: Risk-Tiered | Trung bình | 🟠 Trung bình |
| 4 | Contact Store | **C**: Immutable Snapshot + **A**: Multi-source | Thấp → Cao | 🔴 Cao |
| 5 | String matching | **A**: Normalizer Registry | Thấp | 🟡 Thấp–Trung |

**Thứ tự triển khai đề xuất:**

```
Sprint 1 (1 tuần):
  ✓ Hạn chế 5-A: ResourceNormalizer (thay đổi ít, impact cao)
  ✓ Hạn chế 4-C: FrozenContactStore (đã có sẵn một phần)
  ✓ Hạn chế 2-C: Rule-based CapabilityValidator

Sprint 2 (2 tuần):
  ✓ Hạn chế 3-A: BatchEscalationHandler
  ✓ Hạn chế 3-B: RiskAwareEscalationHandler
  ✓ Hạn chế 2-B: Confidence Score trong Capability schema

Sprint 3 (3 tuần):
  ✓ Hạn chế 1-A: Capability Constraints
  ✓ Hạn chế 4-A: Multi-source VerifiedContactStore
  ✓ Hạn chế 2-A: Dual-Pass Parsing
```

---

## Phân Tích Chi Tiết: Điểm Mạnh, Điểm Yếu Và Kế Hoạch Triển Khai

### Ma Trận Quyết Định — 20 Giải Pháp

| ID | Giải pháp | Trạng thái | Lý do |
|---|---|---|---|
| 1-A | Capability Constraints | ✅ Sprint 3 | Security impact cao, cần schema migration |
| 1-B | Multi-Resource Tuples | ⏸️ v2.0 | Phức tạp, phá vỡ FGA model hiện tại |
| 1-C | OutputSanitizer | ✅ Sprint 2 | Defensive layer, dễ thêm |
| 2-A | Dual-Pass Parsing | 🔵 Optional | Cần 2 LLM calls, cost tăng |
| 2-B | Confidence Score | ✅ Sprint 2 | Thêm field vào Capability, không breaking |
| 2-C | Rule-based Validator | ✅ Sprint 1 | Pure Python, zero overhead |
| 2-D | Regression Test Suite | ✅ Sprint 1 | Tests luôn cần trước khi code |
| 3-A | BatchEscalation | ✅ Sprint 2 | UX critical, logic rõ ràng |
| 3-B | RiskAwareEscalation | ✅ Sprint 2 | Kết hợp với 3-A |
| 3-C | CapabilityProfile | ✅ Sprint 3 | Cần 3-A/3-B hoàn chỉnh trước |
| 3-D | Context Enhancement | ✅ Sprint 1 | Cải thiện intent parsing ngay |
| 3-E | Adaptive Mode | ❌ Không triển khai | Exploitable: attacker học pattern để bypass |
| 4-A | MultiSourceContactStore | ✅ Sprint 3 | Cần thiết kế API trước |
| 4-B | Signing/HMAC | 🔵 Optional | Chỉ cần khi có multi-process env |
| 4-C | FrozenContactStore (Immutable Snapshot) | ✅ Sprint 1 | Đơn giản, security impact cao |
| 4-D | FirstUseGuard | ✅ Sprint 2 | Bổ trợ cho 4-C |
| 5-A | ResourceNormalizer | ✅ Sprint 1 | Dễ nhất, impact lớn nhất |
| 5-B | TypeSafeWrappers | ✅ Sprint 3 | Refactor có rủi ro, cần test coverage tốt trước |
| 5-C | Boundary Wildcard Validation | ✅ Sprint 1 | Cùng module với 5-A |
| 5-D | SuspiciousPatternDetector | ✅ Sprint 2 | Defense-in-depth layer |

---

### Phân Tích Mạnh/Yếu Từng Giải Pháp

#### Hạn Chế 1: Argument Subfields

| Giải pháp | Điểm mạnh | Điểm yếu |
|---|---|---|
| **1-A Capability Constraints** | Chặn injection vào nội dung (body, subject); FGA-native, không thêm runtime layer | Schema migration chạm vào 255 tests; LLM phải học format constraints mới |
| **1-B Multi-Resource Tuples** | Bảo vệ toàn diện nhất, mọi argument đều kiểm soát được | Làm nổ số tuple (O(n*m)), FGA check phức tạp; phá vỡ model tuple hiện tại |
| **1-C OutputSanitizer** | Chặn data exfiltration ở output layer; không cần thay đổi FGA | Không ngăn được injection xảy ra, chỉ giảm thiệt hại sau khi tool đã chạy |

#### Hạn Chế 2: Intent Parser

| Giải pháp | Điểm mạnh | Điểm yếu |
|---|---|---|
| **2-A Dual-Pass Parsing** | Độ chính xác cao nhất; lần 2 có thể cross-validate | +1 LLM call mỗi request → latency ~6s thêm; cost x2 parsing |
| **2-B Confidence Score** | Signal sớm về ambiguity; có thể threshold-gate escalation | LLM tự báo cáo confidence không reliable (thường overconfident) |
| **2-C Rule-based Validator** | Zero-cost, deterministic, không thể bị bypass bằng adversarial prompt | Chỉ phát hiện structural errors, không hiểu ngữ nghĩa |
| **2-D Regression Tests** | Ngăn regression mỗi khi thay đổi prompt; ground truth cho intent parser | Tốn công build corpus, cần update khi thêm agent |

#### Hạn Chế 3: Escalation Fatigue

| Giải pháp | Điểm mạnh | Điểm yếu |
|---|---|---|
| **3-A BatchEscalation** | Giảm số lần interrupt user từ N xuống 1; UX tốt hơn nhiều | Phải chờ collect đủ batch trước khi hỏi → latency |
| **3-B RiskAwareEscalation** | High-risk tools luôn hỏi; low-risk auto-approve → balance security/UX | Risk tier phải được maintain thủ công, có thể lỗi thời |
| **3-C CapabilityProfile** | Giảm escalation về 0 cho trusted workflows | Profile có thể bị stale; cần review mechanism |
| **3-D Context Enhancement** | Cải thiện intent parsing → ít tool bị deny hơn ngay từ đầu | Chỉ giảm false positives, không giải quyết genuine escalation |
| **3-E Adaptive Mode** | ❌ Attacker có thể thăm dò pattern để tìm threshold → bypass |

#### Hạn Chế 4: Contact Store

| Giải pháp | Điểm mạnh | Điểm yếu |
|---|---|---|
| **4-A MultiSourceContactStore** | Trust hierarchy rõ ràng; verified contacts từ nhiều nguồn | Phải implement adapter cho mỗi source (Outlook, Gmail, LDAP...) |
| **4-B Signing/HMAC** | Phát hiện tampering ngay cả khi process bị compromise | Overhead mỗi lookup; key management là vấn đề mới |
| **4-C FrozenContactStore** | Đơn giản nhất; đảm bảo consistency trong một request | Không bảo vệ nếu snapshot đã bị poison trước khi freeze |
| **4-D FirstUseGuard** | Cảnh báo khi LLM tự tạo contact không có trong original list | Có thể flag false positives với người dùng mới |

#### Hạn Chế 5: String Matching

| Giải pháp | Điểm mạnh | Điểm yếu |
|---|---|---|
| **5-A ResourceNormalizer** | Chặn path traversal variants (`../`, `//`, encoded); dễ unit test | Chỉ normalize, không validate context |
| **5-B TypeSafeWrappers** | Compile-time prevention; tự document resource format | Refactor lớn, chạm nhiều files; risk regression |
| **5-C Boundary Wildcard Validation** | Ngăn wildcard quá rộng (`*` → `data:*`); một hàm nhỏ | Chỉ giải quyết một attack vector cụ thể |
| **5-D SuspiciousPatternDetector** | Defense-in-depth; phát hiện pattern bất thường ở runtime | Rate of false positive cao nếu data có text phức tạp |

---

### Kế Hoạch 3 Sprint Chi Tiết

#### Sprint 1 — Nền Tảng Bảo Mật (1 tuần)

**Mục tiêu:** Các thay đổi nhỏ, impact cao, không cần thay đổi schema.

| Task | File | Effort | Verify |
|---|---|---|---|
| **5-A** `ResourceNormalizer` | `ibac/authorization/resource_normalizer.py` (mới) | 0.5 ngày | Unit test path traversal variants |
| **5-C** `validate_tuple_specificity()` | `ibac/authorization/tuple_manager.py` | 0.5 ngày | Test reject `*:*#*` tuples |
| **4-C** `FrozenContactStore` | `ibac/context/request_context.py` | 0.5 ngày | Test mutation sau freeze raises error |
| **3-D** Context Enhancement | `ibac/parser/intent_parser.py` (system prompt) | 0.5 ngày | Test parse với context examples |
| **2-C** `CapabilityValidator` | `ibac/parser/capability_validator.py` (mới) | 1 ngày | Test valid/invalid capability dicts |
| **2-D** Intent Parser Regression Tests | `ibac/tests/test_intent_parser_regression.py` | 1 ngày | 20+ corpus examples pass |

**Dependencies:** Không có — tất cả độc lập nhau.

**Rủi ro:** Thấp. Các thay đổi isolated, không chạm core FGA logic.

---

#### Sprint 2 — UX Và Defense-in-Depth (2 tuần)

**Mục tiêu:** Giảm escalation fatigue; thêm output-layer protection.

| Task | File | Effort | Verify |
|---|---|---|---|
| **3-A** `BatchEscalationHandler` | `ibac/escalation/batch_escalation.py` (mới) | 2 ngày | Test N denials → 1 prompt |
| **3-B** `RiskAwareEscalation` | `ibac/escalation/escalation_handler.py` | 1 ngày | Test high-risk tool luôn hỏi |
| **2-B** Confidence Score | `ibac/models/schemas.py`, `intent_parser.py` | 1 ngày | Schema migration, test low-confidence path |
| **1-C** `OutputSanitizer` | `ibac/executor/output_sanitizer.py` (mới) | 2 ngày | Test strip secrets khỏi tool output |
| **4-D** `FirstUseGuard` | `ibac/context/request_context.py` | 1 ngày | Test flag contact không có trong snapshot |
| **5-D** `SuspiciousPatternDetector` | `ibac/authorization/deny_policies.py` | 1 ngày | Test detect `../`, `.env`, `.ssh` patterns |

**Dependencies:** 3-B phụ thuộc 3-A; 4-D phụ thuộc 4-C từ Sprint 1.

**Rủi ro:** Trung bình. `OutputSanitizer` cần xác định rõ "secret pattern" để tránh false positive.

---

#### Sprint 3 — Schema Evolution (3 tuần)

**Mục tiêu:** Nâng cấp Capability schema; mở rộng ContactStore; TypeSafe refactor.

| Task | File | Effort | Verify |
|---|---|---|---|
| **5-B** TypeSafe Resource Wrappers | `ibac/models/resources.py` (mới), update tất cả tools | 3 ngày | Tất cả 255 tests + mới pass |
| **3-C** `CapabilityProfile` | `ibac/escalation/capability_profile.py` (mới) | 3 ngày | Test profile match → no escalation |
| **4-A** `MultiSourceContactStore` | `ibac/context/contact_store.py` (mới) | 4 ngày | Test merge từ 2+ sources với trust levels |
| **2-A** Dual-Pass Parsing | `ibac/parser/intent_parser.py` | 3 ngày | Test cross-validation catches inconsistency |
| **1-A** Capability Constraints | `ibac/models/schemas.py`, `tuple_manager.py`, `intent_parser.py` | 5 ngày | Test constraint violation → deny |

**Dependencies:**
```
1-A phụ thuộc 2-B (confidence) + 2-C (validator) từ Sprint 1/2
3-C phụ thuộc 3-A + 3-B từ Sprint 2
4-A phụ thuộc 4-C + 4-D từ Sprint 1/2
5-B phụ thuộc 2-D regression tests từ Sprint 1 (safety net)
```

**Rủi ro:** Cao. `1-A` thay đổi `Capability` schema → chạm intent parser prompt, tuple_manager, và 255 existing tests. Phải làm sau khi có regression test coverage tốt từ Sprint 1.

---

### Giải Pháp KHÔNG Triển Khai

| ID | Giải pháp | Lý do từ chối |
|---|---|---|
| **3-E** Adaptive Mode | Attacker có thể probe threshold bằng cách gửi nhiều request với phạm vi leo thang tăng dần để học pattern bypass |
| **1-B** Multi-Resource Tuples | Phá vỡ FGA data model hiện tại; độ phức tạp không tương xứng với lợi ích so với 1-A + 1-C |
| **4-B** Signing/HMAC | Chỉ có giá trị trong multi-process/distributed deployment; over-engineering cho single-process setup hiện tại |

---

### Tóm Tắt Rủi Ro Và Ưu Tiên

```
Rủi ro cao nhất: Sprint 3 / Task 1-A (Capability Constraints)
  → Schema change chạm 255 tests + intent parser prompt
  → Mitigation: Build regression corpus (2-D) trước ở Sprint 1

ROI cao nhất / effort thấp nhất:
  Sprint 1: 5-A (ResourceNormalizer) + 4-C (FrozenContactStore)
  → 2 ngày effort, đóng 2 attack vectors ngay lập tức

Tác động UX lớn nhất:
  Sprint 2: 3-A (BatchEscalation) + 3-B (RiskAwareEscalation)
  → Giảm interrupt user từ N lần xuống 1 lần mỗi request
```

**Tổng estimation:** ~7 tuần (1 + 2 + 3 tuần + buffer), 1 developer.  
**Kết quả kỳ vọng:** Security coverage từ ~85% lên ~97% trên injection scenarios; escalation fatigue giảm 70%+.

