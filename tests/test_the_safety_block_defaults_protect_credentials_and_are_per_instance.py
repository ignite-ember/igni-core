"""``safety``: the shipped protected paths, blocked commands and bypass guard."""

from __future__ import annotations

from igni_core.core.config.schemas import SafetyConfig
from igni_core.core.config.schemas.safety import BypassGuardConfig


def test_credential_and_key_files_are_protected_by_default():
    protected = SafetyConfig().protected_paths
    for pattern in (
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "service-account*.json",
        "credentials.json",
    ):
        assert pattern in protected, pattern


def test_the_root_wipe_and_the_fork_bomb_are_blocked_and_pushes_need_confirmation():
    safety = SafetyConfig()
    assert safety.blocked_commands == ["rm -rf /", ":(){ :|:& };:"]
    assert safety.require_confirmation == [
        "git push",
        "git push --force",
        "npm publish",
        "pip install",
        "docker run",
        "terraform apply",
        "kubectl apply",
        "kubectl delete",
    ]
    assert safety.max_file_size_kb == 500


def test_each_instance_owns_its_lists_so_editing_one_leaves_the_defaults_intact():
    first = SafetyConfig()
    first.protected_paths.append("mine")
    first.blocked_commands.clear()
    first.require_confirmation.clear()
    fresh = SafetyConfig()
    assert "mine" not in fresh.protected_paths
    assert fresh.blocked_commands
    assert fresh.require_confirmation


def test_an_empty_list_overrides_the_default_rather_than_extending_it():
    safety = SafetyConfig(protected_paths=[], require_confirmation=[])
    assert safety.protected_paths == []
    assert safety.require_confirmation == []


def test_the_bypass_guard_is_on_with_the_model_a_three_quarter_threshold_and_a_thirty_second_wait():
    guard = SafetyConfig().bypass_guard
    assert guard.enabled is True
    assert guard.threshold == 0.75
    assert guard.use_model is True
    assert guard.explain is True
    assert guard.explain_timeout == 30.0


def test_the_bypass_guard_block_can_be_set_from_a_mapping_and_is_not_shared():
    safety = SafetyConfig.model_validate({"bypass_guard": {"enabled": False, "threshold": 0.5}})
    assert safety.bypass_guard == BypassGuardConfig(enabled=False, threshold=0.5)
    assert SafetyConfig().bypass_guard.enabled is True
