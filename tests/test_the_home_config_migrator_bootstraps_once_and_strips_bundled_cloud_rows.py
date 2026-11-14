"""``HomeConfigMigrator``: the starter file once, and the stale rows removed.

``~/.igni/config.yaml`` is the person's. The migrator writes the starter only
when there is no file, removes only the rows igni itself once bundled (an igni
server URL with the ``cloud_token`` key), clears a default that named a removed
row, and says in a typed result what it did and why it did nothing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import yaml

from igni_core.core.init.home_migrator import HomeConfigMigrator
from igni_core.core.init_templates import _HOME_CONFIG_BOOTSTRAP, CONFIG_YAML_HEADER

BUNDLED = (
    "    {name}:\n"
    "      provider: openai_like\n"
    "      model_id: x/{name}\n"
    "      url: https://api.ignite-ember.sh/v1\n"
    "      api_key: cloud_token\n"
)
CUSTOM = (
    "    mine:\n"
    "      provider: openai_like\n"
    "      model_id: x/mine\n"
    "      url: https://api.ignite-ember.sh/v1\n"
    "      api_key: my-own-key\n"
)


def _migrator(tmp_path: Path, body: str | None = None) -> HomeConfigMigrator:
    home = tmp_path / ".igni"
    home.mkdir()
    if body is not None:
        (home / "config.yaml").write_text(body)
    return HomeConfigMigrator(home_igni=home)


class TestBootstrap:
    def test_it_writes_the_header_and_the_starter_text_when_there_is_no_file(self, tmp_path):
        m = _migrator(tmp_path)

        m.bootstrap_default_config()

        assert m.config_path.read_text() == CONFIG_YAML_HEADER + _HOME_CONFIG_BOOTSTRAP

    def test_it_never_overwrites_a_file_that_is_there(self, tmp_path):
        m = _migrator(tmp_path, "mine: true\n")

        m.bootstrap_default_config()

        assert m.config_path.read_text() == "mine: true\n"


class TestMigrate:
    def test_no_file_is_ok_with_the_reason_no_file(self, tmp_path):
        result = _migrator(tmp_path).migrate()

        assert (result.ok, result.removed, result.reason) == (True, [], "no-file")

    def test_a_config_that_cannot_be_parsed_is_not_ok_and_is_left_alone(self, tmp_path):
        m = _migrator(tmp_path, "models: [unclosed\n")

        result = m.migrate()

        assert (result.ok, result.removed, result.reason) == (False, [], "unreadable")
        assert m.config_path.read_text() == "models: [unclosed\n"

    def test_a_config_that_is_not_a_mapping_is_unreadable(self, tmp_path):
        result = _migrator(tmp_path, "- a\n- b\n").migrate()

        assert (result.ok, result.reason) == (False, "unreadable")

    @pytest.mark.parametrize("body", ["", "other: 1\n", "models:\n  default: x\n"])
    def test_nothing_to_inspect_is_ok_and_not_rewritten(self, tmp_path, body):
        m = _migrator(tmp_path, body)

        result = m.migrate()

        assert (result.ok, result.removed, result.reason) == (True, [], "")
        assert m.config_path.read_text() == body

    def test_bundled_rows_are_removed_and_reported_by_name(self, tmp_path):
        body = (
            "models:\n  default: mine\n  registry:\n"
            + BUNDLED.format(name="old-a")
            + BUNDLED.format(name="old-b")
            + CUSTOM
        )
        m = _migrator(tmp_path, body)

        result = m.migrate()

        assert (result.ok, sorted(result.removed), result.reason) == (True, ["old-a", "old-b"], "")
        data = yaml.safe_load(m.config_path.read_text())
        assert list(data["models"]["registry"]) == ["mine"]
        assert data["models"]["default"] == "mine"

    def test_a_default_that_named_a_removed_row_is_cleared(self, tmp_path):
        body = "models:\n  default: old-a\n  registry:\n" + BUNDLED.format(name="old-a") + CUSTOM
        m = _migrator(tmp_path, body)

        m.migrate()

        data = yaml.safe_load(m.config_path.read_text())
        assert "default" not in data["models"]
        assert list(data["models"]["registry"]) == ["mine"]

    def test_a_customised_row_is_not_bundled_and_the_file_is_byte_stable(self, tmp_path):
        body = "models:\n  default: mine\n  registry:\n" + CUSTOM
        m = _migrator(tmp_path, body)

        result = m.migrate()

        assert (result.ok, result.removed) == (True, [])
        assert m.config_path.read_text() == body

    def test_a_second_run_removes_nothing_and_writes_nothing(self, tmp_path):
        body = "models:\n  default: old-a\n  registry:\n" + BUNDLED.format(name="old-a") + CUSTOM
        m = _migrator(tmp_path, body)
        m.migrate()
        once = m.config_path.read_bytes()

        result = m.migrate()

        assert (result.ok, result.removed) == (True, [])
        assert m.config_path.read_bytes() == once

    def test_the_removal_is_logged_at_info(self, tmp_path, caplog):
        body = "models:\n  registry:\n" + BUNDLED.format(name="old-a")
        m = _migrator(tmp_path, body)

        with caplog.at_level(logging.INFO, logger="igni_core.core.init.home_migrator"):
            m.migrate()

        assert "old-a" in caplog.text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_a_config_that_cannot_be_written_back_reports_write_failed(self, tmp_path, caplog):
        body = "models:\n  registry:\n" + BUNDLED.format(name="old-a")
        m = _migrator(tmp_path, body)
        m.config_path.chmod(0o400)

        try:
            with caplog.at_level(logging.WARNING, logger="igni_core.core.init.home_migrator"):
                result = m.migrate()
        finally:
            m.config_path.chmod(0o600)

        assert (result.ok, result.removed, result.reason) == (False, ["old-a"], "write-failed")
        assert any(r.levelno == logging.WARNING for r in caplog.records)
