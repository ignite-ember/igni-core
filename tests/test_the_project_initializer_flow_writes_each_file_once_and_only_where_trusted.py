"""``ProjectInitializer.run``: what each run writes, skips and reports.

Real directories throughout; the home directory is passed in rather than
patched. A folder the trust service does not trust gets no starter settings
and no ``.gitignore`` block; a ``settings.local.json`` that does not validate
is the person's to fix and is never overwritten.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from igni_core.core.init.project_initializer import ProjectInitializer
from igni_core.core.init.schemas import InitConfig
from igni_core.core.init_templates import IGNI_MD_TEMPLATE, PROJECT_CONFIG_TEMPLATE
from igni_core.core.paths import CONFIG_DIR, PROJECT_CONTEXT_FILE
from igni_core.core.workspace_trust import process_trust


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    return project


def _empty_package(tmp_path: Path) -> InitConfig:
    return InitConfig(package_dir=tmp_path / "no-package")


def _run(tmp_path: Path, project: Path, config: InitConfig | None = None):
    return ProjectInitializer(
        project_dir=project,
        trust=process_trust(),
        config=config or _empty_package(tmp_path),
        home_igni=tmp_path / "home" / CONFIG_DIR,
    ).run()


def _package(tmp_path: Path) -> InitConfig:
    root = tmp_path / "pkg"
    (root / "bundled_agents").mkdir(parents=True)
    (root / "bundled_agents" / "editor.md").write_text("agent v1")
    (root / "bundled_skills" / "commit").mkdir(parents=True)
    (root / "bundled_skills" / "commit" / "SKILL.md").write_text("skill v1")
    (root / "bundled_skills" / "empty").mkdir()
    (root / "bundled_skills" / "stray.txt").write_text("not a directory")
    return InitConfig(package_dir=root)


class TestTheStarterSettings:
    def test_a_trusted_first_run_writes_the_default_permissions(self, tmp_path):
        project = _project(tmp_path)

        _run(tmp_path, project)

        written = json.loads((project / CONFIG_DIR / "settings.local.json").read_text())
        assert written == {
            "permissions": {
                "allow": ["WebSearch", "WebFetch"],
                "ask": ["Write", "Edit", "Bash", "BashOutput"],
            }
        }

    def test_permissions_the_person_declared_are_left_as_they_are(self, tmp_path):
        project = _project(tmp_path)
        path = project / CONFIG_DIR / "settings.local.json"
        path.parent.mkdir()
        path.write_text('{"permissions":{"allow":["Read"]}}')

        _run(tmp_path, project)

        assert path.read_text() == '{"permissions":{"allow":["Read"]}}'

    def test_a_file_that_does_not_validate_is_left_byte_identical(self, tmp_path):
        project = _project(tmp_path)
        path = project / CONFIG_DIR / "settings.local.json"
        path.parent.mkdir()
        path.write_text('{"hooks": "not a mapping", "env": {"KEEP": "me"}}')

        _run(tmp_path, project)

        assert path.read_text() == '{"hooks": "not a mapping", "env": {"KEEP": "me"}}'

    def test_a_file_that_is_not_json_at_all_is_treated_as_empty_and_gets_the_starter(
        self, tmp_path
    ):
        project = _project(tmp_path)
        path = project / CONFIG_DIR / "settings.local.json"
        path.parent.mkdir()
        path.write_text("{oops")

        _run(tmp_path, project)

        assert "permissions" in json.loads(path.read_text())

    def test_other_keys_in_the_file_survive_the_starter(self, tmp_path):
        project = _project(tmp_path)
        path = project / CONFIG_DIR / "settings.local.json"
        path.parent.mkdir()
        path.write_text('{"env": {"A": "1"}}')

        _run(tmp_path, project)

        written = json.loads(path.read_text())
        assert written["env"] == {"A": "1"}
        assert "permissions" in written

    def test_the_starter_is_written_on_the_first_run_only(self, tmp_path):
        project = _project(tmp_path)
        _run(tmp_path, project)
        path = project / CONFIG_DIR / "settings.local.json"
        path.unlink()

        _run(tmp_path, project)

        assert not path.exists()


@pytest.mark.workspace_trust
class TestAnUntrustedFolder:
    def test_it_gets_the_rest_of_the_first_run_but_no_settings_and_no_gitignore(self, tmp_path):
        project = _project(tmp_path)
        (project / ".git").mkdir()

        result = _run(tmp_path, project)

        assert result.first_run is True
        assert (project / PROJECT_CONTEXT_FILE).exists()
        assert (project / CONFIG_DIR / "config.yaml").exists()
        assert not (project / CONFIG_DIR / "settings.local.json").exists()
        assert not (project / CONFIG_DIR / ".gitignore").exists()

    def test_the_gitignore_block_arrives_on_the_first_run_after_it_is_trusted(self, tmp_path):
        project = _project(tmp_path)
        (project / ".git").mkdir()
        _run(tmp_path, project)
        process_trust().grant_for_run(project)

        _run(tmp_path, project)

        assert (project / CONFIG_DIR / ".gitignore").exists()


class TestTheTrustServiceIsRequired:
    def test_an_initializer_cannot_be_built_without_one(self, tmp_path):
        with pytest.raises(ValidationError):
            ProjectInitializer(project_dir=tmp_path)  # type: ignore[call-arg]

    def test_the_old_entry_points_are_gone(self):
        import igni_core.core.init.project_initializer as module

        assert not hasattr(module, "initialize_project")
        assert not hasattr(ProjectInitializer, "initialize")


class TestTheFirstRunFiles:
    def test_it_writes_the_context_file_and_the_starter_config(self, tmp_path):
        project = _project(tmp_path)

        _run(tmp_path, project)

        assert (project / PROJECT_CONTEXT_FILE).read_text() == IGNI_MD_TEMPLATE
        assert (project / CONFIG_DIR / "config.yaml").read_text() == PROJECT_CONFIG_TEMPLATE
        assert (project / CONFIG_DIR / ".initialized").exists()

    def test_files_the_person_already_has_are_not_overwritten(self, tmp_path):
        project = _project(tmp_path)
        (project / PROJECT_CONTEXT_FILE).write_text("mine")
        (project / CONFIG_DIR).mkdir()
        (project / CONFIG_DIR / "config.yaml").write_text("mine: 1\n")

        _run(tmp_path, project)

        assert (project / PROJECT_CONTEXT_FILE).read_text() == "mine"
        assert (project / CONFIG_DIR / "config.yaml").read_text() == "mine: 1\n"

    def test_the_first_run_is_reported_once(self, tmp_path):
        project = _project(tmp_path)

        assert _run(tmp_path, project).first_run is True
        assert _run(tmp_path, project).first_run is False

    def test_a_project_marker_without_a_home_marker_still_bootstraps_home_only(self, tmp_path):
        project = _project(tmp_path)
        (project / CONFIG_DIR).mkdir()
        (project / CONFIG_DIR / ".initialized").touch()

        result = _run(tmp_path, project)

        assert result.first_run is False
        assert (tmp_path / "home" / CONFIG_DIR / "config.yaml").exists()
        assert not (project / PROJECT_CONTEXT_FILE).exists()

    def test_the_gitignore_block_is_kept_current_on_every_run(self, tmp_path):
        project = _project(tmp_path)
        _run(tmp_path, project)
        ignore = project / CONFIG_DIR / ".gitignore"
        ignore.unlink()

        _run(tmp_path, project)

        assert ignore.exists()


class TestTheHomeDirectory:
    def test_the_starter_home_config_is_written_once_per_machine(self, tmp_path):
        project = _project(tmp_path)
        _run(tmp_path, project)
        home = tmp_path / "home" / CONFIG_DIR
        assert (home / "config.yaml").exists()
        assert (home / ".initialized").exists()
        (home / "config.yaml").unlink()

        _run(tmp_path, _project_two(tmp_path))

        assert not (home / "config.yaml").exists()

    def test_a_stale_bundled_model_row_is_migrated_on_every_run_and_reported(self, tmp_path):
        project = _project(tmp_path)
        _run(tmp_path, project)
        home = tmp_path / "home" / CONFIG_DIR
        (home / "config.yaml").write_text(
            "models:\n  registry:\n    old:\n      url: https://api.ignite-ember.sh/v1\n"
            "      api_key: cloud_token\n"
        )

        result = _run(tmp_path, project)

        assert result.migration is not None
        assert result.migration.removed == ["old"]
        assert "models" not in (yaml.safe_load((home / "config.yaml").read_text()) or {})


def _project_two(tmp_path: Path) -> Path:
    project = tmp_path / "app2"
    project.mkdir()
    return project


class TestTheLegacyDirectory:
    def test_a_legacy_directory_is_moved_before_a_new_one_is_made_beside_it(self, tmp_path):
        project = _project(tmp_path)
        (project / ".ember").mkdir()
        (project / ".ember" / "keep.txt").write_text("kept")

        _run(tmp_path, project)

        assert (project / CONFIG_DIR / "keep.txt").read_text() == "kept"
        assert not (project / ".ember").exists()


class TestTheBundledFiles:
    def test_agents_and_skills_are_copied_and_non_skills_are_skipped(self, tmp_path):
        project = _project(tmp_path)

        _run(tmp_path, project, _package(tmp_path))

        assert (project / CONFIG_DIR / "agents" / "editor.md").read_text() == "agent v1"
        assert (project / CONFIG_DIR / "skills" / "commit" / "SKILL.md").read_text() == "skill v1"
        assert not (project / CONFIG_DIR / "skills" / "empty").exists()
        assert not (project / CONFIG_DIR / "skills" / "stray.txt").exists()

    def test_a_group_that_ships_agents_stands_the_bundled_agents_down(self, tmp_path):
        project = _project(tmp_path)
        config = _package(tmp_path)
        config.skip_bundled_agents = True

        _run(tmp_path, project, config)

        assert not (project / CONFIG_DIR / "agents").exists()
        assert (project / CONFIG_DIR / "skills" / "commit" / "SKILL.md").exists()

    def test_a_package_without_bundles_is_fine(self, tmp_path):
        project = _project(tmp_path)

        result = _run(tmp_path, project)

        assert result.warnings == []
        assert not (project / CONFIG_DIR / "agents").exists()
        assert not (project / CONFIG_DIR / "skills").exists()

    def test_the_checksums_are_saved_under_their_keys(self, tmp_path):
        project = _project(tmp_path)

        _run(tmp_path, project, _package(tmp_path))

        saved = json.loads((project / CONFIG_DIR / ".checksums.json").read_text())
        assert set(saved) == {"agents/editor.md", "skills/commit/SKILL.md"}

    def test_an_edited_agent_the_package_changed_is_a_warning_and_a_logged_one(
        self, tmp_path, caplog
    ):
        project = _project(tmp_path)
        config = _package(tmp_path)
        _run(tmp_path, project, config)
        (project / CONFIG_DIR / "agents" / "editor.md").write_text("my edit")
        (config.package_dir / "bundled_agents" / "editor.md").write_text("agent v2")

        with caplog.at_level(logging.INFO, logger="igni_core.core.init.project_initializer"):
            result = _run(tmp_path, project, config)

        assert len(result.warnings) == 1
        assert "agents/editor.md" in result.warnings[0]
        assert (project / CONFIG_DIR / "agents" / "editor.md.new").read_text() == "agent v2"
        assert result.warnings[0] in caplog.text


class TestTheSecretScan:
    def test_a_literal_key_in_the_shared_config_is_a_warning_in_the_result(self, tmp_path):
        project = _project(tmp_path)
        (project / CONFIG_DIR).mkdir()
        (project / CONFIG_DIR / "config.yaml").write_text(
            "models:\n  registry:\n    m:\n      api_key: sk-live-abcdef123456\n"
        )

        result = _run(tmp_path, project)

        assert len(result.warnings) == 1
        assert "'m'" in result.warnings[0]
        assert "sk-live-abcdef123456" not in result.warnings[0]


class TestTheHooks:
    def test_the_built_in_hooks_are_written_and_registered(self, tmp_path):
        project = _project(tmp_path)

        _run(tmp_path, project)

        assert (project / CONFIG_DIR / "hooks" / "pre-pr-review.sh").exists()
        settings = json.loads((project / CONFIG_DIR / "settings.json").read_text())
        assert "PreToolUse" in settings["hooks"]

    def test_a_group_that_registers_them_keeps_the_scripts_but_not_the_registration(self, tmp_path):
        project = _project(tmp_path)
        config = _empty_package(tmp_path)
        config.skip_builtin_hook_registration = True

        _run(tmp_path, project, config)

        assert (project / CONFIG_DIR / "hooks" / "pre-pr-review.sh").exists()
        assert not (project / CONFIG_DIR / "settings.json").exists()

    def test_a_group_that_ships_the_scripts_gets_none_written_locally(self, tmp_path):
        project = _project(tmp_path)
        config = _empty_package(tmp_path)
        config.group_ships_hook_scripts = True

        _run(tmp_path, project, config)

        assert not (project / CONFIG_DIR / "hooks").exists()
        assert not (project / CONFIG_DIR / "settings.json").exists()
