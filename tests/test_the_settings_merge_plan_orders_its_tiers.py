"""The merge plan reads the files it should, in the order it promises.

``SettingsMergePlan.default`` is the one place the precedence is written
down: user, project, project-local, CLI, group, managed. These drive the
plan it builds over real files in a temporary home and project, and read
what the validated ``Settings`` came out as.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from igni_core.cli.options import CliOptions
from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.merge_plan import SettingsMergePlan
from igni_core.core.config.settings import Settings
from igni_core.core.layer import Layer, layers


class _Trust:
    def __init__(self, trusted: bool = True) -> None:
        self.trusted = trusted
        self.asked: list[Path] = []

    def is_trusted(self, folder: Path, *, what: str = "") -> bool:
        self.asked.append(folder)
        return self.trusted


class _NoLocalSettings(Layer):
    def reads_local_settings(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _desktop_layer():
    layers.reset()
    yield
    layers.reset()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _yaml(path: Path, **values: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}: {v}\n" for k, v in values.items()))


def _group(**settings: object) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g",
        group_name="G",
        fetched_at=0.0,
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="s",
                content=json.dumps(settings),
                content_type="json",
                enabled=True,
            )
        ],
    )


def _run(
    project_dir: Path | None,
    *,
    cli: CliOptions | None = None,
    trust: _Trust | None = None,
    managed: Path | None = None,
    group: GroupPolicyPack | None = None,
) -> Settings:
    plan = SettingsMergePlan.default(
        project_dir=project_dir,
        cli=CliOverrides.from_options(cli) if cli else None,
        accumulator=SettingsAccumulator.from_defaults(Settings.default_dict()),
        settings_cls=Settings,
        defaults_models=Settings.defaults().models,
        workspace_trust=trust or _Trust(),  # type: ignore[arg-type]
        managed_path_provider=lambda: managed,
        group_policy_fetcher=(lambda: group) if group else None,
    )
    return plan.run()


def test_each_tier_overrides_the_one_below_it(home: Path, project: Path, tmp_path: Path) -> None:
    _yaml(home / ".igni" / "config.yaml", runtime_python="user")
    assert _run(project).runtime_python == "user"

    _yaml(project / ".igni" / "config.yaml", runtime_python="project")
    assert _run(project).runtime_python == "project"

    _yaml(project / ".igni" / "config.local.yaml", runtime_python="local")
    assert _run(project).runtime_python == "local"

    group = _group(runtime_python="group")
    assert _run(project, group=group).runtime_python == "group"

    managed = tmp_path / "managed.yaml"
    _yaml(managed, runtime_python="managed")
    assert _run(project, group=group, managed=managed).runtime_python == "managed"


def test_the_command_line_beats_every_file_but_loses_to_the_group_and_managed(
    home: Path, project: Path, tmp_path: Path
) -> None:
    _yaml(project / ".igni" / "config.local.yaml", models="{default: from-file}")
    cli = CliOptions(model="from-cli")

    assert _run(project, cli=cli).models.default == "from-cli"
    assert _run(project).models.default == "from-file"

    group = _group(models={"default": "from-group"})
    assert _run(project, cli=cli, group=group).models.default == "from-group"

    managed = tmp_path / "managed.yaml"
    _yaml(managed, models="{default: from-managed}")
    assert _run(project, cli=cli, group=group, managed=managed).models.default == "from-managed"


def test_without_a_group_fetcher_no_group_speaks(home: Path, project: Path) -> None:
    _yaml(project / ".igni" / "config.yaml", runtime_python="project")

    assert _run(project, group=None).runtime_python == "project"


def test_a_missing_file_is_silent_and_leaves_the_built_in_default(
    home: Path, project: Path
) -> None:
    assert _run(project).runtime_python == Settings.default_dict()["runtime_python"]


def test_an_unmigrated_home_and_project_directory_are_still_read(home: Path, project: Path) -> None:
    _yaml(home / ".ember" / "config.yaml", runtime_python="legacy-user")
    assert _run(project).runtime_python == "legacy-user"

    _yaml(project / ".ember" / "config.yaml", runtime_python="legacy-project")
    assert _run(project).runtime_python == "legacy-project"


def test_the_project_directory_defaults_to_the_working_directory(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _yaml(project / ".igni" / "config.yaml", runtime_python="from-cwd")
    monkeypatch.setattr("pathlib.Path.cwd", lambda: project)

    assert _run(None).runtime_python == "from-cwd"


def test_an_untrusted_folder_may_not_set_permissions_but_is_otherwise_read(
    home: Path, project: Path
) -> None:
    (project / ".igni").mkdir()
    (project / ".igni" / "config.yaml").write_text(
        "runtime_python: project\npermissions:\n  mode: bypassPermissions\n"
    )
    trusted = _run(project, trust=_Trust(True))
    trust = _Trust(False)
    untrusted = _run(project, trust=trust)

    assert trusted.permissions.mode == "bypassPermissions"
    assert untrusted.permissions.mode != "bypassPermissions"
    assert untrusted.runtime_python == "project"
    assert trust.asked == [project]


def test_a_layer_that_reads_no_local_settings_reads_no_local_file(
    home: Path, project: Path, tmp_path: Path
) -> None:
    layers.install(_NoLocalSettings())
    _yaml(home / ".igni" / "config.yaml", runtime_python="user", api_url="https://user.example")
    _yaml(project / ".igni" / "config.yaml", runtime_python="project")
    (home / ".igni" / "settings.json").write_text(
        json.dumps({"permissions": {"mode": "bypassPermissions"}})
    )
    managed = tmp_path / "managed.yaml"
    _yaml(managed, api_url="https://managed.example")

    settings = _run(project, cli=CliOptions(model="from-cli"), managed=managed)

    assert settings.runtime_python == Settings.default_dict()["runtime_python"]
    assert settings.permissions.mode != "bypassPermissions"
    assert settings.models.default == "from-cli"
    assert settings.api_url == "https://managed.example"


def test_the_layer_without_local_settings_ignores_the_developers_own_hook_setting(
    home: Path, project: Path
) -> None:
    layers.install(_NoLocalSettings())
    _yaml(home / ".igni" / "config.yaml")
    (home / ".igni" / "config.yaml").write_text("hooks:\n  group: refuse\n")

    assert _run(project).hooks.group == "run"
    layers.reset()
    assert _run(project).hooks.group == "refuse"


def test_a_merged_payload_that_does_not_fit_fails_at_the_end(home: Path, project: Path) -> None:
    (project / ".igni").mkdir()
    (project / ".igni" / "config.yaml").write_text("update_check_ttl: not-a-number\n")

    with pytest.raises(ValidationError):
        _run(project)


def test_a_plan_with_no_tiers_returns_the_defaults() -> None:
    plan = SettingsMergePlan(
        [],
        accumulator=SettingsAccumulator.from_defaults(Settings.default_dict()),
        settings_cls=Settings,
    )

    assert plan.run() == Settings()


def test_the_plan_runs_a_tier_list_it_is_given_in_order() -> None:
    seen: list[str] = []

    def tier(name: str, value: str):
        def apply(acc: SettingsAccumulator) -> SettingsAccumulator:
            seen.append(name)
            return acc.merge({"runtime_python": value})

        return SimpleNamespace(apply=apply)

    plan = SettingsMergePlan(
        [tier("a", "first"), tier("b", "second")],  # type: ignore[list-item]
        accumulator=SettingsAccumulator.from_defaults(Settings.default_dict()),
        settings_cls=Settings,
    )

    assert plan.run().runtime_python == "second"
    assert seen == ["a", "b"]
