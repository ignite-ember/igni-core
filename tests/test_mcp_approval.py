"""Tests for mcp/approval.py — MCP first-use approval manager."""

import json
from unittest.mock import patch

from igni_core.core.mcp.approval import _USER_GLOBAL_MCP, MCPApprovalManager


class TestMCPApprovalManager:
    """Unit tests for MCPApprovalManager."""

    def test_project_mcp_json_is_not_auto_approved(self, tmp_path):
        """A project .mcp.json is untrusted content — it must not skip the gate.

        Was the opposite assertion: the manager returned True for any
        path named ``.mcp.json``, so cloning a repo and sending one
        message spawned whatever command it declared.
        """
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        assert mgr.is_approved("my-server", "/project/.mcp.json") is False

    def test_non_standard_config_is_not_approved(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        assert mgr.is_approved("my-server", "/project/custom-mcp.json") is False

    def test_approve_persists(self, tmp_path):
        path = tmp_path / "approved.json"
        mgr = MCPApprovalManager(approval_path=path)

        mgr.approve("my-server", "/project/.mcp.json")

        assert mgr.is_approved("my-server", "/project/.mcp.json") is True
        # Verify the file was written
        data = json.loads(path.read_text())
        assert "/project/.mcp.json" in data["my-server"]

    def test_approve_idempotent(self, tmp_path):
        path = tmp_path / "approved.json"
        mgr = MCPApprovalManager(approval_path=path)

        mgr.approve("s", "/a")
        mgr.approve("s", "/a")

        data = json.loads(path.read_text())
        assert data["s"].count("/a") == 1

    def test_user_global_is_auto_approved(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        assert mgr.is_approved("any-server", _USER_GLOBAL_MCP) is True

    def test_different_source_not_approved(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        mgr.approve("s", "/project-a/custom-servers.json")

        assert mgr.is_approved("s", "/project-a/custom-servers.json") is True
        assert mgr.is_approved("s", "/project-b/custom-servers.json") is False

    def test_loads_existing_approvals(self, tmp_path):
        path = tmp_path / "approved.json"
        path.write_text(json.dumps({"cached-server": ["/old/.mcp.json"]}))

        mgr = MCPApprovalManager(approval_path=path)
        assert mgr.is_approved("cached-server", "/old/.mcp.json") is True

    def test_handles_corrupt_file(self, tmp_path):
        path = tmp_path / "approved.json"
        path.write_text("not json{{{")

        mgr = MCPApprovalManager(approval_path=path)
        assert mgr.is_approved("x", "/y") is False

    def test_handles_missing_file(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "nonexistent.json")
        assert mgr.is_approved("x", "/y") is False

    def test_check_approval_returns_true_when_already_approved(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        mgr.approve("s", "/p")
        # Should return True without prompting
        assert mgr.check_approval("s", "/p") is True

    def test_check_approval_returns_true_for_user_global(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        assert mgr.check_approval("s", _USER_GLOBAL_MCP) is True

    def test_check_approval_prompts_and_approves(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")

        # pytest's stdin is not a tty, and the manager refuses to prompt
        # without one — force the interactive branch.
        with (
            patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: True)),
            patch("igni_core.core.mcp.approval.Confirm.ask", return_value=True),
        ):
            result = mgr.check_approval("new-server", "/project/custom-mcp.json")

        assert result is True
        assert mgr.is_approved("new-server", "/project/custom-mcp.json") is True

    def test_check_approval_prompts_and_denies(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")

        with (
            patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: True)),
            patch("igni_core.core.mcp.approval.Confirm.ask", return_value=False),
        ):
            result = mgr.check_approval("new-server", "/project/custom-mcp.json")

        assert result is False
        assert mgr.is_approved("new-server", "/project/custom-mcp.json") is False

    def test_creates_parent_dirs_on_save(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "approved.json"
        mgr = MCPApprovalManager(approval_path=path)

        mgr.approve("s", "/p")

        assert path.exists()
        data = json.loads(path.read_text())
        assert "s" in data


class TestApprovalsGivenElsewhereAndWithdrawn:
    """Campaign R669 / R209: the approval file was read once, at start, so an
    approval given from igni's window or another session did not count until
    a restart; and nothing could withdraw one."""

    def test_an_approval_written_by_another_manager_counts_at_once(self, tmp_path):
        path = tmp_path / "approved.json"
        session_a = MCPApprovalManager(approval_path=path)
        session_b = MCPApprovalManager(approval_path=path)
        assert session_a.is_approved("s", "/p/.mcp.json") is False

        session_b.approve("s", "/p/.mcp.json")

        assert session_a.is_approved("s", "/p/.mcp.json") is True

    def test_two_managers_approving_keep_each_other_s_approvals(self, tmp_path):
        path = tmp_path / "approved.json"
        session_a = MCPApprovalManager(approval_path=path)
        session_b = MCPApprovalManager(approval_path=path)

        session_a.approve("one", "/p/.mcp.json")
        session_b.approve("two", "/p/.mcp.json")

        assert json.loads(path.read_text()) == {"one": ["/p/.mcp.json"], "two": ["/p/.mcp.json"]}

    def test_a_withdrawn_approval_no_longer_counts_anywhere(self, tmp_path):
        path = tmp_path / "approved.json"
        session_a = MCPApprovalManager(approval_path=path)
        session_b = MCPApprovalManager(approval_path=path)
        session_a.approve("s", "/p/.mcp.json")
        assert session_b.is_approved("s", "/p/.mcp.json") is True

        assert session_a.revoke("s", "/p/.mcp.json") is True

        assert session_b.is_approved("s", "/p/.mcp.json") is False
        assert json.loads(path.read_text()) == {}
        # Withdrawing what is not approved is harmless.
        assert session_a.revoke("s", "/p/.mcp.json") is True

    def test_the_user_global_config_cannot_be_withdrawn(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")

        assert mgr.revoke("any", _USER_GLOBAL_MCP) is False
        assert mgr.is_approved("any", _USER_GLOBAL_MCP) is True

    def test_without_a_terminal_it_still_fails_closed(self, tmp_path):
        mgr = MCPApprovalManager(approval_path=tmp_path / "approved.json")
        with patch.object(MCPApprovalManager, "_can_prompt", return_value=False):
            assert mgr.check_approval("s", "/p/.mcp.json") is False
