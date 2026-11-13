"""MonitorsFile and MonitorConfigLoader: tiers, precedence, trust, bad input."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.monitors import MonitorConfigLoader, MonitorsFile
from igni_core.core.paths import CONFIG_DIR


class _Trust:
    """A workspace-trust double that records what it was asked."""

    def __init__(self, trusted: bool) -> None:
        self.trusted = trusted
        self.asked: list[Path] = []

    def is_trusted(self, folder: Path, *, what: str = "") -> bool:
        self.asked.append(folder)
        return self.trusted


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def _row(command: str) -> dict:
    return {"monitors": {"m": {"command": command}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    return h


@pytest.mark.parametrize("content", ["", "{not json", "[1]", "3", '"s"', "null"])
def test_an_unusable_file_reads_as_empty(tmp_path, content):
    path = tmp_path / ".monitors.json"
    path.write_text(content)
    assert MonitorsFile.read(path).configs() == {}


def test_a_missing_file_or_a_directory_reads_as_empty(tmp_path):
    assert MonitorsFile.read(tmp_path / "nope.json").configs() == {}
    assert MonitorsFile.read(tmp_path).configs() == {}


@pytest.mark.parametrize("monitors", [[1], "text", None, 4])
def test_a_monitors_value_that_is_not_an_object_is_empty(monitors):
    assert MonitorsFile.model_validate({"monitors": monitors}).configs() == {}


def test_bad_rows_are_dropped_and_the_rest_kept():
    out = MonitorsFile.model_validate(
        {
            "monitors": {
                "ok": {"command": "go"},
                "null": None,
                "list": [],
                "text": "go",
                "blank": {"command": "  "},
                "missing": {},
            }
        }
    ).configs()
    assert list(out) == ["ok"]


def test_the_key_wins_over_a_name_inside_the_row():
    out = MonitorsFile.model_validate({"monitors": {"real": {"command": "c", "name": "fake"}}})
    assert out.configs()["real"].name == "real"


def test_a_namespace_prefixes_both_key_and_name():
    out = MonitorsFile.model_validate({"monitors": {"w": {"command": "c"}}}).configs("plug")
    assert out["plug:w"].name == "plug:w"


def test_user_tier_loads(tmp_path, home):
    _write(home / CONFIG_DIR / "monitors.json", _row("user"))
    assert MonitorConfigLoader(tmp_path, trust=_Trust(True)).load()["m"].command == "user"


def test_plugin_overrides_project_overrides_user(tmp_path, home):
    _write(home / CONFIG_DIR / "monitors.json", {"monitors": {"a": {"command": "user"}}})
    _write(tmp_path / ".monitors.json", {"monitors": {"a": {"command": "project"}}})
    out = MonitorConfigLoader(tmp_path, trust=_Trust(True)).load()
    assert out["a"].command == "project"
    plug = tmp_path / "plug"
    _write(plug / ".monitors.json", {"monitors": {"a": {"command": "plugin"}}})
    out = MonitorConfigLoader(tmp_path, trust=_Trust(True), plugin_roots=[(plug, "p")]).load()
    # The plugin tier is namespaced, so it adds to rather than replaces the others.
    assert out["a"].command == "project"
    assert out["p:a"].command == "plugin"


def test_later_plugin_wins_on_a_repeated_namespaced_name(tmp_path, home):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / ".monitors.json", _row("first"))
    _write(b / ".monitors.json", _row("second"))
    out = MonitorConfigLoader(
        tmp_path, trust=_Trust(True), plugin_roots=[(a, "same"), (b, "same")]
    ).load()
    assert out["same:m"].command == "second"


def test_two_plugins_with_one_simple_name_are_both_kept(tmp_path, home):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / ".monitors.json", _row("first"))
    _write(b / ".monitors.json", _row("second"))
    out = MonitorConfigLoader(
        tmp_path, trust=_Trust(True), plugin_roots=[(a, "one"), (b, "two")]
    ).load()
    assert {k: v.command for k, v in out.items()} == {"one:m": "first", "two:m": "second"}


def test_a_plugin_root_without_a_file_is_skipped(tmp_path, home):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        MonitorConfigLoader(tmp_path, trust=_Trust(True), plugin_roots=[(empty, "e")]).load() == {}
    )


def test_an_untrusted_project_file_is_skipped_but_user_and_plugin_tiers_load(tmp_path, home):
    _write(home / CONFIG_DIR / "monitors.json", {"monitors": {"u": {"command": "user"}}})
    _write(tmp_path / ".monitors.json", {"monitors": {"p": {"command": "project"}}})
    plug = tmp_path / "plug"
    _write(plug / ".monitors.json", {"monitors": {"x": {"command": "plugin"}}})
    trust = _Trust(False)
    out = MonitorConfigLoader(tmp_path, trust=trust, plugin_roots=[(plug, "pl")]).load()
    assert set(out) == {"u", "pl:x"}
    assert trust.asked == [tmp_path]


def test_trust_is_only_asked_when_a_project_file_exists(tmp_path, home):
    _write(home / CONFIG_DIR / "monitors.json", _row("user"))
    trust = _Trust(True)
    MonitorConfigLoader(tmp_path, trust=trust).load()
    assert trust.asked == []
