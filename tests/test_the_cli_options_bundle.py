"""``CliOptions``: the typed bundle Click's parameters are validated into.

``test_the_cli_flags_do_something.py`` drives the Click surface for most
flags. This pins the bundle itself — its defaults, what it ignores, what it
refuses — and the one flag that file does not cover: ``--trust``.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

import igni_core.cli as cli_module
from igni_core.cli.options import CliOptions
from igni_core.cli.run_settings import CliSettingsLoader

_BOOLEANS = (
    "verbose",
    "quiet",
    "continue_session",
    "read_only",
    "accept_edits",
    "auto_approve",
    "no_web",
    "strict",
    "pipe",
    "no_color",
    "debug",
    "worktree",
    "trust",
)


def test_an_empty_bundle_is_every_flag_off():
    options = CliOptions.model_validate({})

    for name in _BOOLEANS:
        assert getattr(options, name) is False, name
    assert (options.model, options.message, options.session_id) == (None, None, None)
    assert options.add_dir == ()


def test_parameters_the_bundle_does_not_know_are_ignored():
    options = CliOptions.model_validate({"verbose": True, "not_a_flag": 1})

    assert options.verbose is True
    assert not hasattr(options, "not_a_flag")


def test_a_flag_of_the_wrong_type_fails_at_construction():
    with pytest.raises(ValidationError, match="trust"):
        CliOptions.model_validate({"trust": "banana"})


def test_add_dir_from_a_list_becomes_a_tuple_of_strings():
    options = CliOptions.model_validate({"add_dir": ["/a", "/b"]})

    assert options.add_dir == ("/a", "/b")


def test_a_flag_is_a_string_or_nothing():
    with pytest.raises(ValidationError, match="model"):
        CliOptions.model_validate({"model": ["not", "a", "string"]})


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, CliOptions] = {}
    original = CliSettingsLoader.__init__

    def _spy(self: CliSettingsLoader, options: CliOptions) -> None:
        seen["options"] = options
        original(self, options)

    monkeypatch.setattr(CliSettingsLoader, "__init__", _spy)

    def run(*argv: str) -> CliOptions:
        seen.pop("options", None)
        CliRunner().invoke(cli_module.cli, list(argv))
        assert "options" in seen, f"the CLI did not accept {argv}"
        return seen["options"]

    return run


def test_trust_is_set_by_the_flag_and_off_without_it(parsed):
    assert parsed("--trust").trust is True
    assert parsed().trust is False


def test_pipe_and_message_reach_the_bundle(parsed):
    assert parsed("--pipe").pipe is True
    assert parsed("-m", "hello there").message == "hello there"
