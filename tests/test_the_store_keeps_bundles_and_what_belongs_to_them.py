"""The store keeps bundles, and what belongs to a bundle goes with it.

A bundle is copied in whole or not at all; an upgrade that cannot land
costs nothing; an id that is not a name never becomes a path. Removing
an extension takes its record, its environments and its settings, even
when somebody already deleted the bundle by hand, so a reinstall starts
clean. Assets stay only while an installed manifest declares them.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.paths import home_config_dir
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.store import EXTENSIONS_DIR, ExtensionStore
from igni_core.extensions.store_errors import StoreError, StoreName
from tests.extension_distribution_helpers import asset


def bundle(tmp_path: Path, name: str = "src", **changes: Any) -> Path:
    """A development bundle that declares one setting."""
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
        "contributes": {
            "settings": [{"key": "level", "type": "string", "label": "Level", "default": "low"}]
        },
        **changes,
    }
    (directory / "manifest.json").write_text(json.dumps(raw))
    (directory / "server.py").write_text(f"# {raw['version']}\n")
    return directory


def record(version: str = "1.0.0") -> DistributionRecord:
    return DistributionRecord(
        extension_id="acme.notes",
        version=version,
        sha256="a" * 64,
        service_url="https://notes.acme.test",
    )


@pytest.fixture
def store(tmp_path: Path) -> ExtensionStore:
    return ExtensionStore(tmp_path / "store")


# ── Names ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["a/b", "/abs", "a\\b", "..\\x", ".", "..", "../x", "a/.."])
def test_an_id_that_could_leave_the_store_is_not_a_name(name: str):
    with pytest.raises(StoreError, match="not a usable directory name"):
        StoreName(name)


@pytest.mark.parametrize("name", ["acme.notes", ".hidden", "a..b", "ünï", "", "x" * 200])
def test_any_other_id_is_a_name_as_it_is(name: str):
    assert StoreName(name).id == name


def test_a_name_makes_the_file_beside_the_bundle():
    name = StoreName("acme.notes")
    assert name.with_suffix(".settings.json") == "acme.notes.settings.json"
    assert name.with_suffix("") == "acme.notes"


@pytest.mark.parametrize("name", ["a\\b", "..", "."])
def test_the_store_will_not_name_a_path_for_such_an_id(store: ExtensionStore, name: str):
    with pytest.raises(StoreError):
        store.path_for(name)


# ── Where things are ───────────────────────────────────────────────


def test_the_default_store_is_the_extensions_folder_of_the_config_dir():
    assert ExtensionStore().root == home_config_dir() / EXTENSIONS_DIR


def test_the_store_says_where_environments_and_assets_live(store: ExtensionStore):
    assert store.environments_root == store.root / ".python"
    assert store.asset_cache.root == store.root / ".assets"


def test_a_store_that_does_not_exist_yet_has_nothing_installed(store: ExtensionStore):
    assert not store.root.exists()
    assert store.installed() == []


# ── What is installed ──────────────────────────────────────────────


def test_installed_is_sorted_and_skips_what_is_not_a_bundle(
    store: ExtensionStore, tmp_path: Path, caplog
):
    for extension_id in ("acme.zeta", "acme.alpha"):
        store.add(bundle(tmp_path, extension_id, id=extension_id))
    (store.root / ".acme.notes.incoming").mkdir()
    (store.root / ".acme.notes.incoming" / "manifest.json").write_text("{}")
    (store.root / "acme.empty").mkdir()
    (store.root / "notes.txt").write_text("a file, not a bundle")
    with caplog.at_level(logging.WARNING):
        listed = store.installed()
    assert [p.name for p in listed] == ["acme.alpha", "acme.zeta"]
    assert "acme.empty: no manifest.json" in caplog.text
    assert ".incoming" not in caplog.text, "the store's own directories are skipped quietly"


def test_reading_a_directory_without_a_manifest_names_it(store: ExtensionStore, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(StoreError, match=f"{empty} has no manifest.json"):
        store.read(empty)


def test_reading_an_invalid_manifest_names_the_file(store: ExtensionStore, tmp_path: Path):
    broken = bundle(tmp_path, "broken", id="Not An Id")
    with pytest.raises(StoreError, match=r"manifest\.json is not a valid manifest"):
        store.read(broken)


# ── Adding ─────────────────────────────────────────────────────────


def test_an_id_already_installed_needs_replace(store: ExtensionStore, tmp_path: Path):
    store.add(bundle(tmp_path))
    with pytest.raises(StoreError, match="already installed.*--replace"):
        store.add(bundle(tmp_path, "again", version="2.0.0"))
    assert store.read(store.path_for("acme.notes")).version == "1.0.0"


def test_adding_returns_where_the_bundle_landed_and_copies_everything(
    store: ExtensionStore, tmp_path: Path
):
    landed = store.add(bundle(tmp_path))
    assert landed == store.root / "acme.notes"
    assert (landed / "server.py").read_text() == "# 1.0.0\n"


def test_an_upgrade_replaces_the_files_and_leaves_no_dot_leftovers(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path))
    old = store.path_for("acme.notes")
    (old / "stale.txt").write_text("from the old version")
    store.add(bundle(tmp_path, "new", version="2.0.0"), replace=True)
    new = store.path_for("acme.notes")
    assert (new / "server.py").read_text() == "# 2.0.0\n"
    assert not (new / "stale.txt").exists()
    assert sorted(p.name for p in store.root.iterdir()) == ["acme.notes"]


def test_leftovers_of_a_crashed_install_do_not_get_in_the_way(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path))
    for leftover in (".acme.notes.incoming", ".acme.notes.outgoing"):
        (store.root / leftover).mkdir()
        (store.root / leftover / "junk").write_text("from a crash")
    store.add(bundle(tmp_path, "new", version="2.0.0"), replace=True)
    assert not (store.root / "acme.notes" / "junk").exists()
    assert sorted(p.name for p in store.root.iterdir()) == ["acme.notes"]
    assert store.read(store.path_for("acme.notes")).version == "2.0.0"


def test_a_copy_that_fails_midway_leaves_nothing_and_says_why(
    store: ExtensionStore, tmp_path: Path, monkeypatch
):
    def fail_after_creating(source: Any, destination: Any) -> None:
        Path(destination).mkdir()
        (Path(destination) / "half").write_text("half a bundle")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", fail_after_creating)
    with pytest.raises(OSError, match="disk full"):
        store.add(bundle(tmp_path))
    assert list(store.root.iterdir()) == []
    assert store.installed() == []


def test_a_copy_that_fails_during_an_upgrade_keeps_the_working_version(
    store: ExtensionStore, tmp_path: Path, monkeypatch
):
    store.add(bundle(tmp_path))

    def fail(source: Any, destination: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", fail)
    with pytest.raises(OSError, match="disk full"):
        store.add(bundle(tmp_path, "new", version="2.0.0"), replace=True)
    monkeypatch.undo()
    assert store.read(store.path_for("acme.notes")).version == "1.0.0"
    assert [p.name for p in store.root.iterdir()] == ["acme.notes"]


def test_a_swap_that_fails_puts_the_old_bundle_back_and_removes_the_staging(
    store: ExtensionStore, tmp_path: Path, monkeypatch
):
    store.add(bundle(tmp_path))
    real_rename = Path.rename

    def refuse_incoming(self: Path, target: Any) -> Any:
        if self.name.endswith(".incoming"):
            raise OSError("cannot rename")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse_incoming)
    with pytest.raises(OSError, match="cannot rename"):
        store.add(bundle(tmp_path, "new", version="2.0.0"), replace=True)
    monkeypatch.undo()
    assert store.read(store.path_for("acme.notes")).version == "1.0.0"
    assert [p.name for p in store.root.iterdir()] == ["acme.notes"]


def test_an_install_records_its_distribution_and_a_development_install_forgets_it(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path), distribution=record())
    assert store.provenance.of("acme.notes") == record()
    store.add(bundle(tmp_path, "dev", version="1.1.0"), replace=True)
    assert store.provenance.of("acme.notes") is None
    assert not store.provenance.path("acme.notes").exists()


def test_a_new_install_drops_the_environments_built_from_the_old_one(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path))
    built = store.environments_root / "acme.notes" / "1.0.0"
    built.mkdir(parents=True)
    other = store.environments_root / "acme.other" / "1.0.0"
    other.mkdir(parents=True)
    store.add(bundle(tmp_path, "new", version="2.0.0"), replace=True)
    assert not (store.environments_root / "acme.notes").exists()
    assert other.is_dir(), "another extension's environment is not touched"


def test_dropping_environments_that_are_not_there_is_harmless(store: ExtensionStore):
    store.drop_environments("acme.notes")


def test_a_replaced_bundle_with_read_only_assets_is_removed_whole(
    store: ExtensionStore, tmp_path: Path
):
    from igni_core.extensions.assets import ReadOnlyTree

    store.add(bundle(tmp_path))
    laid_out = store.path_for("acme.notes") / "assets" / "model"
    laid_out.mkdir(parents=True)
    (laid_out / "weights.bin").write_text("x")
    ReadOnlyTree.seal(store.path_for("acme.notes") / "assets")
    store.add(bundle(tmp_path, "new", version="2.0.0"), replace=True)
    assert sorted(p.name for p in store.root.iterdir()) == ["acme.notes"]
    assert not (store.path_for("acme.notes") / "assets").exists()


# ── Removing ───────────────────────────────────────────────────────


def test_removing_takes_the_bundle_its_record_its_environments_and_its_settings(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path), distribution=record())
    manifest = store.read(store.path_for("acme.notes"))
    store.save_settings(manifest, {"level": "high"})
    (store.environments_root / "acme.notes" / "1.0.0").mkdir(parents=True)

    assert store.remove("acme.notes") is True

    assert list(store.root.iterdir()) == [store.environments_root]
    assert not store.provenance.path("acme.notes").exists()
    assert not (store.root / "acme.notes.settings.json").exists()
    assert not (store.environments_root / "acme.notes").exists()


def test_a_reinstall_does_not_inherit_what_the_last_administrator_configured(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path))
    manifest = store.read(store.path_for("acme.notes"))
    store.save_settings(manifest, {"level": "high"})
    store.remove("acme.notes")
    store.add(bundle(tmp_path))
    assert store.settings(manifest) == {"level": "low"}


def test_removing_what_is_not_installed_says_so(store: ExtensionStore):
    assert store.remove("acme.notes") is False


def test_a_bundle_deleted_by_hand_still_loses_what_belonged_to_it(
    store: ExtensionStore, tmp_path: Path
):
    store.add(bundle(tmp_path), distribution=record())
    manifest = store.read(store.path_for("acme.notes"))
    store.save_settings(manifest, {"level": "high"})
    (store.environments_root / "acme.notes" / "1.0.0").mkdir(parents=True)
    shutil.rmtree(store.path_for("acme.notes"))

    assert store.remove("acme.notes") is False

    assert not store.provenance.path("acme.notes").exists()
    assert not (store.environments_root / "acme.notes").exists()
    assert not (store.root / "acme.notes.settings.json").exists()


def test_removing_an_id_that_is_not_a_name_is_refused(store: ExtensionStore):
    with pytest.raises(StoreError):
        store.remove("../elsewhere")


# ── Assets ─────────────────────────────────────────────────────────


def test_assets_stay_only_while_an_installed_manifest_declares_them(
    store: ExtensionStore, tmp_path: Path
):
    kept = asset("model", "any", b"kept bytes", file="model.onnx")
    store.add(bundle(tmp_path, assets=[kept]))
    store.root.joinpath(".assets").mkdir()
    (store.root / ".assets" / kept["sha256"]).write_bytes(b"kept bytes")
    (store.root / ".assets" / ("f" * 64)).write_bytes(b"declared by nobody")

    store.prune_assets()

    assert [p.name for p in (store.root / ".assets").iterdir()] == [kept["sha256"]]


def test_a_bundle_with_an_unreadable_manifest_does_not_stop_the_pruning(
    store: ExtensionStore, tmp_path: Path
):
    kept = asset("model", "any", b"kept bytes", file="model.onnx")
    store.add(bundle(tmp_path, assets=[kept]))
    store.add(bundle(tmp_path, "other", id="acme.other"))
    (store.path_for("acme.other") / "manifest.json").write_text("{ broken")
    (store.root / ".assets").mkdir()
    (store.root / ".assets" / kept["sha256"]).write_bytes(b"kept bytes")
    (store.root / ".assets" / ("f" * 64)).write_bytes(b"declared by nobody")

    store.prune_assets()

    assert [p.name for p in (store.root / ".assets").iterdir()] == [kept["sha256"]]


def test_pruning_a_store_with_no_assets_does_nothing(store: ExtensionStore):
    store.prune_assets()
    assert not store.root.exists()
