"""MonitorConfig: validation edges, and where a monitor runs and with what env."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.monitors import MonitorConfig


@pytest.mark.parametrize("command", ["", "   ", "\t \n"])
def test_a_blank_command_is_refused(command):
    with pytest.raises(ValidationError, match="command must not be blank"):
        MonitorConfig(name="x", command=command)


def test_name_and_command_are_required():
    with pytest.raises(ValidationError):
        MonitorConfig(command="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        MonitorConfig(name="x")  # type: ignore[call-arg]


@pytest.mark.parametrize("policy", ["never", "on_crash", "always"])
def test_known_restart_policies_are_kept(policy):
    assert MonitorConfig(name="x", command="c", restart=policy).restart == policy


@pytest.mark.parametrize("policy", [None, 3, ["always"], {"a": 1}, "ALWAYS", ""])
def test_any_other_restart_value_falls_back_to_on_crash(policy):
    assert MonitorConfig(name="x", command="c", restart=policy).restart == "on_crash"


def test_env_that_is_not_a_mapping_is_empty_and_values_are_strings():
    assert MonitorConfig(name="x", command="c", env=["A=1"]).env == {}
    assert MonitorConfig(name="x", command="c", env=None).env == {}
    cfg = MonitorConfig(name="x", command="c", env={"A": 1, "B": None, "C": [1]})
    assert cfg.env == {"A": "1", "B": "None", "C": "[1]"}


def test_unknown_keys_are_ignored_and_unicode_survives():
    cfg = MonitorConfig(name="überwacher", command="c", args=["--名前"], colour="blue")
    assert cfg.name == "überwacher"
    assert cfg.args == ["--名前"]
    assert not hasattr(cfg, "colour")


def test_cwd_is_the_project_dir_when_absent_or_empty(tmp_path):
    assert MonitorConfig(name="x", command="c").resolve_cwd(tmp_path) == str(tmp_path)
    assert MonitorConfig(name="x", command="c", cwd="").resolve_cwd(tmp_path) == str(tmp_path)


def test_a_relative_cwd_is_joined_onto_the_project_dir(tmp_path):
    cfg = MonitorConfig(name="x", command="c", cwd="frontend/app")
    out = cfg.resolve_cwd(tmp_path)
    assert isinstance(out, str)
    assert out == str(tmp_path / "frontend" / "app")


def test_an_absolute_cwd_is_kept(tmp_path):
    elsewhere = str(Path(tmp_path.anchor) / "somewhere" / "else")
    assert MonitorConfig(name="x", command="c", cwd=elsewhere).resolve_cwd(tmp_path) == elsewhere


def test_env_overlays_the_base_without_touching_it():
    cfg = MonitorConfig(name="x", command="c", env={"A": "override", "NEW": "n"})
    base = {"A": "base", "KEEP": "k"}
    merged = cfg.resolve_env(base)
    assert merged == {"A": "override", "KEEP": "k", "NEW": "n"}
    assert base == {"A": "base", "KEEP": "k"}
    assert merged is not base


def test_an_explicit_empty_base_env_is_not_replaced_by_os_environ():
    cfg = MonitorConfig(name="x", command="c", env={"A": "1"})
    assert cfg.resolve_env({}) == {"A": "1"}


def test_the_default_base_env_is_os_environ(monkeypatch):
    monkeypatch.setenv("IGNI_MONITOR_PROBE", "from-os")
    merged = MonitorConfig(name="x", command="c", env={"B": "2"}).resolve_env()
    assert merged["IGNI_MONITOR_PROBE"] == "from-os"
    assert merged["B"] == "2"
    assert merged is not os.environ


@pytest.mark.parametrize("cwd", [3, ["a"], {"a": 1}, True])
def test_a_cwd_that_is_not_a_string_is_dropped(cwd):
    assert MonitorConfig(name="x", command="c", cwd=cwd).cwd is None
