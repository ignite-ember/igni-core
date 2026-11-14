"""The credential scan's edges: what counts as a key, what it cannot read, how it speaks.

A scan run at every session start must never be the reason a session does not
start, and a warning is read by a person who has to find the line.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from igni_core.core.config.secret_scan import scan_project_config
from igni_core.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR


def write_config(project: Path, text: str, config_dir: str = CONFIG_DIR) -> Path:
    path = project / config_dir / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def registry(**entries: dict) -> str:
    return yaml.safe_dump({"models": {"registry": entries}})


class TestWhatCountsAsAKey:
    @pytest.mark.parametrize(
        "value", ["", None, 12345, True, ["sk-x"], {"k": "sk-x"}, "cloud_token"]
    )
    def test_only_a_non_empty_string_other_than_the_cloud_sentinel_is_a_key(
        self, tmp_path: Path, value
    ):
        write_config(tmp_path, registry(m={"model_id": "m", "api_key": value}))

        assert scan_project_config(tmp_path) == []

    def test_a_string_key_is_one_even_when_other_entries_hold_none(self, tmp_path: Path):
        write_config(
            tmp_path,
            registry(
                a={"api_key": ""},
                b={"api_key": "sk-1"},
                c={"api_key_env": "K"},
                d="not-a-dict",
            ),
        )

        warnings = scan_project_config(tmp_path)

        assert len(warnings) == 1
        assert "'b'" in warnings[0]
        assert "'a'" not in warnings[0]
        assert "'c'" not in warnings[0]

    def test_a_model_name_that_is_not_a_string_is_still_named(self, tmp_path: Path):
        write_config(tmp_path, "models:\n  registry:\n    7:\n      api_key: sk-1\n")

        assert "'7'" in scan_project_config(tmp_path)[0]


class TestWhatItCannotRead:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "just a string",
            "- a\n- list\n",
            "models: just text\n",
            "models: [1, 2]\n",
            "models:\n  registry: [a, b]\n",
            "models:\n  registry:\n    m: [a]\n",
            "models:\n",
        ],
    )
    def test_a_file_that_is_not_a_registry_says_nothing(self, tmp_path: Path, text: str):
        write_config(tmp_path, text)

        assert scan_project_config(tmp_path) == []

    def test_a_file_that_is_not_utf8_says_nothing_and_does_not_raise(self, tmp_path: Path):
        path = write_config(tmp_path, "")
        path.write_bytes(b"models: \xff\xfe\x00 broken")
        write_config(tmp_path, registry(old={"api_key": "sk-1"}), LEGACY_CONFIG_DIR)

        warnings = scan_project_config(tmp_path)

        assert len(warnings) == 1
        assert "'old'" in warnings[0]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_a_file_that_cannot_be_read_says_nothing_and_does_not_raise(self, tmp_path: Path):
        path = write_config(tmp_path, registry(m={"api_key": "sk-1"}))
        path.chmod(0)
        try:
            assert scan_project_config(tmp_path) == []
        finally:
            path.chmod(0o600)

    def test_a_config_yaml_that_is_a_directory_is_skipped(self, tmp_path: Path):
        (tmp_path / CONFIG_DIR / "config.yaml").mkdir(parents=True)

        assert scan_project_config(tmp_path) == []

    def test_a_project_with_no_config_directory_says_nothing(self, tmp_path: Path):
        assert scan_project_config(tmp_path) == []
        assert scan_project_config(str(tmp_path)) == []  # type: ignore[arg-type]


class TestHowItSpeaks:
    def test_one_offender_is_singular(self, tmp_path: Path):
        write_config(tmp_path, registry(solo={"api_key": "sk-1"}))

        text = scan_project_config(tmp_path)[0]

        assert text.startswith(f"{CONFIG_DIR}/config.yaml carries a literal API key for 'solo'.")

    def test_several_offenders_are_plural_and_sorted(self, tmp_path: Path):
        # Written by hand: ``yaml.safe_dump`` would sort the names itself.
        write_config(
            tmp_path,
            "models:\n  registry:\n"
            "    zed: {api_key: sk-1}\n    alpha: {api_key: sk-2}\n    mid: {api_key: sk-3}\n",
        )

        text = scan_project_config(tmp_path)[0]

        assert "carries literal API keys for 'alpha', 'mid', 'zed'." in text

    def test_it_points_at_the_home_config_and_the_env_alternative(self, tmp_path: Path):
        write_config(tmp_path, registry(m={"api_key": "sk-1"}))

        text = scan_project_config(tmp_path)[0]

        assert f"~/{CONFIG_DIR}/config.yaml" in text
        assert "`api_key_env`" in text
        assert "sk-1" not in text
