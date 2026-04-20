"""
Unit tests cho Phase 5: InMemoryFGAClient + Deny Policies.

Chạy: pytest ibac/tests/test_fga_client.py -v
"""

import pytest
from pathlib import Path

from ibac.authorization.fga_client import InMemoryFGAClient, CheckResult
from ibac.authorization.deny_policies import (
    load_default_deny_policies,
    load_deny_policies_from_yaml,
    DEFAULT_POLICIES,
)
from ibac.authorization.tuple_manager import TupleManager
from ibac.models.schemas import AuthorizationTuple, Capability, DenyPolicy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fga():
    return InMemoryFGAClient()


@pytest.fixture
def fga_with_defaults():
    client = InMemoryFGAClient()
    load_default_deny_policies(client)
    return client


def _write_allow(fga: InMemoryFGAClient, request_id: str, agent: str, tool: str,
                 resource: str, created_turn: int = 0, ttl: int = 3) -> None:
    fga.write_allow(AuthorizationTuple(
        request_id=request_id, agent=agent, tool=tool,
        resource=resource, created_turn=created_turn, ttl=ttl,
    ))


# ---------------------------------------------------------------------------
# Tests: FGAStore Protocol (TupleManager compat)
# ---------------------------------------------------------------------------

class TestFGAStoreProtocol:
    def test_write_and_list(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com")
        tuples = fga.list_by_request("req_1")
        assert len(tuples) == 1
        assert tuples[0].agent == "email"

    def test_list_by_request_scoped(self, fga):
        _write_allow(fga, "req_A", "email", "send", "a@a.com")
        _write_allow(fga, "req_B", "file", "read", "/b.pdf")
        assert len(fga.list_by_request("req_A")) == 1
        assert len(fga.list_by_request("req_B")) == 1

    def test_list_all_with_wildcard(self, fga):
        _write_allow(fga, "req_A", "email", "send", "a@a.com")
        _write_allow(fga, "req_B", "file", "read", "/b.pdf")
        assert len(fga.list_by_request("*")) == 2

    def test_delete_allow(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com")
        fga.delete_allow("req_1", "email", "send", "bob@company.com")
        assert fga.list_by_request("req_1") == []

    def test_delete_nonexistent_is_safe(self, fga):
        fga.delete_allow("req_x", "email", "send", "nobody@nowhere.com")  # no exception

    def test_compatible_with_tuple_manager(self, fga):
        manager = TupleManager(fga, default_ttl=3)
        caps = [Capability(agent="email", tool="send", resource="bob@company.com", reasoning="test")]
        manager.write_tuples("req_1", caps, current_turn=0)
        assert len(fga.list_by_request("req_1")) == 1


# ---------------------------------------------------------------------------
# Tests: check() — 3 cases
# ---------------------------------------------------------------------------

class TestCheckAllowed:
    def test_allowed_when_tuple_exists(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com")
        result = fga.check("req_1", "email", "send", "bob@company.com", current_turn=0)
        assert result.allowed is True
        assert result.blocked is False

    def test_allowed_within_ttl(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com", created_turn=0, ttl=3)
        assert fga.check("req_1", "email", "send", "bob@company.com", current_turn=3).allowed is True

    def test_allowed_exact_ttl_boundary(self, fga):
        # delta = ttl → still valid
        _write_allow(fga, "req_1", "calendar", "read", "*", created_turn=2, ttl=2)
        assert fga.check("req_1", "calendar", "read", "*", current_turn=4).allowed is True


class TestCheckDeniedNotInIntent:
    def test_denied_no_tuple(self, fga):
        result = fga.check("req_1", "email", "send", "attacker@evil.com", current_turn=0)
        assert result.allowed is False
        assert result.blocked is False  # có thể escalate

    def test_denied_wrong_resource(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com")
        result = fga.check("req_1", "email", "send", "attacker@evil.com", current_turn=0)
        assert result.allowed is False
        assert result.blocked is False

    def test_denied_wrong_agent(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com")
        result = fga.check("req_1", "calendar", "send", "bob@company.com", current_turn=0)
        assert result.allowed is False

    def test_denied_expired_ttl(self, fga):
        _write_allow(fga, "req_1", "email", "send", "bob@company.com", created_turn=0, ttl=3)
        result = fga.check("req_1", "email", "send", "bob@company.com", current_turn=4)
        assert result.allowed is False
        assert result.blocked is False  # expired ≠ blocked, vẫn có thể escalate

    def test_denied_different_request(self, fga):
        _write_allow(fga, "req_A", "email", "send", "bob@company.com")
        result = fga.check("req_B", "email", "send", "bob@company.com", current_turn=0)
        assert result.allowed is False


class TestCheckBlocked:
    def test_blocked_by_deny_policy(self, fga):
        fga.add_deny_policy(DenyPolicy(agent="shell", tool="exec", resource="*", reason="test"))
        result = fga.check("req_1", "shell", "exec", "rm", current_turn=0)
        assert result.allowed is False
        assert result.blocked is True

    def test_blocked_overrides_allow_tuple(self, fga):
        """Deny policy phải thắng ngay cả khi có allow tuple — đây là tính chất quan trọng nhất."""
        fga.add_deny_policy(DenyPolicy(agent="shell", tool="exec", resource="*", reason="test"))
        _write_allow(fga, "req_1", "shell", "exec", "rm")  # có allow tuple
        result = fga.check("req_1", "shell", "exec", "rm", current_turn=0)
        assert result.allowed is False
        assert result.blocked is True

    def test_blocked_prefix_wildcard_etc(self, fga):
        fga.add_deny_policy(DenyPolicy(agent="*", tool="*", resource="/etc/*", reason="sys files"))
        assert fga.check("req_1", "file", "read", "/etc/passwd", current_turn=0).blocked is True
        assert fga.check("req_1", "file", "read", "/etc/shadow", current_turn=0).blocked is True
        assert fga.check("req_1", "file", "read", "/home/user/file", current_turn=0).blocked is False

    def test_blocked_ssh_keys(self, fga):
        fga.add_deny_policy(DenyPolicy(agent="*", tool="*", resource="~/.ssh/*", reason="ssh"))
        assert fga.check("req_1", "file", "read", "~/.ssh/id_rsa", current_turn=0).blocked is True
        assert fga.check("req_1", "file", "read", "~/.bashrc", current_turn=0).blocked is False


# ---------------------------------------------------------------------------
# Tests: Deny Policies
# ---------------------------------------------------------------------------

class TestDefaultDenyPolicies:
    def test_load_default_policies(self, fga):
        count = load_default_deny_policies(fga)
        assert count == len(DEFAULT_POLICIES)
        assert len(fga.list_deny_policies()) == len(DEFAULT_POLICIES)

    def test_shell_exec_blocked(self, fga_with_defaults):
        result = fga_with_defaults.check("req_1", "shell", "exec", "rm -rf /", current_turn=0)
        assert result.blocked is True

    def test_etc_passwd_blocked(self, fga_with_defaults):
        result = fga_with_defaults.check("req_1", "file", "read", "/etc/passwd", current_turn=0)
        assert result.blocked is True

    def test_ssh_key_blocked(self, fga_with_defaults):
        result = fga_with_defaults.check("req_1", "file", "read", "~/.ssh/id_rsa", current_turn=0)
        assert result.blocked is True

    def test_env_file_blocked(self, fga_with_defaults):
        result = fga_with_defaults.check("req_1", "file", "read", "~/.env", current_turn=0)
        assert result.blocked is True

    def test_root_delete_blocked(self, fga_with_defaults):
        result = fga_with_defaults.check("req_1", "file", "delete", "/important.conf", current_turn=0)
        assert result.blocked is True

    def test_normal_email_not_blocked(self, fga_with_defaults):
        _write_allow(fga_with_defaults, "req_1", "email", "send", "bob@company.com")
        result = fga_with_defaults.check("req_1", "email", "send", "bob@company.com", current_turn=0)
        assert result.allowed is True
        assert result.blocked is False

    def test_normal_file_read_not_blocked(self, fga_with_defaults):
        _write_allow(fga_with_defaults, "req_1", "file", "read", "/docs/report.pdf")
        result = fga_with_defaults.check("req_1", "file", "read", "/docs/report.pdf", current_turn=0)
        assert result.allowed is True


class TestLoadDenyPoliciesFromYaml:
    def test_load_from_yaml(self, tmp_path, fga):
        config = tmp_path / "policies.yaml"
        config.write_text(
            "deny_policies:\n"
            "  - agent: '*'\n"
            "    tool: '*'\n"
            "    resource: '/secrets/*'\n"
            "    reason: 'Cấm đọc secrets'\n"
        )
        count = load_deny_policies_from_yaml(fga, str(config))
        assert count == 1
        result = fga.check("req_1", "file", "read", "/secrets/key.pem", current_turn=0)
        assert result.blocked is True

    def test_load_file_not_found(self, fga):
        with pytest.raises(FileNotFoundError):
            load_deny_policies_from_yaml(fga, "/nonexistent/policies.yaml")

    def test_empty_yaml_loads_zero(self, tmp_path, fga):
        config = tmp_path / "empty.yaml"
        config.write_text("deny_policies: []\n")
        count = load_deny_policies_from_yaml(fga, str(config))
        assert count == 0


# ---------------------------------------------------------------------------
# Tests: CheckResult repr
# ---------------------------------------------------------------------------

class TestCheckResult:
    def test_repr_allowed(self):
        assert "ALLOWED" in repr(CheckResult(allowed=True, blocked=False))

    def test_repr_blocked(self):
        assert "BLOCKED" in repr(CheckResult(allowed=False, blocked=True))

    def test_repr_denied(self):
        assert "DENIED" in repr(CheckResult(allowed=False, blocked=False))


# ---------------------------------------------------------------------------
# Tests: Scenario từ bài báo
# ---------------------------------------------------------------------------

class TestPaperScenarios:
    def test_email_to_bob_allowed(self, fga_with_defaults):
        """(user:req_abc, can_invoke, email:send#bob@company.com) → allowed: true"""
        _write_allow(fga_with_defaults, "req_abc", "email", "send", "bob@company.com")
        result = fga_with_defaults.check("req_abc", "email", "send", "bob@company.com", current_turn=0)
        assert result.allowed is True

    def test_email_to_attacker_denied(self, fga_with_defaults):
        """(user:req_abc, can_invoke, email:send#attacker@evil.com) → allowed: false (no tuple)"""
        _write_allow(fga_with_defaults, "req_abc", "email", "send", "bob@company.com")
        result = fga_with_defaults.check("req_abc", "email", "send", "attacker@evil.com", current_turn=0)
        assert result.allowed is False
        assert result.blocked is False  # canEscalate=True

    def test_shell_exec_hard_denied(self, fga_with_defaults):
        """(user:req_abc, can_invoke, shell:exec#rm) → allowed: false (blocked)"""
        result = fga_with_defaults.check("req_abc", "shell", "exec", "rm", current_turn=0)
        assert result.allowed is False
        assert result.blocked is True  # canEscalate=False
