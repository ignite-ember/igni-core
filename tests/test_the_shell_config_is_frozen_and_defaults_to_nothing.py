"""``ShellToolsConfig``: the shell toolkit's options, fixed once built."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.tools.shell_config import ShellToolsConfig


def test_it_defaults_to_no_directory_no_confirmation_and_the_igni_name():
    config = ShellToolsConfig()

    assert config.base_dir is None
    assert config.requires_confirmation_tools is None
    assert config.name == "igni_shell"


def test_a_field_cannot_be_assigned_after_construction():
    config = ShellToolsConfig(base_dir=Path("/work"))

    with pytest.raises(ValidationError):
        config.base_dir = Path("/elsewhere")
    with pytest.raises(ValidationError):
        config.name = "other"
    assert config.base_dir == Path("/work")


def test_a_text_directory_becomes_a_path():
    config = ShellToolsConfig(base_dir="/work/project")  # type: ignore[arg-type]

    assert config.base_dir == Path("/work/project")


def test_an_empty_confirmation_list_stays_empty_rather_than_none():
    config = ShellToolsConfig(requires_confirmation_tools=[])

    assert config.requires_confirmation_tools == []


def test_a_custom_name_is_kept():
    assert ShellToolsConfig(name="plugin_shell").name == "plugin_shell"


def test_unknown_options_are_ignored_not_smuggled_in():
    config = ShellToolsConfig(**{"colour": "red"})

    assert not hasattr(config, "colour")
