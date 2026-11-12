"""What ``igni ext list``, ``remove``, ``sync`` and ``init`` print and refuse.

These work against the store on disk, not a running backend, so each is run
for real against a store in a temporary directory. Only igni server is
replaced, by the same in-process stand-in the other sync tests use.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.extensions.distribution.once import OneShotSync
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.store import ExtensionStore
from tests.extension_distribution_helpers import (
    API_URL,
    SESSION,
    FakeIgni,
    Listed,
    Publisher,
    bundle_archive,
    manifest,
)

# ``igni_core.cli`` re-exports the ``ext`` group under the module's name.
ext_cli = importlib.import_module("igni_core.cli.ext")


def _dev_bundle(tmp_path: Path, extension_id: str = "acme.notes", name: str = "mine") -> Path:
    bundle = tmp_path / name
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "id": extension_id,
                "version": "0.0.1",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
            }
        )
    )
    return bundle


def _run(*args: str):
    return CliRunner().invoke(ext_cli.ext, list(args))


@pytest.fixture
def store(tmp_path: Path) -> ExtensionStore:
    return ExtensionStore(tmp_path / "store")


def _distribute(store: ExtensionStore, tmp_path: Path, extension_id: str = "acme.notes") -> None:
    store.add(
        _dev_bundle(tmp_path, extension_id, name="d-" + extension_id),
        distribution=DistributionRecord(
            extension_id=extension_id,
            version="0.0.1",
            sha256="0" * 64,
            service_url="https://notes.example.com",
        ),
    )


# ── list ───────────────────────────────────────────────────────────


def test_list_says_where_nothing_is_installed(store):
    result = _run("list", "--root", str(store.root))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"nothing installed in {store.root}"


def test_list_says_a_bundle_it_cannot_read_and_goes_on_to_the_next(store, tmp_path):
    store.add(_dev_bundle(tmp_path, "acme.good", name="good"))
    broken = store.root / "acme.broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json")
    result = _run("list", "--root", str(store.root))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("acme.broken") and "unreadable — " in line for line in lines)
    assert any(
        line.startswith("acme.good") and "0.0.1" in line and "development install" in line
        for line in lines
    )


def test_list_names_the_service_a_distributed_bundle_came_from(store, tmp_path):
    _distribute(store, tmp_path)
    result = _run("list", "--root", str(store.root))
    assert "from https://notes.example.com" in result.output
    assert "development install" not in result.output


def test_a_tilde_in_root_is_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    bundle = _dev_bundle(tmp_path)
    installed = _run("install", str(bundle), "--root", "~/igni-store")
    assert installed.exit_code == 0, installed.output
    assert (tmp_path / "home" / "igni-store" / "acme.notes" / "manifest.json").is_file()
    assert not (tmp_path / "~").exists()
    listed = _run("list", "--root", "~/igni-store")
    assert "acme.notes" in listed.output


# ── remove ─────────────────────────────────────────────────────────


def test_removing_what_is_not_installed_fails_saying_so(store):
    result = _run("remove", "acme.nothing", "--root", str(store.root))
    assert result.exit_code != 0
    assert "acme.nothing is not installed" in result.output
    assert "Traceback" not in result.output


def test_removing_a_development_install_says_when_it_stops_and_nothing_else(store, tmp_path):
    store.add(_dev_bundle(tmp_path))
    result = _run("remove", "acme.notes", "--root", str(store.root))
    assert result.exit_code == 0, result.output
    assert "removed acme.notes" in result.output
    assert "It stops the next time igni's backend starts" in result.output
    assert "next sync" not in result.output
    assert not store.path_for("acme.notes").exists()


def test_removing_a_distributed_one_says_it_returns_at_the_next_sync(store, tmp_path):
    _distribute(store, tmp_path)
    result = _run("remove", "acme.notes", "--root", str(store.root))
    assert result.exit_code == 0, result.output
    assert "It came from https://notes.example.com through igni" in result.output
    assert "installed again at the next sync while that service is approved" in result.output
    assert "It stops the next time igni's backend starts" in result.output
    assert store.provenance.of("acme.notes") is None


def test_an_id_that_is_a_path_is_refused_as_a_message_not_a_traceback(store):
    result = _run("remove", "../escape", "--root", str(store.root))
    assert result.exit_code != 0
    assert "is not a usable directory name" in result.output
    assert "Traceback" not in result.output


# ── init ───────────────────────────────────────────────────────────


def test_init_writes_into_a_directory_that_exists_and_is_empty(tmp_path):
    target = tmp_path / "mine"
    target.mkdir()
    result = _run("init", str(target))
    assert result.exit_code == 0, result.output
    assert (target / "manifest.json").is_file()
    assert f"created {target} (local.mine)" in result.output


def test_init_into_a_directory_with_a_file_in_it_is_refused_and_leaves_it_alone(tmp_path):
    target = tmp_path / "mine"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    result = _run("init", str(target))
    assert result.exit_code != 0
    assert f"{target} is not empty" in result.output
    assert sorted(path.name for path in target.iterdir()) == ["keep.txt"]


def test_init_says_what_to_run_next_for_a_dev_extension(tmp_path):
    target = tmp_path / "mine"
    result = _run("init", str(target))
    tail = result.output.split("next:\n", 1)[1]
    assert f"igni ext doctor {target}\n" in tail
    assert f"IGNI_DEV_EXTENSIONS=1 igni ext install {target}" in tail
    assert "uv sync" not in tail


def test_init_says_what_to_run_next_for_a_service_extension(tmp_path):
    target = tmp_path / "mine"
    result = _run("init", str(target), "--service")
    assert result.exit_code == 0, result.output
    tail = result.output.split("next:\n", 1)[1]
    assert f"cd {target} && uv sync && uv run pytest" in tail
    assert f"igni ext doctor {target / 'extension'}" in tail
    assert f"{target / 'README.md'}" in tail
    assert "igni ext install" not in tail


def test_init_lists_every_file_it_wrote(tmp_path):
    target = tmp_path / "mine"
    result = _run("init", str(target))
    written = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
    head = result.output.split("\nnext:", 1)[0].splitlines()
    assert head[0] == f"created {target} (local.mine)"
    assert {line.strip() for line in head[1:] if line.strip()} == written


def test_an_unusable_id_is_explained_and_the_directory_is_not_created(tmp_path):
    result = _run("init", str(tmp_path / "mine"), "--id", "NotReverseDns")
    assert result.exit_code != 0
    assert "'NotReverseDns' is not a usable extension id" in result.output
    assert "reverse-DNS, lower case and without hyphens" in result.output
    assert not (tmp_path / "mine").exists()


# ── sync ───────────────────────────────────────────────────────────


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.igni = FakeIgni()
        self.publisher = Publisher("RSA")
        self.credentials = tmp_path / "credentials.json"
        self.store = ExtensionStore(tmp_path / "store")
        self.settings = SimpleNamespace(
            api_url=API_URL,
            portal_url="",
            air_gapped=False,
            auth=SimpleNamespace(credentials_file=str(self.credentials)),
            storage=SimpleNamespace(data_dir=str(tmp_path / "data")),
            group_policy=SimpleNamespace(trusted_keys=[], required=False),
        )
        transport = self.igni.transport()
        monkeypatch.setattr(
            ext_cli,
            "OneShotSync",
            lambda store, settings: OneShotSync(store, settings, transport=transport),
        )
        monkeypatch.setattr(PolicySettingsLoader, "load", lambda _self: self.settings)
        CredentialsStore(str(self.credentials)).save(Credentials.new(SESSION, "ada@acme.test"))

    def publish(self, **kwargs) -> None:
        raw = manifest()
        self.igni.publish(
            Listed(
                bundle_archive(raw, {"wheels/placeholder.txt": b"x"}),
                self.publisher,
                **kwargs,
            )
        )

    def sync(self):
        return _run("sync", "--root", str(self.store.root))


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


def test_sync_says_what_it_removed_and_that_a_restart_is_needed(world):
    world.publish()
    assert world.sync().exit_code == 0
    world.igni.withdraw("acme.notes")
    result = world.sync()
    assert result.exit_code == 0, result.output
    assert "removed    acme.notes (no longer listed)" in result.output
    assert "What changed applies the next time igni's backend starts" in result.output
    assert not world.store.path_for("acme.notes").exists()


def test_sync_says_where_an_installed_one_came_from(world):
    world.publish()
    result = world.sync()
    assert "installed  acme.notes 1.0.0 from https://notes.example.com" in result.output
    assert f"synced with {API_URL} (group Legal)" in result.output


def test_a_second_sync_changes_nothing_and_asks_for_no_restart(world):
    world.publish()
    world.sync()
    result = world.sync()
    assert "unchanged  acme.notes" in result.output
    assert "What changed applies" not in result.output


def test_a_bundle_that_does_not_verify_is_a_failed_row_and_a_failed_exit(world):
    world.publish(signature="not-a-signature")
    result = world.sync()
    assert result.exit_code != 0
    assert "failed     acme.notes: " in result.output
    assert "1 extension(s) could not be installed" in result.output
    assert not world.store.path_for("acme.notes").exists()


def test_one_bad_bundle_does_not_stop_the_good_one(world):
    world.publish(signature="not-a-signature", extension_id="acme.bad")
    world.igni.publish(
        Listed(
            bundle_archive(manifest(), {"wheels/placeholder.txt": b"x"}),
            world.publisher,
        )
    )
    result = world.sync()
    assert result.exit_code != 0
    assert "failed     acme.bad" in result.output
    assert "installed  acme.notes" in result.output
    assert world.store.path_for("acme.notes").is_dir()


# ── the settings the commands read ─────────────────────────────────


@pytest.mark.parametrize("argv", [("sync",), ("install", "SOURCE")])
def test_a_required_policy_with_no_verified_pack_is_a_message_not_a_traceback(
    tmp_path, monkeypatch, argv
):
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")

    def unavailable(_self):
        raise GroupPolicyUnavailable("group policy is required and no verified pack is on disk")

    monkeypatch.setattr(PolicySettingsLoader, "load", unavailable)
    source = _dev_bundle(tmp_path)
    args = [str(source) if arg == "SOURCE" else arg for arg in argv]
    result = _run(*args, "--root", str(tmp_path / "store"))
    assert result.exit_code != 0
    assert "group policy is required and no verified pack is on disk" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_installing_over_an_installed_id_needs_replace_and_replace_upgrades_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    monkeypatch.setattr(
        PolicySettingsLoader,
        "load",
        lambda _self: SimpleNamespace(air_gapped=False, network=SimpleNamespace(internal_hosts=[])),
    )
    root = tmp_path / "store"
    first = _dev_bundle(tmp_path, name="one")
    assert _run("install", str(first), "--root", str(root)).exit_code == 0
    second = _dev_bundle(tmp_path, name="two")
    data = json.loads((second / "manifest.json").read_text())
    data["version"] = "0.0.2"
    (second / "manifest.json").write_text(json.dumps(data))
    refused = _run("install", str(second), "--root", str(root))
    assert refused.exit_code != 0
    upgraded = _run("install", str(second), "--root", str(root), "--replace")
    assert upgraded.exit_code == 0, upgraded.output
    assert ExtensionStore(root).read(root / "acme.notes").version == "0.0.2"
