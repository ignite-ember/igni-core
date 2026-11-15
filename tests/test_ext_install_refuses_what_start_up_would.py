"""``igni ext install`` refuses a bundle the next start-up would refuse.

The contract range (campaign R236) and an ``own-agents`` audience with
no plugin (campaign R276) were checked only by the backend's install
preflight at start-up: ``igni ext install`` stored the bundle with rc 0,
``igni ext list`` called it installed, and it never started.
EXTENSIBILITY.md §7: "validate at install not at use".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from igni_core.cli.ext import ext

_BASE = {
    "id": "local.c1",
    "version": "0.1.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "dev": {"command": ["python", "server.py"]},
}


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    store = tmp_path / "store"

    def run(manifest: dict, *extra: str):
        bundle = tmp_path / "bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        (bundle / "server.py").write_text("")
        result = CliRunner().invoke(ext, ["install", str(bundle), *extra, "--root", str(store)])
        return result, store

    return run


@pytest.mark.parametrize("contract", [">=2.0 <3", "<1.0", ">=1.5 <2", "banana"])
def test_a_contract_that_leaves_out_this_igni_is_refused_and_not_stored(install, contract):
    result, store = install({**_BASE, "contract": contract})

    assert result.exit_code != 0
    assert "contract" in result.output
    assert not (store / "local.c1").exists()


def test_a_replace_with_a_bad_contract_keeps_the_installed_one(install):
    good, store = install(_BASE)
    assert good.exit_code == 0, good.output

    bad, _ = install({**_BASE, "contract": ">=2.0 <3"}, "--replace")

    assert bad.exit_code != 0
    manifest = json.loads((store / "local.c1" / "manifest.json").read_text())
    assert manifest["contract"] == ">=1.0 <2"


def test_own_agents_with_no_plugin_is_refused(install):
    result, store = install({**_BASE, "permissions": {"toolAudience": "own-agents"}})

    assert result.exit_code != 0
    assert "own-agents" in result.output
    assert not (store / "local.c1").exists()


def test_doctor_reports_own_agents_with_no_plugin(tmp_path, monkeypatch):
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps({**_BASE, "permissions": {"toolAudience": "own-agents"}})
    )
    (bundle / "server.py").write_text("")

    result = CliRunner().invoke(ext, ["doctor", "--no-start", str(bundle)])

    assert result.exit_code != 0
    assert "own-agents" in result.output


def test_a_good_bundle_still_installs(install):
    result, store = install(_BASE)
    assert result.exit_code == 0, result.output
    assert (store / "local.c1" / "manifest.json").exists()


def test_remove_takes_the_carried_plugin_with_it(install, tmp_path):
    """``igni ext remove`` left ~/.igni/plugins/<id>, and its skills,
    agents and hooks stayed live on every later run (campaign R299)."""
    from igni_core.core.config.settings_loader import SettingsLoader
    from igni_core.core.plugins.installer import PluginInstaller

    result, store = install(_BASE)
    assert result.exit_code == 0, result.output
    plugins = PluginInstaller(data_dir=SettingsLoader.load().storage.data_dir).plugins_dir
    (plugins / "local.c1" / ".claude-plugin").mkdir(parents=True)
    (plugins / "local.c1" / ".claude-plugin" / "plugin.json").write_text("{}")
    (plugins / "someone-elses").mkdir()

    removed = CliRunner().invoke(ext, ["remove", "local.c1", "--root", str(store)])

    assert removed.exit_code == 0, removed.output
    assert not (plugins / "local.c1").exists()
    assert (plugins / "someone-elses").is_dir()


def _doctor(tmp_path, manifest: dict):
    bundle = tmp_path / "ind"
    bundle.mkdir(exist_ok=True)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    (bundle / "server.py").write_text("")
    return CliRunner().invoke(ext, ["doctor", "--no-start", str(bundle)])


_INDICATOR = {"id": "local.c1.status", "label": "Kit", "source": "kit_status"}


@pytest.mark.parametrize(
    ("source", "rpc", "says"),
    [
        ("local.c1.kit_status", ["ext.local.c1.*"], "bare name"),
        ("kit_status", ["ext.local.c1.lookup"], "permissions.rpc does not grant"),
    ],
)
def test_doctor_reports_an_indicator_whose_chip_could_never_appear(tmp_path, source, rpc, says):
    """The chip never appeared and nothing but a refused-RPC log line said
    why (campaign R292)."""
    manifest = {
        **_BASE,
        "contributes": {"indicators": [{**_INDICATOR, "source": source}]},
        "permissions": {"rpc": rpc},
    }
    result = _doctor(tmp_path, manifest)
    assert result.exit_code != 0
    assert says in result.output


def test_doctor_accepts_a_bare_source_the_rpc_grant_covers(tmp_path):
    manifest = {
        **_BASE,
        "contributes": {"indicators": [_INDICATOR]},
        "permissions": {"rpc": ["ext.local.c1.*"]},
    }
    result = _doctor(tmp_path, manifest)
    assert "indicator" not in result.output, result.output
