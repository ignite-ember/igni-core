"""The user-global config file: where it lives, what reads back, what is kept.

Tested with the real store on a temporary directory, so what is pinned is
the file's content, not that a call was made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from igni_core.core.config.user_config_store import UserConfigStore


def test_without_a_path_the_store_is_the_igni_config_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    store = UserConfigStore()

    assert store.path == tmp_path / ".igni" / "config.yaml"


def test_an_explicit_path_is_used_as_given(tmp_path: Path) -> None:
    path = tmp_path / "elsewhere.yaml"

    assert UserConfigStore(path).path == path


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n", "42\n"])
def test_a_file_that_is_not_a_mapping_reads_as_empty(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(text)

    assert UserConfigStore(path).read() == {}


def test_an_absent_file_reads_as_empty(tmp_path: Path) -> None:
    assert UserConfigStore(tmp_path / "nope.yaml").read() == {}


def test_malformed_yaml_raises_rather_than_reading_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: [unclosed\n")

    with pytest.raises(yaml.YAMLError):
        UserConfigStore(path).read()


def test_write_creates_missing_parents_and_keeps_key_order_and_unicode(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "er" / "config.yaml"

    UserConfigStore(path).write({"zeta": 1, "alpha": {"name": "Zoë"}, "mid": [1, 2]})

    lines = path.read_text(encoding="utf-8").splitlines()
    keys = [ln.split(":")[0] for ln in lines if ":" in ln and not ln.startswith(" ")]
    assert keys == ["zeta", "alpha", "mid"]
    assert UserConfigStore(path).read()["alpha"] == {"name": "Zoë"}


def test_set_server_keeps_every_other_key_and_stores_the_portal_when_given(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("theme: dark\nmodels:\n  default: seer\n")

    UserConfigStore(path).set_server("https://api.example", "https://portal.example")

    assert yaml.safe_load(path.read_text()) == {
        "theme": "dark",
        "models": {"default": "seer"},
        "api_url": "https://api.example",
        "portal_url": "https://portal.example",
    }


def test_set_server_raises_on_a_file_it_cannot_parse_and_leaves_it_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: [unclosed\n")

    with pytest.raises(yaml.YAMLError):
        UserConfigStore(path).set_server("https://api.example")

    assert path.read_text() == "a: [unclosed\n"


def test_set_server_raises_when_the_parent_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")

    with pytest.raises(OSError):
        UserConfigStore(blocker / "config.yaml").set_server("https://api.example")


def test_set_default_model_says_false_on_malformed_yaml_and_keeps_the_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: [unclosed\n")

    assert UserConfigStore(path).set_default_model("seer") is False
    assert path.read_text() == "a: [unclosed\n"


def test_set_default_model_says_false_when_the_file_cannot_be_written(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")

    assert UserConfigStore(blocker / "config.yaml").set_default_model("seer") is False


def test_set_default_model_says_false_on_a_file_that_is_not_text(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xff\xfe\x00\x80 not utf-8 \xc3\x28")

    assert UserConfigStore(path).set_default_model("seer") is False


@pytest.mark.parametrize("models", ["models: [a, b]\n", "models: text\n", "models:\n"])
def test_set_default_model_replaces_a_models_block_that_is_not_a_mapping(
    tmp_path: Path, models: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"keep: 1\n{models}")

    assert UserConfigStore(path).set_default_model("seer") is True
    assert yaml.safe_load(path.read_text()) == {"keep": 1, "models": {"default": "seer"}}
