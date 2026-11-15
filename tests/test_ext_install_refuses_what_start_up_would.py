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
