"""The trust controller, driven directly: status mapping, answers, settings reload.

``tests/test_workspace_trust_rpc.py`` reaches the controller through the RPC
handler. This file asks it straight: what the status carries for each way a
folder can stand, whether an answer changed trust, which service the controller
asks, and what ``reload_settings`` does when the group policy is unavailable.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from igni_core.backend.workspace_trust_controller import (
    WorkspaceTrustController,
    WorkspaceTrustRefusedError,
    WorkspaceTrustStatus,
    reload_settings,
)
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.settings import Settings
from igni_core.core.workspace_trust import WorkspaceTrust, process_trust, trust_service
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig

pytestmark = pytest.mark.workspace_trust


def _repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


# ── Status ─────────────────────────────────────────────────────────


def test_a_fresh_folder_is_ask_with_no_source_and_no_remote(tmp_path: Path):
    status = WorkspaceTrustController(tmp_path).status()
    assert status.state == "ask"
    assert status.source == "unset"
    assert status.remote is None
    assert status.can_trust is True


def test_the_remote_is_reported_without_its_credentials(tmp_path: Path):
    folder = _repo(tmp_path / "p", "https://user:s3cret@example.com/org/repo.git")
    status = WorkspaceTrustController(folder).status()
    assert status.remote is not None
    assert "s3cret" not in status.remote
    assert "user" not in status.remote
    assert "example.com/org/repo" in status.remote


def test_the_source_says_how_the_state_was_reached(tmp_path: Path):
    controller = WorkspaceTrustController(tmp_path)
    assert controller.answer("once")[0].source == "run"
    controller.answer("trust")
    assert process_trust().store.get(process_trust().decide(tmp_path).identity) == "trusted"
    process_trust().revoke(tmp_path)
    assert controller.answer("trust")[0].source == "store"
    process_trust().set_policy(WorkspaceTrustConfig(mode="never"))
    assert controller.status().source == "policy"


def test_the_path_is_canonical_and_a_str_project_dir_is_accepted(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    status = WorkspaceTrustController(str(link)).status()  # type: ignore[arg-type]
    assert status.path == str(real.resolve())


def test_status_of_maps_every_field_of_the_decision(tmp_path: Path):
    decision = process_trust().decide(tmp_path)
    status = WorkspaceTrustStatus.of(decision)
    assert (status.state, status.can_trust, status.reason) == (
        decision.state,
        decision.can_trust,
        decision.reason,
    )
    assert (status.path, status.policy_mode, status.source) == (
        decision.identity.path,
        decision.policy_mode,
        decision.source,
    )
    assert status.gated  # the default summary of what an untrusted folder lacks


# ── Answers ────────────────────────────────────────────────────────


def test_trust_reports_that_trust_changed_and_a_repeat_does_not(tmp_path: Path):
    controller = WorkspaceTrustController(tmp_path)
    status, changed = controller.answer("trust")
    assert (status.state, changed) == ("trusted", True)
    status, changed = controller.answer("trust")
    assert (status.state, changed) == ("trusted", False)


def test_distrust_of_an_untrusted_folder_changes_nothing(tmp_path: Path):
    status, changed = WorkspaceTrustController(tmp_path).answer("distrust")
    assert (status.state, changed) == ("untrusted", False)


def test_distrust_of_a_trusted_folder_changes_trust(tmp_path: Path):
    controller = WorkspaceTrustController(tmp_path)
    controller.answer("trust")
    status, changed = controller.answer("distrust")
    assert (status.state, changed) == ("untrusted", True)


def test_once_trusts_without_saving(tmp_path: Path):
    controller = WorkspaceTrustController(tmp_path)
    status, changed = controller.answer("once")
    assert (status.state, changed) == ("trusted", True)
    assert process_trust().store.get(process_trust().decide(tmp_path).identity) is None


@pytest.mark.parametrize("answer", ["", "Trust", "TRUST", "maybe", "trust "])
def test_an_unknown_answer_is_refused_and_records_nothing(tmp_path: Path, answer: str):
    controller = WorkspaceTrustController(tmp_path)
    with pytest.raises(WorkspaceTrustRefusedError, match="Unknown answer"):
        controller.answer(answer)
    assert controller.status().state == "ask"
    assert process_trust().store.get(process_trust().decide(tmp_path).identity) is None


def test_trusted_remotes_refuses_a_folder_with_an_unapproved_remote(tmp_path: Path):
    process_trust().set_policy(
        WorkspaceTrustConfig(mode="trusted_remotes", trusted_remotes=["example.com/approved/*"])
    )
    folder = _repo(tmp_path / "p", "https://example.com/other/repo.git")
    controller = WorkspaceTrustController(folder)
    assert controller.status().can_trust is False
    for answer in ("trust", "once"):
        with pytest.raises(WorkspaceTrustRefusedError, match="approved git remotes"):
            controller.answer(answer)
    assert controller.status().state == "untrusted"


def test_trusted_remotes_allows_a_folder_with_an_approved_remote(tmp_path: Path):
    process_trust().set_policy(
        WorkspaceTrustConfig(mode="trusted_remotes", trusted_remotes=["example.com/approved/*"])
    )
    folder = _repo(tmp_path / "p", "https://example.com/approved/repo.git")
    status, changed = WorkspaceTrustController(folder).answer("trust")
    assert (status.state, changed) == ("trusted", True)


def test_distrust_is_not_refused_while_policy_is_never(tmp_path: Path):
    process_trust().set_policy(WorkspaceTrustConfig(mode="never"))
    status, changed = WorkspaceTrustController(tmp_path).answer("distrust")
    assert (status.state, changed) == ("untrusted", False)


# ── Which trust service the controller asks ────────────────────────


def test_an_injected_service_is_used_instead_of_the_process_one(tmp_path: Path):
    own = WorkspaceTrust()
    controller = WorkspaceTrustController(tmp_path, trust=own)
    controller.answer("once")
    assert own.decide(tmp_path).state == "trusted"
    assert process_trust().decide(tmp_path).state == "ask"


def test_an_injected_service_that_is_falsy_is_still_used(tmp_path: Path):
    class Falsy(WorkspaceTrust):
        def __bool__(self) -> bool:
            return False

    own = Falsy()
    WorkspaceTrustController(tmp_path, trust=own).answer("once")
    assert own.decide(tmp_path).state == "trusted"
    assert process_trust().decide(tmp_path).state == "ask"


def test_the_process_service_is_read_per_call(tmp_path: Path):
    controller = WorkspaceTrustController(tmp_path)
    controller.answer("once")  # held by this process's service, not on disk
    assert controller.status().state == "trusted"
    trust_service.replace(WorkspaceTrust())
    assert controller.status().state == "ask"


# ── Reloading settings ─────────────────────────────────────────────


def test_reload_falls_back_to_the_local_settings_when_the_policy_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    local = Settings()
    calls: list[str] = []

    class Loader:
        def __init__(self, project_dir: Path) -> None:
            calls.append("init")

        def local(self) -> Settings:
            calls.append("local")
            return local

        def load(self, given: Settings) -> Settings:
            assert given is local
            calls.append("load")
            raise GroupPolicyUnavailable("the pack is required but missing")

    monkeypatch.setattr("igni_core.backend.workspace_trust_controller.PolicySettingsLoader", Loader)
    with caplog.at_level(logging.ERROR):
        assert reload_settings(tmp_path) is local
    assert calls == ["init", "local", "load"]
    assert "the pack is required but missing" in caplog.text


def test_reload_returns_the_policy_aware_load_when_it_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    local, loaded = Settings(), Settings()

    class Loader:
        def __init__(self, project_dir: Path) -> None: ...
        def local(self) -> Settings:
            return local

        def load(self, given: Settings) -> Settings:
            return loaded

    monkeypatch.setattr("igni_core.backend.workspace_trust_controller.PolicySettingsLoader", Loader)
    assert reload_settings(tmp_path) is loaded


# ── The published status model ─────────────────────────────────────


def test_every_status_field_is_published_with_a_description():
    properties = WorkspaceTrustStatus.model_json_schema()["properties"]
    assert set(properties) == {
        "state",
        "can_trust",
        "reason",
        "path",
        "remote",
        "policy_mode",
        "gated",
        "source",
    }
    assert all(prop.get("description") for prop in properties.values())
