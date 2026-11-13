"""Plugins a pack installs from a git source, in the pack's own directory.

The coordinator owns ``<cache>/plugins/<entry name>``: it installs once,
adopts an older install out of the user's directory, says which plugins are
missing, and removes what the pack does not name. A failing plugin must
never stop the rest of the pack.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.plugins import PluginInstallCoordinator
from igni_core.core.config.group_policy.signed_store import PluginSourceClaim
from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.installer import PluginError

URL = "https://git.example.invalid/org/tool.git"


class _Installer:
    """Records what it is asked and installs by creating the directory."""

    def __init__(self, *, raises: Exception | None = None, released: list[Path] | None = None):
        self.raises = raises
        self.released = released or []
        self.release_raises: Exception | None = None
        self.scoped: list[Path] = []
        self.installs: list[dict] = []
        self.release_asked: list[str] = []

    def scoped_to(self, plugins_dir) -> _Installer:
        self.scoped.append(Path(plugins_dir))
        return self

    def release_installed_from(self, url: str) -> list[Path]:
        self.release_asked.append(url)
        if self.release_raises is not None:
            raise self.release_raises
        return list(self.released)

    def install(self, url, *, ref=None, subdir=None, directory_name=None):
        self.installs.append(
            {"url": url, "ref": ref, "subdir": subdir, "directory_name": directory_name}
        )
        if self.raises is not None:
            raise self.raises
        (self.scoped[-1] / directory_name).mkdir(parents=True)


def _entry(name: str = "tool", **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=extra.pop("kind", "plugins"),
        entry_name=name,
        content="",
        content_type="yaml",
        **{"source_url": URL, **extra},
    )


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g", group_name="G", fetched_at=datetime.now(timezone.utc), entries=list(entries)
    )


@pytest.fixture
def layout(tmp_path: Path) -> GroupPolicyLayout:
    return GroupPolicyLayout(root=tmp_path / "group-policy")


def _coordinator(layout, installer) -> PluginInstallCoordinator:
    return PluginInstallCoordinator(layout, installer)


class TestInstall:
    def test_it_clones_into_a_directory_named_for_the_entry_with_ref_and_subdir(self, layout):
        installer = _Installer()

        _coordinator(layout, installer).install(_entry("tool", source_ref="v2", source_subdir="p"))

        assert installer.scoped == [layout.dir_for("plugins")]
        assert installer.installs == [
            {"url": URL, "ref": "v2", "subdir": "p", "directory_name": "tool"}
        ]
        assert (layout.dir_for("plugins") / "tool").is_dir()

    def test_an_existing_directory_is_not_cloned_again(self, layout):
        installer = _Installer()
        (layout.dir_for("plugins") / "tool").mkdir(parents=True)

        _coordinator(layout, installer).install(_entry())

        assert installer.installs == []
        assert installer.release_asked == []

    def test_an_unsafe_name_installs_nothing(self, layout):
        installer = _Installer()

        _coordinator(layout, installer).install(_entry("../out"))

        assert installer.installs == []
        assert not (layout.root.parent / "out").exists()

    def test_an_entry_without_a_source_installs_nothing(self, layout):
        installer = _Installer()
        entry = GroupPolicyEntry(kind="plugins", entry_name="tool", content="", content_type="yaml")

        _coordinator(layout, installer).install(entry)

        assert installer.installs == []

    def test_without_an_installer_it_warns_and_installs_nothing(self, layout, caplog):
        caplog.set_level(logging.WARNING)

        _coordinator(layout, None).install(_entry("tool"))

        assert not (layout.dir_for("plugins") / "tool").exists()
        assert any(
            "'tool'" in r.getMessage() and "no installer is configured" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        "message", ["Plugin 'tool' is already installed", "target already exists at /x"]
    )
    def test_a_plugin_the_installer_says_is_there_already_is_reused_quietly(
        self, layout, caplog, message
    ):
        caplog.set_level(logging.INFO)
        installer = _Installer(raises=PluginError(message))

        _coordinator(layout, installer).install(_entry())

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []
        assert any("already installed from" in r.getMessage() for r in caplog.records)

    def test_any_other_plugin_error_is_warned_about_and_does_not_raise(self, layout, caplog):
        caplog.set_level(logging.WARNING)
        installer = _Installer(raises=PluginError("bad manifest"))

        _coordinator(layout, installer).install(_entry())

        assert any(
            "install failed: bad manifest" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.parametrize("error", [GitError("no git"), OSError("disk full")])
    def test_a_git_or_os_failure_is_warned_about_and_does_not_raise(self, layout, caplog, error):
        caplog.set_level(logging.WARNING)
        installer = _Installer(raises=error)

        _coordinator(layout, installer).install(_entry())

        assert any(
            "install errored" in r.getMessage() and str(error) in r.getMessage()
            for r in caplog.records
        )


class TestAdoptingAnOlderInstall:
    def _released(self, tmp_path: Path, *names: str) -> list[Path]:
        paths = []
        for name in names:
            path = tmp_path / "user-plugins" / name
            path.mkdir(parents=True)
            (path / "plugin.json").write_text(name)
            paths.append(path)
        return paths

    def test_the_first_released_directory_becomes_the_packs_install_and_no_clone_happens(
        self, layout, tmp_path
    ):
        old = self._released(tmp_path, "old-tool")
        installer = _Installer(released=old)

        _coordinator(layout, installer).install(_entry("tool"))

        assert installer.release_asked == [URL]
        assert installer.installs == []
        assert (layout.dir_for("plugins") / "tool" / "plugin.json").read_text() == "old-tool"
        assert not old[0].exists()

    def test_a_second_released_directory_is_removed(self, layout, tmp_path):
        first, second = self._released(tmp_path, "one", "two")
        installer = _Installer(released=[first, second])

        _coordinator(layout, installer).install(_entry("tool"))

        assert (layout.dir_for("plugins") / "tool" / "plugin.json").read_text() == "one"
        assert not second.exists()

    def test_when_the_target_exists_every_released_directory_is_removed(self, layout, tmp_path):
        (old,) = self._released(tmp_path, "one")
        target = layout.dir_for("plugins") / "tool"
        target.mkdir(parents=True)
        (target / "keep").write_text("mine")
        installer = _Installer(released=[old])

        _coordinator(layout, installer).adopt_user_install(_entry("tool"), target)

        assert not old.exists()
        assert (target / "keep").read_text() == "mine"

    def test_a_failure_to_look_is_not_the_packs_problem(self, layout):
        installer = _Installer()
        installer.release_raises = ValueError("plugins.json is corrupt")

        _coordinator(layout, installer).install(_entry("tool"))

        assert [c["directory_name"] for c in installer.installs] == ["tool"]

    def test_a_move_that_fails_is_warned_about_and_the_pack_goes_on(
        self, layout, tmp_path, caplog, monkeypatch
    ):
        (old,) = self._released(tmp_path, "one")
        installer = _Installer(released=[old])

        def refuse(src, dst):
            raise OSError("cross-device")

        monkeypatch.setattr("igni_core.core.config.group_policy.plugins.shutil.move", refuse)
        caplog.set_level(logging.WARNING)

        _coordinator(layout, installer).install(_entry("tool"))

        assert any("could not move" in r.getMessage() for r in caplog.records)
        assert [c["directory_name"] for c in installer.installs] == ["tool"]

    def test_with_no_installer_or_no_source_nothing_is_asked(self, layout):
        installer = _Installer()
        target = layout.dir_for("plugins") / "tool"

        _coordinator(layout, None).adopt_user_install(_entry("tool"), target)
        _coordinator(layout, installer).adopt_user_install(
            GroupPolicyEntry(kind="plugins", entry_name="tool", content="", content_type="yaml"),
            target,
        )

        assert installer.release_asked == []


class TestWhatIsMissing:
    def test_an_enabled_source_plugin_with_no_directory_is_missing(self, layout):
        pack = _pack(_entry("a"), _entry("b"))
        (layout.dir_for("plugins") / "b").mkdir(parents=True)

        assert _coordinator(layout, None).missing(pack) == ["a"]

    def test_disabled_other_kind_sourceless_and_unsafe_entries_are_not_counted(self, layout):
        plain = GroupPolicyEntry(kind="plugins", entry_name="p", content="", content_type="yaml")
        pack = _pack(
            _entry("off", enabled=False),
            _entry("agentish", kind="agents"),
            plain,
            _entry("../x"),
        )

        assert _coordinator(layout, None).missing(pack) == []

    def test_a_claim_with_nothing_installed_means_the_pack_lacks_one(self, layout):
        claim = PluginSourceClaim(kind="plugins", entry_name="a", source_url=URL)

        assert _coordinator(layout, None).lacks_any([claim]) is True

    def test_an_installed_claim_lacks_nothing(self, layout):
        (layout.dir_for("plugins") / "a").mkdir(parents=True)
        claim = PluginSourceClaim(kind="plugins", entry_name="a", source_url=URL)

        assert _coordinator(layout, None).lacks_any([claim]) is False

    @pytest.mark.parametrize(
        "claim",
        [
            PluginSourceClaim(kind="agents", entry_name="a", source_url=URL),
            PluginSourceClaim(kind="plugins", entry_name="a", source_url=None),
            PluginSourceClaim(kind="plugins", entry_name="a", source_url=""),
            PluginSourceClaim(kind="plugins", entry_name="", source_url=URL),
            PluginSourceClaim(kind="", entry_name="a", source_url=URL),
            PluginSourceClaim(kind="plugins", entry_name="../a", source_url=URL),
        ],
    )
    def test_a_claim_that_names_no_plugin_to_install_is_skipped(self, layout, claim):
        assert _coordinator(layout, None).lacks_any([claim]) is False

    def test_one_missing_claim_among_installed_ones_is_enough(self, layout):
        (layout.dir_for("plugins") / "a").mkdir(parents=True)
        claims = [
            PluginSourceClaim(kind="plugins", entry_name="a", source_url=URL),
            PluginSourceClaim(kind="plugins", entry_name="b", source_url=URL),
        ]

        assert _coordinator(layout, None).lacks_any(claims) is True
        assert _coordinator(layout, None).lacks_any([]) is False


class TestPruning:
    def test_a_directory_the_pack_does_not_name_is_removed_with_a_warning(self, layout, caplog):
        caplog.set_level(logging.WARNING)
        plugins = layout.dir_for("plugins")
        for name in ("named", "planted"):
            (plugins / name).mkdir(parents=True)
        (plugins / "file.txt").write_text("not a directory")

        _coordinator(layout, None).prune_planted(_pack(_entry("named")))

        assert sorted(p.name for p in plugins.iterdir()) == ["file.txt", "named"]
        assert any(
            "planted" in r.getMessage()
            and "not in the organisation's signed pack" in r.getMessage()
            for r in caplog.records
        )

    def test_a_directory_named_only_by_a_sourceless_or_other_kind_entry_is_removed(self, layout):
        plugins = layout.dir_for("plugins")
        for name in ("plain", "agentish"):
            (plugins / name).mkdir(parents=True)
        plain = GroupPolicyEntry(
            kind="plugins", entry_name="plain", content="", content_type="yaml"
        )

        _coordinator(layout, None).prune_planted(_pack(plain, _entry("agentish", kind="agents")))

        assert list(plugins.iterdir()) == []

    def test_a_missing_plugins_directory_is_fine(self, layout):
        _coordinator(layout, None).prune_planted(_pack())

        assert not layout.dir_for("plugins").exists()

    def test_the_directory_property_is_the_plugins_directory(self, layout):
        assert _coordinator(layout, None).directory == layout.root / "plugins"
