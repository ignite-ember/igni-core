"""How MCP first-use approval answers under a layer's policy, and what it makes of a bad file.

A layer (the cloud's, say) can keep approvals somewhere else: then its
:class:`McpApprovalPolicy` answers, nobody is prompted and the desktop's
``mcp-approved.json`` is neither read nor written. Without one, the file
is the record, and a file that is damaged is an empty record, never a
crash and never an approval nobody gave.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from igni_core.core.layer import Layer, layers
from igni_core.core.mcp.approval import MCPApprovalManager, McpApprovalPolicy

PROJECT = "/home/u/repo/.mcp.json"


class _Policy(McpApprovalPolicy):
    def __init__(self, approved: set[tuple[str, str]] | None = None) -> None:
        self.approved = approved or set()
        self.asked: list[tuple[str, str]] = []

    def is_approved(self, server_name: str, config_path: str) -> bool:
        self.asked.append((server_name, config_path))
        return (server_name, config_path) in self.approved

    def approve(self, server_name: str, config_path: str) -> None:
        self.approved.add((server_name, config_path))


@pytest.fixture(autouse=True)
def _restore_layer():
    yield
    layers.reset()


def test_a_policy_answers_instead_of_the_file_and_never_touches_it(tmp_path: Path) -> None:
    path = tmp_path / "approved.json"
    path.write_text(json.dumps({"srv": [PROJECT]}))
    policy = _Policy()
    mgr = MCPApprovalManager(approval_path=path, policy=policy)

    # The file says yes; the policy is who answers, and says no.
    assert mgr.is_approved("srv", PROJECT) is False

    mgr.approve("srv", PROJECT)
    assert policy.approved == {("srv", PROJECT)}
    assert mgr.is_approved("srv", PROJECT) is True
    # The file is exactly as it was: nothing read into memory, nothing written.
    assert json.loads(path.read_text()) == {"srv": [PROJECT]}


def test_a_policy_writes_no_file_when_there_was_none(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "approved.json"
    mgr = MCPApprovalManager(approval_path=path, policy=_Policy())

    mgr.approve("srv", PROJECT)

    assert not path.exists()
    assert not path.parent.exists()


def test_a_policy_decides_the_user_global_config_too(tmp_path: Path) -> None:
    from igni_core.core.mcp.approval import _USER_GLOBAL_MCP

    mgr = MCPApprovalManager(approval_path=tmp_path / "a.json", policy=_Policy())

    assert mgr.is_approved("srv", _USER_GLOBAL_MCP) is False


def test_a_refused_server_is_logged_with_the_policys_words_and_nobody_is_asked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Worded(_Policy):
        def refusal(self, server_name: str, config_path: str) -> str:
            return f"ask your admin about {server_name}"

    mgr = MCPApprovalManager(approval_path=tmp_path / "a.json", policy=Worded())

    with (
        patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: True)),
        patch("igni_core.core.mcp.approval.Confirm.ask") as ask,
        caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.approval"),
    ):
        assert mgr.check_approval("srv", PROJECT) is False

    ask.assert_not_called()
    assert "ask your admin about srv" in caplog.text


def test_an_approved_server_passes_a_policy_check_without_a_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mgr = MCPApprovalManager(approval_path=tmp_path / "a.json", policy=_Policy({("srv", PROJECT)}))

    with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.approval"):
        assert mgr.check_approval("srv", PROJECT) is True

    assert caplog.text == ""


def test_the_default_refusal_names_the_server_and_the_file() -> None:
    assert _Policy().refusal("srv", PROJECT) == (
        f"MCP 'srv' from {PROJECT} is not approved — not connecting."
    )


def test_the_layers_policy_is_used_when_none_is_passed(tmp_path: Path) -> None:
    policy = _Policy({("srv", PROJECT)})

    class _Cloud(Layer):
        def mcp_approvals(self) -> McpApprovalPolicy:
            return policy

    layers.install(_Cloud())
    mgr = MCPApprovalManager(approval_path=tmp_path / "a.json")

    assert mgr.is_approved("srv", PROJECT) is True
    assert mgr.is_approved("srv", "/elsewhere/.mcp.json") is False


def test_without_a_policy_the_file_is_the_record(tmp_path: Path) -> None:
    layers.install(Layer())
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"srv": [PROJECT]}))

    assert MCPApprovalManager(approval_path=path).is_approved("srv", PROJECT) is True


def test_an_unwritable_record_is_logged_and_the_approval_holds_in_memory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    layers.install(Layer())
    mgr = MCPApprovalManager(approval_path=blocker / "approved.json")

    with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.approval"):
        mgr.approve("srv", PROJECT)

    assert "Failed to save MCP approvals" in caplog.text
    assert mgr.is_approved("srv", PROJECT) is True
    assert (
        MCPApprovalManager(approval_path=blocker / "approved.json").is_approved("srv", PROJECT)
        is False
    )


@pytest.mark.parametrize(
    "content",
    [
        json.dumps([PROJECT]),
        json.dumps("approved"),
        "null",
        "{not json",
        "",
    ],
)
def test_a_record_that_is_not_an_object_approves_nothing(tmp_path: Path, content: str) -> None:
    layers.install(Layer())
    path = tmp_path / "a.json"
    path.write_text(content)
    mgr = MCPApprovalManager(approval_path=path)

    assert mgr.is_approved("srv", PROJECT) is False
    mgr.approve("srv", PROJECT)
    assert json.loads(path.read_text()) == {"srv": [PROJECT]}


def test_a_record_that_is_not_utf8_approves_nothing_and_does_not_raise(tmp_path: Path) -> None:
    layers.install(Layer())
    path = tmp_path / "a.json"
    path.write_bytes(b'{"srv": ["\xff\xfe"]}')

    mgr = MCPApprovalManager(approval_path=path)

    assert mgr.is_approved("srv", PROJECT) is False


def test_a_record_with_malformed_entries_keeps_only_the_paths_it_can_trust(
    tmp_path: Path,
) -> None:
    layers.install(Layer())
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps({"scalar": PROJECT, "mixed": [PROJECT, 3, None, "/other"], "ok": ["/a"]})
    )
    mgr = MCPApprovalManager(approval_path=path)

    # A server mapped to a string must not turn a substring into an approval.
    assert mgr.is_approved("scalar", PROJECT) is False
    assert mgr.is_approved("scalar", "/home") is False
    assert mgr.is_approved("mixed", PROJECT) is True
    assert mgr.is_approved("mixed", "/other") is True
    assert mgr.is_approved("ok", "/a") is True

    # And approving on top of one still works rather than raising.
    mgr.approve("scalar", PROJECT)
    assert json.loads(path.read_text())["scalar"] == [PROJECT]


def test_the_user_global_path_is_recognised_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from igni_core.core.mcp import approval

    real = tmp_path / "home" / ".mcp.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    monkeypatch.setattr(approval, "_USER_GLOBAL_MCP", str(real))
    layers.install(Layer())
    mgr = MCPApprovalManager(approval_path=tmp_path / "a.json")

    assert mgr.is_approved("srv", str(link)) is True
    assert mgr.is_approved("srv", str(tmp_path / "other.json")) is False


def test_an_unresolvable_path_falls_back_to_comparing_the_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from igni_core.core.mcp import approval

    monkeypatch.setattr(approval, "_USER_GLOBAL_MCP", "/home/u/.igni/.mcp.json")

    def boom(self: Path, strict: bool = False) -> Path:
        raise OSError("loop")

    monkeypatch.setattr(Path, "resolve", boom)

    assert MCPApprovalManager._is_user_global("/home/u/.igni/.mcp.json") is True
    assert MCPApprovalManager._is_user_global("/home/u/repo/.mcp.json") is False


def test_a_missing_terminal_warning_names_the_file_to_edit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    layers.install(Layer())
    record = tmp_path / "mcp-approved.json"
    mgr = MCPApprovalManager(approval_path=record)

    with (
        patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: False)),
        caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.approval"),
    ):
        assert mgr.check_approval("srv", PROJECT) is False

    assert "'srv'" in caplog.text
    assert PROJECT in caplog.text
    assert str(record) in caplog.text
    assert not record.exists()


def test_the_prompt_names_the_server_and_its_source_and_defaults_to_no(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layers.install(Layer())
    mgr = MCPApprovalManager(approval_path=tmp_path / "a.json")

    with (
        patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: True)),
        patch("igni_core.core.mcp.approval.Confirm.ask", return_value=False) as ask,
    ):
        mgr.check_approval("srv[bold]", PROJECT)

    shown = capsys.readouterr().err
    assert "srv" in shown
    assert "wants to connect" in shown
    assert PROJECT in shown
    assert ask.call_args.kwargs["default"] is False
    assert ask.call_args.args[0] == "  Allow this MCP server?"
