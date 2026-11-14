"""The sandbox switches' edges: unreadable tiers, odd values, and what each warning says.

``test_an_extension_process_is_confined_to_its_grants.py`` pins who may
turn the sandbox off and who may require it. This file pins what is left:
a tier that cannot be read or is not a mapping applies nothing and says
so, the environment switch's spellings, the group policy's failure modes,
which tier ``disabled_by`` names, the three warnings in their words, and
that the result cannot be changed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.extensions.sandbox import REQUIRE_SANDBOX_ENV, SandboxSettings
from igni_core.extensions.sandbox import settings as settings_module

pytestmark = pytest.mark.extension_sandbox


def yaml_file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode())
    return path


def load(**kwargs) -> SandboxSettings:
    kwargs.setdefault("managed_path", lambda: None)
    kwargs.setdefault("environ", {})
    return SandboxSettings.load(**kwargs)


def pack(extensions: object) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Engineering",
        fetched_at=datetime.now(UTC),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="sandbox",
                content=json.dumps({"extensions": extensions}),
                content_type="json",
            )
        ],
    )


# ── A tier that cannot be read applies nothing, and says so ───────────


def test_a_file_that_is_missing_empty_or_not_a_mapping_applies_nothing(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert load(user_file=tmp_path / "missing.yaml").enabled
        assert load(user_file=yaml_file(tmp_path, "empty.yaml", "")).enabled
        assert load(user_file=yaml_file(tmp_path, "list.yaml", "- sandbox\n- false\n")).enabled
    assert not caplog.records


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("syntax.yaml", b"extensions: [unclosed\n"),
        ("latin1.yaml", b"# caf\xe9\nextensions:\n  sandbox: false\n"),
    ],
)
def test_a_file_that_is_not_valid_yaml_or_not_text_applies_nothing_and_warns(
    tmp_path, caplog, name, content
):
    path = tmp_path / name
    path.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        settings = load(user_file=path)
    assert settings.enabled and settings.disabled_by == ""
    assert f"could not read {path}: " in caplog.text


def test_a_path_that_is_a_directory_applies_nothing_and_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert load(user_file=tmp_path).enabled
    assert f"could not read {tmp_path}: " in caplog.text


@pytest.mark.parametrize("block", ["null", "[sandbox, false]", "'sandbox: false'", "42"])
def test_an_extensions_key_that_is_not_a_mapping_applies_nothing(tmp_path, caplog, block):
    path = yaml_file(tmp_path, "c.yaml", f"extensions: {block}\n")
    with caplog.at_level(logging.WARNING):
        settings = load(managed_path=lambda: path)
    assert settings.enabled and not settings.required
    # Not a mapping is not an error: nothing is said.
    assert not caplog.records


def test_an_invalid_block_is_ignored_whole_and_the_warning_names_its_file(tmp_path, caplog):
    path = yaml_file(tmp_path, "managed.yaml", "extensions:\n  sandbox: maybe\n")
    with caplog.at_level(logging.WARNING):
        settings = load(managed_path=lambda: path)
    assert settings.enabled
    assert f"extensions block in {path} is invalid and ignored: " in caplog.text


def test_keys_the_block_does_not_know_do_not_spoil_the_ones_it_does(tmp_path):
    path = yaml_file(tmp_path, "managed.yaml", "extensions:\n  sandbox: false\n  other: 1\n")
    assert not load(managed_path=lambda: path).enabled


# ── The environment switch ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "required"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        (" TRUE ", True),
        ("On", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("maybe", False),
        ("2", False),
    ],
)
def test_the_environment_requires_the_sandbox_only_in_its_four_spellings(value, required):
    assert load(environ={REQUIRE_SANDBOX_ENV: value}).required is required


def test_without_the_variable_nothing_is_required():
    assert not load(environ={}).required


# ── The group policy: what a pack that fails does ─────────────────────


def test_a_pack_that_cannot_be_had_applies_nothing_and_says_why(caplog):
    for failure in (GroupPolicyUnavailable("does not verify"), OSError("disk gone")):

        def unavailable(failure=failure):
            raise failure

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            settings = load(group=unavailable)
        assert settings.enabled and not settings.required
        assert f"group policy not read for extensions: {failure}" in caplog.text


def test_a_failure_that_is_not_a_missing_pack_is_not_swallowed():
    def broken():
        raise RuntimeError("a bug")

    with pytest.raises(RuntimeError, match="a bug"):
        load(group=broken)


def test_a_pack_whose_extensions_block_is_not_a_mapping_or_invalid_applies_nothing(caplog):
    assert not load(group=lambda: pack("require everything")).required
    with caplog.at_level(logging.WARNING):
        assert not load(group=lambda: pack({"require_sandbox": "perhaps"})).required
    assert "extensions block in the group policy is invalid and ignored: " in caplog.text


def test_a_pack_that_says_both_requires_and_the_off_switch_is_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        settings = load(group=lambda: pack({"sandbox": False, "require_sandbox": True}))
    assert settings.required and settings.enabled
    assert (
        "extensions.sandbox: false in the group policy is ignored: an organisation's pack "
        "may require the sandbox, not turn it off"
    ) in caplog.text


def test_a_group_policy_that_says_nothing_against_the_sandbox_warns_of_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        load(group=lambda: pack({"require_sandbox": True}))
        load(group=lambda: None)
    assert not caplog.records


# ── Which tier turned it off ──────────────────────────────────────────


def test_managed_settings_win_when_they_set_the_switch_at_all(tmp_path):
    off = yaml_file(tmp_path, "off.yaml", "extensions:\n  sandbox: false\n")
    on = yaml_file(tmp_path, "on.yaml", "extensions:\n  sandbox: true\n")
    # The user cannot turn off what managed settings turned on.
    held = load(managed_path=lambda: on, user_file=off)
    assert held.enabled and held.disabled_by == ""
    # Both off: managed is the one named.
    both = load(managed_path=lambda: off, user_file=off)
    assert not both.enabled and both.disabled_by == "managed"
    # Managed settings that do not mention it leave it to the user.
    silent = yaml_file(tmp_path, "silent.yaml", "extensions:\n  require_sandbox: false\n")
    by_user = load(managed_path=lambda: silent, user_file=off)
    assert not by_user.enabled and by_user.disabled_by == "user"


def test_a_required_sandbox_stays_on_against_either_off_switch_and_says_which(tmp_path, caplog):
    off = yaml_file(tmp_path, "off.yaml", "extensions:\n  sandbox: false\n")
    for kwargs, who in (({"managed_path": lambda: off}, "managed"), ({"user_file": off}, "user")):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            settings = load(environ={REQUIRE_SANDBOX_ENV: "1"}, **kwargs)
        assert settings.enabled and settings.required and settings.disabled_by == ""
        assert (
            f"extensions.sandbox: false ({who} settings) is ignored because "
            "extensions.require_sandbox is set"
        ) in caplog.text


# ── The user's require_sandbox ────────────────────────────────────────


def test_a_users_require_sandbox_is_ignored_with_a_warning_naming_the_file_and_the_variable(
    tmp_path, caplog
):
    path = yaml_file(tmp_path, "user.yaml", "extensions:\n  require_sandbox: false\n")
    with caplog.at_level(logging.WARNING):
        settings = load(user_file=path)
    assert not settings.required
    assert (
        f"extensions.require_sandbox in {path} is ignored: only the managed settings file "
        f"or {REQUIRE_SANDBOX_ENV} may set it"
    ) in caplog.text


def test_a_user_file_that_does_not_set_it_draws_no_warning(tmp_path, caplog):
    path = yaml_file(tmp_path, "user.yaml", "extensions:\n  sandbox: true\n")
    with caplog.at_level(logging.WARNING):
        load(user_file=path)
    assert not caplog.records


# ── Where the user's file is, and what the result is ──────────────────


def test_a_layer_that_reads_no_local_settings_reads_no_user_file(tmp_path, monkeypatch):
    home_config = yaml_file(tmp_path, "config.yaml", "extensions:\n  sandbox: false\n")
    monkeypatch.setattr(settings_module, "home_config_dir", lambda: tmp_path)

    class NoLocal:
        def reads_local_settings(self) -> bool:
            return False

    class Local:
        def reads_local_settings(self) -> bool:
            return True

    monkeypatch.setattr(settings_module.layers, "current", lambda: NoLocal())
    assert load().enabled
    monkeypatch.setattr(settings_module.layers, "current", lambda: Local())
    assert not load().enabled and home_config.exists()


def test_the_result_cannot_be_changed_after_it_is_read():
    settings = load()
    with pytest.raises(ValidationError):
        settings.enabled = False  # type: ignore[misc]
