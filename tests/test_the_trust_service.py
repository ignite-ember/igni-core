"""The trust service itself: the instance, the answers, and who asks it.

The service is one object, handed to whoever needs it
(``core/workspace_trust``). Three pieces make that work: the holder that
owns the process's instance, the gate a consumer asks, and the run-scoped
answers that last for one process only. This file covers those, the order
the service reaches its answer in, the loaders that take a service instead
of reaching for the process's, and the double the rest of the suite runs
on.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from igni_core.core.config.tool_permissions.settings_files import SettingsFileLoader
from igni_core.core.config.tool_permissions.store import ToolPermissions
from igni_core.core.hooks.loader import HookLoader
from igni_core.core.lsp.loader import load_lsp_config
from igni_core.core.plugins.loader import PluginLoader
from igni_core.core.utils.markdown_commands import MarkdownCommand
from igni_core.core.workspace_trust import (
    RunTrust,
    TrustDecision,
    TrustGate,
    TrustRefused,
    TrustServiceHolder,
    WorkspaceIdentity,
    WorkspaceTrust,
    WorkspaceTrustStore,
    process_trust,
    trust_service,
)
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig
from tests.workspace_trust_doubles import TrustEverything

pytestmark = pytest.mark.workspace_trust


# ── helpers ────────────────────────────────────────────────────────


def _service(tmp_path: Path) -> WorkspaceTrust:
    """A trust service with its own store — no decision of another test's."""
    return WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))


def _folder(tmp_path: Path, name: str = "folder") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ── the holder ─────────────────────────────────────────────────────


def test_the_holder_keeps_the_service_it_made():
    """One holder, one service: the second caller gets the first one's."""
    holder = TrustServiceHolder()

    first = holder.current()

    assert isinstance(first, WorkspaceTrust)
    assert holder.current() is first


def test_replacing_the_service_puts_another_one_in_place():
    holder = TrustServiceHolder()
    other = WorkspaceTrust()

    holder.replace(other)

    assert holder.current() is other


def test_clearing_the_service_leaves_the_next_caller_a_new_one():
    """A cleared holder is a new process: the next caller makes its own."""
    holder = TrustServiceHolder()
    first = holder.current()

    holder.replace(None)

    assert holder.current() is not first


def test_the_process_service_is_the_holders():
    mine = WorkspaceTrust()
    trust_service.replace(mine)

    assert process_trust() is mine


# ── the gate ───────────────────────────────────────────────────────


def test_the_gate_asks_the_service_it_was_given(tmp_path):
    mine = _service(tmp_path)

    gate = TrustGate(mine)

    assert gate.service() is mine


def test_the_gate_falls_back_to_the_process_s_service():
    mine = WorkspaceTrust()
    trust_service.replace(mine)

    assert TrustGate().service() is mine


def test_the_gate_reads_the_process_s_service_each_time():
    """The gate does not capture the process's service when it is built:
    a service put in place later is the one that answers."""
    gate = TrustGate()
    first = gate.service()
    later = WorkspaceTrust()
    trust_service.replace(later)

    assert gate.service() is later
    assert gate.service() is not first


def test_the_gate_asks_whether_the_folder_may_run_its_own_code(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    gate = TrustGate(service)

    assert gate.is_trusted(folder) is False
    service.trust(folder)
    assert gate.is_trusted(folder) is True


# ── the run-scoped answers ─────────────────────────────────────────


def test_a_run_grant_is_not_a_recorded_decision(tmp_path):
    """A "this time only" answer lasts for this process, and leaves no trace."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.trust(folder, remember=False)

    assert service.is_trusted(folder)
    assert service.store.get(WorkspaceIdentity.of(folder)) is None


def test_a_run_refusal_is_not_a_recorded_decision(tmp_path):
    """A "not this time" answer lasts for this process, and leaves no trace."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.distrust(folder, remember=False)

    assert not service.is_trusted(folder)
    assert service.store.get(WorkspaceIdentity.of(folder)) is None


def test_the_two_run_answers_replace_each_other(tmp_path):
    """The newest answer for a folder is the one that counts."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.trust(folder, remember=False)
    service.distrust(folder, remember=False)

    decision = service.decide(folder)
    assert decision.state == "untrusted"
    assert decision.source == "run"

    service.trust(folder, remember=False)

    assert service.is_trusted(folder)


def test_recording_a_decision_clears_the_run_answer(tmp_path):
    """A remembered answer is the answer; the run's answer steps aside."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.trust(folder, remember=False)
    service.distrust(folder, remember=True)

    assert not service.is_trusted(folder)
    assert service.store.get(WorkspaceIdentity.of(folder)) == "untrusted"

    service.trust(folder, remember=True)
    service.distrust(folder, remember=False)

    assert not service.is_trusted(folder)
    assert service.store.get(WorkspaceIdentity.of(folder)) == "trusted"


def test_revoking_forgets_the_recorded_decision_and_the_run_answer(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.trust(folder, remember=True)
    service.distrust(folder, remember=False)

    decision = service.revoke(folder)

    assert decision.state == "ask"
    assert decision.source == "unset"
    assert service.store.get(WorkspaceIdentity.of(folder)) is None


def test_the_run_answers_are_mutually_exclusive():
    """The value object behind them: one key, one answer."""
    run = RunTrust()

    run.grant("a")
    assert run.granted("a")
    assert not run.refused("a")
    assert run.answer("a") == "trusted"

    run.refuse("a")
    assert run.refused("a")
    assert not run.granted("a")
    assert run.answer("a") == "untrusted"

    run.grant("a")
    assert run.granted("a")
    assert not run.refused("a")
    assert run.answer("a") == "trusted"

    run.forget("a")
    assert not run.granted("a")
    assert not run.refused("a")
    assert run.answer("a") is None


def test_a_remembered_answer_replaces_the_run_answer(tmp_path):
    """A decision written down is the answer; the run's answer steps aside."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.trust(folder, remember=False)
    assert service.decide(folder).source == "run"

    service.trust(folder, remember=True)

    decision = service.decide(folder)
    assert decision.source == "store"
    assert decision.state == "trusted"


# ── the order of the answers ───────────────────────────────────────


def test_the_policy_outranks_a_run_grant(tmp_path):
    """An organisation's rule is not a default the person may override."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    service.trust(folder, remember=False)

    service.set_policy(WorkspaceTrustConfig(mode="never"))

    decision = service.decide(folder)
    assert decision.state == "untrusted"
    assert decision.source == "policy"
    assert not decision.can_trust


def test_trusting_a_folder_the_policy_forbids_is_refused(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    service.set_policy(WorkspaceTrustConfig(mode="never"))

    with pytest.raises(TrustRefused):
        service.trust(folder, remember=False)

    with pytest.raises(TrustRefused):
        service.trust(folder)


def test_a_run_grant_outranks_a_recorded_refusal(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    service.distrust(folder, remember=True)

    service.trust(folder, remember=False)

    decision = service.decide(folder)
    assert decision.state == "trusted"
    assert decision.source == "run"


def test_a_run_refusal_outranks_a_recorded_trust(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    service.trust(folder, remember=True)

    service.distrust(folder, remember=False)

    decision = service.decide(folder)
    assert decision.state == "untrusted"
    assert decision.source == "run"


def test_the_recorded_decision_is_the_answer_when_nothing_else_is(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    service.trust(folder, remember=True)

    decision = service.decide(folder)
    assert decision.state == "trusted"
    assert decision.source == "store"


def test_an_undecided_folder_asks(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    decision = service.decide(folder)

    assert decision.state == "ask"
    assert decision.source == "unset"
    assert decision.needs_answer


# ── the double the suite runs on ───────────────────────────────────


def test_the_double_trusts_a_folder_nobody_decided_about(tmp_path):
    double = TrustEverything(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))
    folder = _folder(tmp_path)

    decision = double.decide(folder)

    assert decision.trusted
    assert decision.source == "run"


def test_the_double_still_honours_the_policy(tmp_path):
    """The seam the whole suite rests on: it trusts what nobody decided
    about, and nothing else."""
    double = TrustEverything(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))
    folder = _folder(tmp_path)
    double.set_policy(WorkspaceTrustConfig(mode="never"))

    assert not double.decide(folder).trusted


def test_the_double_still_honours_a_recorded_refusal(tmp_path):
    double = TrustEverything(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))
    folder = _folder(tmp_path)

    double.distrust(folder, remember=True)

    assert not double.decide(folder).trusted


def test_the_double_still_honours_a_run_refusal(tmp_path):
    double = TrustEverything(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))
    folder = _folder(tmp_path)

    double.distrust(folder, remember=False)

    assert not double.decide(folder).trusted


# ── change notification ────────────────────────────────────────────


def test_a_listener_is_told_when_a_decision_changes(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    told: list[TrustDecision] = []

    service.subscribe(told.append)
    service.trust(folder)

    assert [decision.state for decision in told] == ["trusted"]
    assert told[0].identity == WorkspaceIdentity.of(folder)


def test_an_unsubscribed_listener_is_not_told_again(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    told: list[TrustDecision] = []

    unsubscribe = service.subscribe(told.append)
    unsubscribe()
    service.trust(folder)

    assert told == []
    # Unsubscribing again says the same thing.
    unsubscribe()


def test_a_listener_that_fails_does_not_undo_the_decision(tmp_path):
    """A listener is a bystander: it cannot take the decision back."""
    service = _service(tmp_path)
    folder = _folder(tmp_path)

    def fail(_decision: TrustDecision) -> None:
        raise RuntimeError("the listener failed")

    service.subscribe(fail)

    decision = service.trust(folder)

    assert decision.state == "trusted"
    assert service.is_trusted(folder)


# ── the loaders take the service they are given ────────────────────


def test_the_hooks_loader_asks_the_service_it_was_given(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    _write(
        folder / ".igni" / "settings.json",
        json.dumps({"hooks": {"SessionStart": [{"type": "command", "command": "echo hi"}]}}),
    )

    untrusted = HookLoader(folder, cross_tool_support=True, trust=service).load().registry.raw
    assert untrusted == {}

    service.trust(folder)

    trusted = HookLoader(folder, cross_tool_support=True, trust=service).load().registry.raw
    assert trusted["SessionStart"][0].command == "echo hi"


def test_the_hooks_loader_falls_back_to_the_process_s_service(tmp_path):
    """A loader given no service of its own asks the process's."""
    service = _service(tmp_path)
    trust_service.replace(service)
    folder = _folder(tmp_path)
    _write(
        folder / ".igni" / "settings.json",
        json.dumps({"hooks": {"SessionStart": [{"type": "command", "command": "echo hi"}]}}),
    )

    assert HookLoader(folder, cross_tool_support=True).load().registry.raw == {}

    service.trust(folder)

    assert HookLoader(folder, cross_tool_support=True).load().registry.raw["SessionStart"]


def test_the_language_server_loader_asks_the_service_it_was_given(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    _write(
        folder / ".lsp.json",
        json.dumps({"lspServers": {"mine": {"command": "true"}}}),
    )

    assert "mine" not in load_lsp_config(folder, trust=service)

    service.trust(folder)

    assert "mine" in load_lsp_config(folder, trust=service)


def test_the_permission_rules_loader_asks_the_service_it_was_given(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    _write(folder / ".igni" / "settings.json", json.dumps({"permissions": {"allow": ["Bash"]}}))

    project_settings = folder / ".igni" / "settings.json"

    loader = SettingsFileLoader(project_dir=folder, trust=service)
    assert project_settings not in loader.paths()

    service.trust(folder)

    loader = SettingsFileLoader(project_dir=folder, trust=service)
    assert project_settings in loader.paths()
    assert ToolPermissions(project_dir=folder, trust=service).get_level("Bash") == "allow"


def test_the_plugin_loader_asks_the_service_it_was_given(tmp_path):
    service = _service(tmp_path)
    folder = _folder(tmp_path)
    _write(
        folder / ".igni" / "plugins" / "p" / ".claude-plugin" / "plugin.json",
        json.dumps({"name": "p"}),
    )

    loader = PluginLoader(data_dir=tmp_path / "data", trust=service)
    loader.load_all(folder)
    assert loader.list_plugins() == []

    service.trust(folder)

    loader = PluginLoader(data_dir=tmp_path / "data", trust=service)
    loader.load_all(folder)
    assert [plugin.name for plugin in loader.list_plugins()] == ["p"]


def test_a_markdown_command_shell_token_follows_the_process_s_service(tmp_path):
    """A markdown command has no service of its own to hand: the process's
    is the one that answers."""
    service = _service(tmp_path)
    trust_service.replace(service)
    folder = _folder(tmp_path)
    marker = tmp_path / "cmd-ran"
    _write(folder / ".igni" / "commands" / "go.md", f"Status: !`touch {marker}`\n")

    command = MarkdownCommand.discover(folder, read_claude=False)["go"]
    text = asyncio.run(command.render("", project_dir=folder))
    assert not marker.exists()
    assert "not trusted" in text

    service.trust(folder)

    command = MarkdownCommand.discover(folder, read_claude=False)["go"]
    text = asyncio.run(command.render("", project_dir=folder))
    assert marker.exists()
    assert "Status:" in text
