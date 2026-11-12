"""The backend's settings loader, and the gate it closes.

When the organisation's policy is required and no verified pack can be
had, the backend still boots on local settings so the person can sign
in, and the :class:`RunGate` it was handed is closed with the reason.
The gate is the instance given to the loader, not a process-wide one.
"""

from __future__ import annotations

import logging

from igni_core.backend.settings_loader import BackendSettingsLoader
from igni_core.core.config.group_policy.gate import GatePhase, RunGate
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.settings import Settings


def test_a_loadable_policy_leaves_the_gate_open_and_is_what_comes_back(
    tmp_path, monkeypatch
) -> None:
    merged = Settings()
    monkeypatch.setattr(PolicySettingsLoader, "load", lambda self, local=None: merged)
    gate = RunGate()

    assert BackendSettingsLoader(tmp_path, gate).load() is merged
    assert gate.phase is GatePhase.open
    assert gate.reason is None


def test_an_unavailable_policy_closes_the_given_gate_with_the_reason_and_returns_local(
    tmp_path, monkeypatch, caplog
) -> None:
    # A fresh object per read, so only the one the loader read first can be returned.
    reads: list[Settings] = []

    def read_local(self):
        reads.append(Settings())
        return reads[-1]

    monkeypatch.setattr(PolicySettingsLoader, "local", read_local)

    def unavailable(self, local=None):
        raise GroupPolicyUnavailable("sign in to fetch the policy")

    monkeypatch.setattr(PolicySettingsLoader, "load", unavailable)
    mine, other = RunGate(), RunGate()

    with caplog.at_level(logging.ERROR, logger="igni_core.backend.settings_loader"):
        result = BackendSettingsLoader(tmp_path, mine).load()

    assert result is reads[0]
    assert len(reads) == 1
    assert mine.phase is GatePhase.closed
    assert mine.reason == "sign in to fetch the policy"
    assert other.phase is GatePhase.open
    assert any("sign in to fetch the policy" in r.getMessage() for r in caplog.records)
