"""What one extension's sandbox plan grants, and what it leaves out.

The plan is the one list every platform's sandbox is written from, so
its edges are the sandbox's edges: a glob that climbs out of the
workspace, a link that leads out of it, a Python layout that is not the
usual one, a service address that is not quite a URL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from igni_core.core.network_policy import NetworkPolicy
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.sandbox import SandboxPlan

pytestmark = pytest.mark.extension_sandbox


def manifest(**permissions) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.probe",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": "Probe",
            "trust": "unsigned",
            "dev": {"command": ["python", "probe.py"]},
            "permissions": permissions,
        }
    )


def build(tmp_path: Path, m: ExtensionManifest | None = None, **kw) -> SandboxPlan:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    args = dict(
        command=sys.executable,
        env={},
        cwd=None,
        bundle=tmp_path / "bundle",
        data_dir=None,
        project_dir=None,
        service_url=None,
        development=False,
        network=NetworkPolicy(),
        run_dir=run,
    )
    args.update(kw)
    return SandboxPlan.build(m or manifest(), **args)


def tree(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


# ── the files ─────────────────────────────────────────────────────────


def test_only_the_data_and_temp_directories_are_writable_and_all_of_them_readable(tmp_path):
    bundle, data, cwd = tmp_path / "bundle", tmp_path / "data", tmp_path / "cwd"
    tree(bundle, data, cwd)
    plan = build(tmp_path, bundle=bundle, data_dir=data, cwd=cwd, command="/nonexistent/tool")
    run = (tmp_path / "run").resolve()
    assert set(plan.write) == {run / "tmp", tmp_path / "run" / "tmp", data.resolve(), data}
    assert bundle.resolve() in plan.read and cwd.resolve() in plan.read
    assert all(p in plan.read for p in plan.write)
    assert bundle.resolve() not in plan.write and cwd.resolve() not in plan.write
    assert len(plan.read) == len(set(plan.read)) and len(plan.write) == len(set(plan.write))


def test_without_a_data_directory_only_the_temp_directory_is_writable(tmp_path):
    plan = build(tmp_path, command="/nonexistent/tool")
    assert set(plan.write) <= {tmp_path / "run" / "tmp", (tmp_path / "run" / "tmp").resolve()}
    assert plan.write


def test_the_temp_and_home_directories_are_inside_the_run_directory(tmp_path):
    plan = build(tmp_path)
    assert plan.temp_dir == tmp_path / "run" / "tmp"
    assert plan.home_dir == tmp_path / "run" / "tmp" / "home"


def test_a_plan_without_a_run_directory_has_no_temp_or_home():
    plan = SandboxPlan(extension_id="acme.probe")
    with pytest.raises(AssertionError):
        _ = plan.temp_dir
    with pytest.raises(AssertionError):
        _ = plan.home_dir


# ── the workspace ─────────────────────────────────────────────────────


def test_a_workspace_granted_with_no_globs_is_the_whole_project(tmp_path):
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    assert SandboxPlan.workspace_paths(ws, []) == [ws]


def test_a_glob_that_starts_with_a_pattern_grants_the_workspace_root(tmp_path):
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    for glob in ("**", "*.py", "**/*.md", "[ab]/x"):
        assert SandboxPlan.workspace_paths(ws, [glob]) == [ws], glob


def test_a_glob_is_granted_up_to_its_first_pattern_and_the_same_prefix_once(tmp_path):
    ws = (tmp_path / "ws").resolve()
    tree(ws / "src" / "app", ws / "docs")
    paths = SandboxPlan.workspace_paths(ws, ["src/app/**/*.py", "docs/?.md", "src/app/*.txt"])
    assert paths == [ws / "src" / "app", ws / "docs"]


def test_a_glob_that_climbs_out_of_the_workspace_is_dropped_even_after_going_in(tmp_path):
    ws = (tmp_path / "ws").resolve()
    tree(ws / "docs", tmp_path / "secret")
    assert SandboxPlan.workspace_paths(ws, ["docs/../../secret/*"]) == []
    assert SandboxPlan.workspace_paths(ws, ["docs/../*.md"]) == [ws]
    assert SandboxPlan.workspace_paths(ws, [str(tmp_path / "secret" / "*")]) == []


def test_a_link_inside_the_workspace_that_leads_out_is_not_granted(tmp_path):
    ws = (tmp_path / "ws").resolve()
    secret = (tmp_path / "secret").resolve()
    tree(ws, secret)
    os.symlink(secret, ws / "shortcut")
    assert SandboxPlan.workspace_paths(ws, ["shortcut/**"]) == []
    inside = ws / "real"
    inside.mkdir()
    os.symlink(inside, ws / "alias")
    assert SandboxPlan.workspace_paths(ws, ["alias/*"]) == [inside]


def test_a_workspace_that_is_itself_a_link_is_granted_as_it_and_as_it_resolves(tmp_path):
    real = (tmp_path / "real").resolve()
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    assert SandboxPlan.workspace_paths(link, []) == [link, real]
    assert SandboxPlan.workspace_paths(link, ["*.py"]) == [real]


def test_workspace_modes_through_the_whole_plan(tmp_path):
    ws = (tmp_path / "ws").resolve()
    tree(ws / "docs", ws / "src")
    scoped = build(tmp_path, manifest(workspace="write", workspacePaths=["docs/*"]), project_dir=ws)
    assert ws / "docs" in scoped.write and ws / "docs" in scoped.read
    assert ws not in scoped.read and ws / "src" not in scoped.read
    reading = build(tmp_path, manifest(workspace="read", workspacePaths=["docs/*"]), project_dir=ws)
    assert ws / "docs" in reading.read and ws / "docs" not in reading.write
    # No project open: nothing to grant, however the manifest asks.
    assert not any("ws" in p.parts for p in build(tmp_path, manifest(workspace="write")).read)


# ── the interpreter's tree ────────────────────────────────────────────


def test_a_tool_outside_a_virtual_environment_grants_the_tree_above_its_bin(tmp_path):
    prefix = (tmp_path / "opt" / "tool").resolve()
    tree(prefix / "bin")
    exe = prefix / "bin" / "tool"
    exe.write_text("")
    assert SandboxPlan.runtime_paths(str(exe), {}) == [prefix]


def test_a_relative_command_is_found_on_the_path_the_extension_will_have(tmp_path):
    prefix = (tmp_path / "opt" / "tool").resolve()
    tree(prefix / "bin")
    exe = prefix / "bin" / "mytool"
    exe.write_text("")
    exe.chmod(0o755)
    assert SandboxPlan.runtime_paths("mytool", {"PATH": str(prefix / "bin")}) == [prefix]


def test_a_relative_command_is_found_on_the_process_path_when_the_env_has_none(
    tmp_path, monkeypatch
):
    prefix = (tmp_path / "opt" / "tool").resolve()
    tree(prefix / "bin")
    exe = prefix / "bin" / "mytool"
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(prefix / "bin"))
    assert SandboxPlan.runtime_paths("mytool", {}) == [prefix]
    assert SandboxPlan.runtime_paths("mytool", {"PATH": ""}) == [prefix]


def test_a_command_that_is_nowhere_grants_nothing(tmp_path):
    assert SandboxPlan.runtime_paths("no-such-tool-anywhere", {"PATH": str(tmp_path)}) == []


def test_a_symlinked_interpreter_grants_where_it_lies_and_where_it_leads(tmp_path):
    real = (tmp_path / "real").resolve()
    link = tmp_path / "linked"
    tree(real / "bin", link / "bin")
    (real / "bin" / "tool").write_text("")
    os.symlink(real / "bin" / "tool", link / "bin" / "tool")
    assert SandboxPlan.runtime_paths(str(link / "bin" / "tool"), {}) == [link, real]


def venv_with(tmp_path: Path, cfg_text: bytes | None) -> Path:
    venv = tmp_path / "venv"
    tree(venv / "bin")
    (venv / "bin" / "python").write_text("")
    if cfg_text is not None:
        (venv / "pyvenv.cfg").write_bytes(cfg_text)
    return venv


@pytest.mark.parametrize(
    "cfg",
    [b"", b"version = 3.12\n", b"home =\n", b"home =    \n", b"no equals sign\n"],
    ids=["empty", "no home", "blank home", "spaces home", "not a setting"],
)
def test_a_pyvenv_config_without_a_usable_home_grants_only_the_environment(tmp_path, cfg):
    venv = venv_with(tmp_path, cfg)
    assert SandboxPlan.runtime_paths(str(venv / "bin" / "python"), {}) == [venv.resolve()]


def test_a_pyvenv_config_that_is_not_text_grants_only_the_environment(tmp_path):
    venv = venv_with(tmp_path, b"home = \xff\xfe/broken\n")
    assert SandboxPlan.runtime_paths(str(venv / "bin" / "python"), {}) == [venv.resolve()]


def test_the_home_a_pyvenv_config_names_is_granted_once_with_its_own_prefix(tmp_path):
    base = (tmp_path / "py").resolve()
    tree(base / "bin")
    venv = venv_with(tmp_path, f"version = 3\nhome = {base / 'bin'}\n".encode())
    assert SandboxPlan.runtime_paths(str(venv / "bin" / "python"), {}) == [venv.resolve(), base]


def test_a_windows_pyvenv_home_is_the_prefix_itself(tmp_path):
    base = (tmp_path / "py312").resolve()
    tree(base)
    venv = venv_with(tmp_path, f"home = {base}\n".encode())
    assert SandboxPlan.runtime_paths(str(venv / "bin" / "python"), {}) == [venv.resolve(), base]


def test_a_windows_interpreter_never_grants_the_directory_above_its_own(tmp_path):
    base = tmp_path / "Python312"
    tree(base)
    (base / "python.exe").write_text("")
    assert SandboxPlan.runtime_paths(str(base / "python.exe"), {}) == [base.resolve()]
    scripts = tmp_path / "Tool" / "Scripts"
    tree(scripts)
    (scripts / "tool.EXE").write_text("")
    assert SandboxPlan.runtime_paths(str(scripts / "tool.EXE"), {}) == [scripts.parent.resolve()]
    assert tmp_path.resolve() not in SandboxPlan.runtime_paths(str(base / "python.exe"), {})


# ── the hosts ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "development", "entries"),
    [
        ("https://svc.acme.com", False, ["svc.acme.com:443"]),
        ("HTTPS://SVC.ACME.COM:8443/path", False, ["svc.acme.com:8443"]),
        ("  https://svc.acme.com  ", False, ["svc.acme.com:443"]),
        ("http://devbox", True, ["devbox:80"]),
        ("http://devbox:9000", True, ["devbox:9000"]),
        ("https://[::1]:8443", False, ["[::1]:8443"]),
        ("https://svc.acme.com:99999", False, ["svc.acme.com:443"]),
        ("https://svc.acme.com:notaport", False, ["svc.acme.com:443"]),
        ("ws://svc.acme.com:80", True, []),
        ("ftp://svc.acme.com", True, []),
        ("https://", False, []),
        ("svc.acme.com", False, []),
        ("", False, []),
        (None, False, []),
    ],
)
def test_a_service_address_is_granted_by_its_host_and_port_or_not_at_all(
    tmp_path, url, development, entries
):
    plan = build(tmp_path, service_url=url, development=development)
    assert plan.hosts.entries == entries


@pytest.mark.parametrize("url", ["ws://svc.acme.com", "https://", "svc.acme.com", "", None])
def test_only_a_plain_http_service_is_recorded_as_one(tmp_path, url):
    assert build(tmp_path, service_url=url).plain_http_service is None


def test_a_plain_http_service_is_recorded_as_written_without_its_padding(tmp_path):
    plan = build(tmp_path, service_url="  HTTP://svc.acme.com:81  ")
    assert plan.plain_http_service == "HTTP://svc.acme.com:81"
    assert plan.hosts.entries == []


def test_a_malformed_egress_entry_is_skipped_and_the_rest_are_kept(tmp_path):
    m = manifest()
    # The manifest refuses these; a plan built from a manifest that was not validated skips them.
    object.__setattr__(
        m.permissions,
        "egress",
        ["", "   ", "host:notaport", ":443", "good.acme.com:8443", "other.acme.com"],
    )
    plan = build(tmp_path, m)
    assert plan.hosts.entries == ["good.acme.com:8443", "other.acme.com:443"]
    assert plan.withheld == []


def test_an_air_gap_withholds_a_service_outside_it_and_records_what_was_withheld(tmp_path):
    network = NetworkPolicy(air_gapped=True, internal_hosts=["*.acme.internal"])
    plan = build(
        tmp_path,
        manifest(egress=["inside.acme.internal:8443", "outside.example.com:8443"]),
        service_url="https://svc.example.com",
        network=network,
    )
    assert plan.hosts.entries == ["inside.acme.internal:8443"]
    assert plan.withheld == ["svc.example.com:443", "outside.example.com:8443"]


# ── the loopback ports ────────────────────────────────────────────────


def test_only_loopback_urls_with_a_valid_port_are_ports_igni_serves_it_on(tmp_path):
    env = {
        "A": "ws://127.0.0.1:41001/v1",
        "B": "http://localhost:41002/token",
        "C": "http://[::1]:41003/x",
        "D": "http://127.0.0.1:99999/bad-port",
        "E": "http://127.0.0.1/no-port",
        "F": "http://10.0.0.5:41004/not-loopback",
        "G": "https://api.acme.com:443/elsewhere",
        "H": "not a url 127.0.0.1:41005",
        "I": "http://127.0.0.1:41001/again",
    }
    plan = build(tmp_path, env=env, service_url="http://127.0.0.1:41006/svc")
    assert plan.loopback_ports == [41001, 41002, 41003, 41006]


def test_no_loopback_url_means_no_port(tmp_path):
    assert build(tmp_path, env={"A": "plain"}).loopback_ports == []
