"""``igni ext doctor`` reads the manifest against the directory it sits in.

A manifest can be valid and still not work from where it is: a page with
no ``ui`` directory, a wheelhouse that is not there, an endpoint whose
grants nobody could deliver. Each is reported before anything is started,
and a *problem* (which fails the command) is kept apart from a *note*
(which only says something worth knowing) — the first version failed
``doctor`` on the output of ``ext init``.

Everything runs through the real command with ``--no-start``, so what is
asserted is what an author reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from igni_core.cli.ext import ext
from igni_core.cli.ext_doctor import Findings
from igni_core.cli.ext_manifest_check import ManifestCheck
from igni_core.extensions.manifest import ExtensionManifest

PAGE = {"kind": "acme.tickets.board", "label": "Board"}


def _bundle(tmp_path: Path, **fields: object) -> Path:
    bundle = tmp_path / "b"
    bundle.mkdir()
    manifest: dict[str, object] = {
        "id": "acme.tickets",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
    }
    manifest.update(fields)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def _dev(command: str = "server") -> dict[str, object]:
    return {"dev": {"command": [command]}}


def _python() -> dict[str, object]:
    return {
        "python": {
            "package": "acme-tickets",
            "entry": "acme-tickets",
            "mcp": {"transport": "stdio"},
        }
    }


def _doctor(bundle: Path):
    return CliRunner().invoke(ext, ["doctor", str(bundle), "--no-start"])


def test_a_development_bundle_is_noted_and_passes(tmp_path):
    result = _doctor(_bundle(tmp_path, **_dev()))
    assert result.exit_code == 0, result.output
    assert "development packaging" in result.output
    assert "no problems found" in result.output
    assert "!" not in result.output


def test_a_sidecar_is_a_problem_because_igni_cannot_start_images(tmp_path):
    result = _doctor(
        _bundle(
            tmp_path,
            sidecar={"image": "registry.example/acme:1", "mcp": {"transport": "stdio"}},
        )
    )
    assert result.exit_code != 0
    assert "packaged as a sidecar image (registry.example/acme:1)" in result.output
    assert "cannot start container images yet" in result.output
    assert "1 problem(s) found" in result.output


def test_python_with_no_wheelhouse_and_nothing_to_build_is_a_problem(tmp_path):
    result = _doctor(_bundle(tmp_path, **_python()))
    assert result.exit_code != 0
    assert "python packaging with no wheelhouse at 'wheels'" in result.output
    assert "no pyproject.toml or setup.py" in result.output


@pytest.mark.parametrize("build_file", ["pyproject.toml", "setup.py"])
def test_python_with_a_buildable_source_and_no_wheels_is_a_note(tmp_path, build_file):
    bundle = _bundle(tmp_path, **_python())
    (bundle / build_file).write_text("")
    result = _doctor(bundle)
    assert result.exit_code == 0, result.output
    assert "no wheelhouse at 'wheels'" in result.output
    assert "builds one from this directory" in result.output


def test_an_empty_wheelhouse_counts_as_none(tmp_path):
    bundle = _bundle(tmp_path, **_python())
    (bundle / "wheels").mkdir()
    result = _doctor(bundle)
    assert result.exit_code != 0
    assert "python packaging with no wheelhouse" in result.output


def test_a_wheelhouse_with_wheels_raises_neither_problem_nor_note(tmp_path):
    bundle = _bundle(tmp_path, **_python())
    (bundle / "wheels").mkdir()
    (bundle / "wheels" / "acme_tickets-0.1.0-py3-none-any.whl").write_text("")
    result = _doctor(bundle)
    assert result.exit_code == 0, result.output
    assert "no wheelhouse" not in result.output
    assert "no problems found" in result.output


def test_a_declared_wheelhouse_name_is_the_one_looked_in(tmp_path):
    fields = _python()
    fields["python"]["wheels"] = "dist"  # type: ignore[index]
    bundle = _bundle(tmp_path, **fields)
    (bundle / "wheels").mkdir()
    (bundle / "wheels" / "a.whl").write_text("")
    result = _doctor(bundle)
    assert result.exit_code != 0
    assert "no wheelhouse at 'dist'" in result.output


def test_pages_without_a_ui_directory_are_a_problem(tmp_path):
    result = _doctor(_bundle(tmp_path, **_dev(), contributes={"pages": [PAGE]}))
    assert result.exit_code != 0
    assert "contributes pages and the bundle has no 'ui' directory" in result.output


def test_a_ui_that_is_a_file_is_not_a_directory(tmp_path):
    bundle = _bundle(tmp_path, **_dev(), contributes={"pages": [PAGE]})
    (bundle / "ui").write_text("")
    result = _doctor(bundle)
    assert result.exit_code != 0
    assert "no 'ui' directory" in result.output


def test_pages_with_a_ui_directory_pass_and_say_only_a_single_user_backend_shows_them(tmp_path):
    bundle = _bundle(tmp_path, **_dev(), contributes={"pages": [PAGE]})
    (bundle / "ui").mkdir()
    result = _doctor(bundle)
    assert result.exit_code == 0, result.output
    assert "contributes pages, which only a single-user backend shows" in result.output
    assert "no 'ui' directory" not in result.output


def test_a_process_extension_is_noted_as_single_user_only(tmp_path):
    result = _doctor(_bundle(tmp_path, **_dev()))
    assert "installs only on a single-user backend" in result.output
    assert "One process cannot serve a group" in result.output


ENDPOINT = {"endpoint": {"url": "https://tickets.acme.test/mcp"}}


def test_an_endpoint_with_no_egress_is_a_problem_and_is_not_a_process_note(tmp_path):
    result = _doctor(_bundle(tmp_path, **ENDPOINT))
    assert result.exit_code != 0
    assert "has an endpoint and declares no egress" in result.output
    # An endpoint is somebody else's process: no "runs as a process" note.
    assert "installs only on a single-user backend" not in result.output


def test_an_endpoint_with_egress_and_no_other_grant_is_clean(tmp_path):
    result = _doctor(_bundle(tmp_path, **ENDPOINT, permissions={"egress": ["tickets.acme.test"]}))
    assert result.exit_code == 0, result.output
    assert "no problems found" in result.output


@pytest.mark.parametrize("level", ["read", "write"])
def test_an_endpoint_asking_for_the_workspace_is_a_problem(tmp_path, level):
    result = _doctor(
        _bundle(
            tmp_path,
            **ENDPOINT,
            permissions={"egress": ["tickets.acme.test"], "workspace": level},
        )
    )
    assert result.exit_code != 0
    assert f"declares workspace: {level} and is an endpoint" in result.output
    assert "the grant could never be delivered" in result.output


def test_an_endpoint_asking_for_secrets_is_a_problem(tmp_path):
    result = _doctor(
        _bundle(
            tmp_path,
            **ENDPOINT,
            permissions={"egress": ["tickets.acme.test"], "secrets": ["api_key"]},
        )
    )
    assert result.exit_code != 0
    assert "declares secrets ['api_key'] and is an endpoint" in result.output


def test_an_endpoint_with_several_faults_reports_them_all(tmp_path):
    result = _doctor(
        _bundle(tmp_path, **ENDPOINT, permissions={"workspace": "read", "secrets": ["k"]})
    )
    assert result.exit_code != 0
    assert "3 problem(s) found" in result.output


@pytest.mark.parametrize("command", ["./server", "../server", "./bin/run"])
def test_a_local_dev_command_that_is_not_in_the_bundle_is_a_problem(tmp_path, command):
    result = _doctor(_bundle(tmp_path, **_dev(command)))
    assert result.exit_code != 0
    assert f"its dev command {command} is not in the bundle" in result.output


def test_a_local_dev_command_that_is_in_the_bundle_passes(tmp_path):
    bundle = _bundle(tmp_path, **_dev("./server"))
    (bundle / "server").write_text("")
    result = _doctor(bundle)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("command", ["python", "/usr/bin/env", "bin/run"])
def test_a_path_or_absolute_dev_command_is_not_checked_against_the_bundle(tmp_path, command):
    result = _doctor(_bundle(tmp_path, **_dev(command)))
    assert result.exit_code == 0, result.output
    assert "is not in the bundle" not in result.output


def test_a_bare_wildcard_rpc_grant_is_refused_by_the_schema_before_the_check(tmp_path):
    result = _doctor(_bundle(tmp_path, **_dev(), permissions={"rpc": ["*"]}))
    assert result.exit_code != 0
    assert "permissions.rpc" in result.output


def test_a_wildcard_that_got_past_the_schema_is_still_a_problem(tmp_path):
    """The check is the second line of defence: a manifest built in code,
    not read from a file, is not run through the schema's pattern."""
    manifest = ExtensionManifest.model_validate(
        {
            "id": "acme.tickets",
            "version": "0.1.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["server"]},
        }
    )
    permissions = manifest.permissions.model_copy(update={"rpc": ["ext.acme.tickets.lookup", "*"]})
    manifest = manifest.model_copy(update={"permissions": permissions})
    findings = Findings()
    ManifestCheck(manifest, tmp_path).check(findings)
    assert findings.problems == ["permissions.rpc contains '*', which grants every method"]


def test_a_namespaced_wildcard_is_not_the_bare_one(tmp_path):
    result = _doctor(_bundle(tmp_path, **_dev(), permissions={"rpc": ["ext.acme.tickets.*"]}))
    assert result.exit_code == 0, result.output
    assert "which grants every method" not in result.output


def test_several_problems_are_all_listed_and_counted(tmp_path):
    result = _doctor(
        _bundle(
            tmp_path,
            **_dev("./missing"),
            contributes={"pages": [PAGE]},
        )
    )
    assert result.exit_code != 0
    assert "2 problem(s) found" in result.output
    assert result.output.count("  ! ") == 2
