"""What ``igni ext doctor`` tells an author when the process will not talk.

The probe starts the extension the way igni does and asks it for its tools.
Each way that can fail has its own sentence, because the first one an author
meets decides whether they can fix it from the terminal: a command that is
not on PATH, a process that dies without a word, one that dies with a
screenful, a package that will not install.

The probe is the real one against real child processes; only the build of a
python package's environment, which needs an index, is replaced.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from igni_core.cli.ext import ext
from igni_core.cli.ext_doctor import STDERR_LINES, Findings, NameCheck, Probe, ProcessProbe
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.python_env import PythonEnvironmentError, PythonEnvironments

HINTS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

QUIET_SERVER = """
from mcp.server.mcpserver import MCPServer

server = MCPServer("t")

if __name__ == "__main__":
    server.run()
"""

ONE_TOOL_SERVER = """
from mcp.server.mcpserver import MCPServer

server = MCPServer("t")


@server.tool()
def lookup_ticket(ticket_id: str) -> str:
    return ticket_id


if __name__ == "__main__":
    server.run()
"""

# Says where it was started and with what, then dies, so the probe quotes it.
WHERE_SERVER = """
import os
import sys

print("cwd=" + os.path.basename(os.getcwd()), file=sys.stderr)
print("data=" + os.environ.get("IGNI_EXTENSION_DATA", "-"), file=sys.stderr)
print("flavour=" + os.environ.get("FLAVOUR", "-"), file=sys.stderr)
sys.exit(3)
"""


def _manifest(**fields: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "id": "acme.tickets",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": [sys.executable, "server.py"]},
    }
    manifest.update(fields)
    return manifest


def _bundle(tmp_path: Path, server: str, **fields: object) -> Path:
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps(_manifest(**fields)))
    (bundle / "server.py").write_text(server)
    return bundle


def _doctor(bundle: Path, *args: str):
    return CliRunner().invoke(ext, ["doctor", str(bundle), *args])


def _probe(bundle: Path, *, timeout: float = 30.0) -> Probe:
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    return asyncio.run(ProcessProbe(manifest, bundle, timeout=timeout).run())


# ── a process that is not there, or says nothing ───────────────────


def test_a_command_that_is_not_on_path_says_so_and_how_to_fix_it(tmp_path):
    bundle = _bundle(tmp_path, "", dev={"command": ["igni-no-such-binary-anywhere"]})
    result = _doctor(bundle)
    assert result.exit_code != 0
    assert "'igni-no-such-binary-anywhere' was not found on PATH" in result.output
    assert "give its full path in dev.command" in result.output


def test_a_missing_absolute_command_does_not_blame_path(tmp_path):
    missing = str(tmp_path / "bin" / "server")
    bundle = _bundle(tmp_path, "", dev={"command": [missing]})
    probe = _probe(bundle)
    assert probe.error is not None
    assert f"its command {missing!r} was not found" in probe.error
    assert "PATH" not in probe.error


def test_a_process_that_exits_without_a_word_says_it_wrote_nothing(tmp_path):
    probe = _probe(_bundle(tmp_path, "import sys\nsys.exit(0)\n"))
    assert probe.error is not None
    assert probe.error.startswith("it exited before answering MCP (")
    assert probe.error.endswith("; it wrote nothing to stderr")
    assert probe.tools == []


def test_a_dying_process_is_quoted_by_the_last_twelve_lines_of_its_stderr(tmp_path):
    server = (
        "import sys\n"
        "for n in range(1, 31):\n"
        "    print(f'line {n:02d}', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    probe = _probe(_bundle(tmp_path, server))
    assert probe.error is not None
    assert STDERR_LINES == 12
    assert "the last of its stderr:" in probe.error
    assert "      line 30" in probe.error
    assert "line 19" in probe.error
    assert "line 18" not in probe.error


def test_a_few_lines_of_stderr_are_quoted_whole(tmp_path):
    server = (
        "import sys\nprint('one', file=sys.stderr)\nprint('two', file=sys.stderr)\nsys.exit(1)\n"
    )
    probe = _probe(_bundle(tmp_path, server))
    assert probe.error is not None
    assert "the last of its stderr:\n      one\n      two" in probe.error


def test_stderr_that_is_not_utf8_is_still_quoted(tmp_path):
    server = (
        "import sys\n"
        "sys.stderr.buffer.write(b'\\xff\\xfe the cause\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )
    probe = _probe(_bundle(tmp_path, server))
    assert probe.error is not None
    assert "the cause" in probe.error


def test_a_timeout_names_the_seconds_and_what_the_process_said(tmp_path):
    server = "import sys, time\nprint('warming the cache', file=sys.stderr, flush=True)\ntime.sleep(60)\n"
    probe = _probe(_bundle(tmp_path, server), timeout=2.5)
    assert probe.error is not None
    assert "within 2.5s" in probe.error
    assert "warming the cache" in probe.error


# ── how the process is started ─────────────────────────────────────


def test_the_process_starts_in_dev_cwd_with_dev_env_and_a_scratch_data_directory(tmp_path):
    bundle = _bundle(
        tmp_path,
        "",
        dev={
            "command": [sys.executable, str(tmp_path / "where.py")],
            "cwd": "sub",
            "env": {"FLAVOUR": "mint"},
        },
    )
    (bundle / "sub").mkdir()
    (tmp_path / "where.py").write_text(WHERE_SERVER)
    probe = _probe(bundle)
    assert probe.error is not None
    assert "cwd=sub" in probe.error
    assert "flavour=mint" in probe.error
    data = next(line for line in probe.error.splitlines() if "data=" in line).split("data=")[1]
    assert "igni-doctor-" in data
    # Everything it made is gone once the probe returns.
    assert not Path(data).exists()
    assert not Path(data).parent.exists()


def test_a_dev_env_that_sets_the_data_directory_is_left_alone(tmp_path):
    bundle = _bundle(
        tmp_path,
        WHERE_SERVER,
        dev={
            "command": [sys.executable, "server.py"],
            "env": {"IGNI_EXTENSION_DATA": "/the/authors/own"},
        },
    )
    probe = _probe(bundle)
    assert probe.error is not None
    assert "data=/the/authors/own" in probe.error


def test_without_a_dev_cwd_the_bundle_is_the_working_directory(tmp_path):
    probe = _probe(_bundle(tmp_path, WHERE_SERVER))
    assert probe.error is not None
    assert "cwd=b" in probe.error


# ── what it offers ─────────────────────────────────────────────────


def test_a_process_with_no_tools_and_a_manifest_with_none_is_clean(tmp_path):
    result = _doctor(_bundle(tmp_path, QUIET_SERVER))
    assert result.exit_code == 0, result.output
    assert "started it and asked for its tools: 0" in result.output
    assert "each tool's names" not in result.output
    assert "no problems found" in result.output


def test_a_tool_granted_to_the_page_only_is_a_note_not_a_warning(tmp_path):
    bundle = _bundle(
        tmp_path,
        ONE_TOOL_SERVER,
        permissions={"rpc": ["ext.acme.tickets.lookup_ticket"]},
    )
    result = _doctor(bundle)
    assert result.exit_code == 0, result.output
    assert "'lookup_ticket' is for the page only" in result.output
    assert "warning" not in result.output
    assert "(not declared)" in result.output
    assert "(not granted in permissions.rpc)" not in result.output


def test_a_tool_nobody_is_granted_is_flagged_in_the_table_and_warned_about(tmp_path):
    result = _doctor(_bundle(tmp_path, ONE_TOOL_SERVER))
    assert result.exit_code == 0, result.output
    assert "(not declared)" in result.output
    assert "(not granted in permissions.rpc)" in result.output
    assert "nothing can call it" in result.output
    assert "acme.tickets.lookup_ticket with its four hints" in result.output


def test_a_declared_and_granted_tool_has_clean_rows(tmp_path):
    bundle = _bundle(
        tmp_path,
        ONE_TOOL_SERVER,
        permissions={
            "tools": [{"name": "acme.tickets.lookup_ticket", "annotations": HINTS}],
            "rpc": ["ext.acme.tickets.*"],
        },
    )
    result = _doctor(bundle)
    assert result.exit_code == 0, result.output
    assert "(not declared)" not in result.output
    assert "(not granted" not in result.output
    assert "warning" not in result.output


# ── interruption is not an answer ──────────────────────────────────


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_during_the_probe_is_not_swallowed_as_a_failed_probe(
    tmp_path, monkeypatch, raised
):
    async def interrupted(self):  # noqa: ANN001
        raise raised()

    monkeypatch.setattr("mcp.ClientSession.initialize", interrupted)
    bundle = _bundle(tmp_path, QUIET_SERVER)
    with pytest.raises(raised):
        _probe(bundle)


# ── a python package that will not install ─────────────────────────


PYTHON = {
    "python": {"package": "acme-tickets", "entry": "acme-tickets", "mcp": {"transport": "stdio"}}
}


def _python_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "py"
    bundle.mkdir()
    manifest = _manifest()
    del manifest["dev"]
    manifest.update(PYTHON)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def test_a_package_that_will_not_install_says_so(tmp_path, monkeypatch):
    async def refuse(self, manifest, bundle, *, development):  # noqa: ANN001
        raise PythonEnvironmentError("pip said: no matching distribution for libfoo")

    monkeypatch.setattr(PythonEnvironments, "ensure", refuse)
    probe = _probe(_python_bundle(tmp_path))
    assert probe.error == (
        "installing its package failed: pip said: no matching distribution for libfoo"
    )


@pytest.mark.parametrize("name", ["igni-extension", "igni_extension"])
def test_a_missing_sdk_adds_where_to_find_it(tmp_path, monkeypatch, name):
    async def refuse(self, manifest, bundle, *, development):  # noqa: ANN001
        raise PythonEnvironmentError(f"no matching distribution for {name}")

    monkeypatch.setattr(PythonEnvironments, "ensure", refuse)
    probe = _probe(_python_bundle(tmp_path))
    assert probe.error is not None
    assert probe.error.startswith(
        f"installing its package failed: no matching distribution for {name}"
    )
    assert "PIP_FIND_LINKS=<directory with the wheel> igni ext doctor" in probe.error


def test_a_package_that_installs_is_started_from_the_bundle_and_asked_for_its_tools(
    tmp_path, monkeypatch
):
    entry = tmp_path / "entry"
    entry.write_text(f"#!{sys.executable}\n{ONE_TOOL_SERVER}\nserver.run()\n")
    entry.chmod(0o755)
    seen: dict[str, object] = {}

    async def ensure(self, manifest, bundle, *, development):  # noqa: ANN001
        seen.update(bundle=bundle, development=development)
        return entry

    monkeypatch.setattr(PythonEnvironments, "ensure", ensure)
    bundle = _python_bundle(tmp_path)
    probe = _probe(bundle)
    assert probe.error is None
    assert probe.tools == ["lookup_ticket"]
    # A development install: the package may be built from the directory.
    assert seen == {"bundle": bundle, "development": True}


# ── the names, directly ────────────────────────────────────────────


def _names(**permissions: object) -> NameCheck:
    return NameCheck(ExtensionManifest.model_validate(_manifest(permissions=permissions)))


@pytest.mark.parametrize(
    ("declared", "bare"),
    [
        ("acme.tickets.lookup", "lookup"),
        ("lookup", "lookup"),
        ("acme.ticketsX.lookup", "acme.ticketsX.lookup"),
        ("other.acme.tickets.lookup", "other.acme.tickets.lookup"),
    ],
)
def test_bare_strips_only_this_extensions_own_prefix(declared, bare):
    assert _names().bare(declared) == bare


def test_findings_do_not_share_their_lists():
    first, second = Findings(), Findings()
    first.problems.append("x")
    first.warnings.append("y")
    first.notes.append("z")
    assert (second.problems, second.warnings, second.notes) == ([], [], [])


def test_the_table_has_one_row_per_tool_with_its_four_names():
    names = _names(
        tools=[{"name": "acme.tickets.lookup", "annotations": HINTS}],
        rpc=["ext.acme.tickets.lookup"],
    )
    rows = names.table(["lookup", "extra"])
    assert len(rows) == 2
    assert "manifest acme.tickets.lookup" in rows[0]
    assert "page     ext.acme.tickets.lookup" in rows[0]
    assert "registry mcp_ext:acme.tickets_lookup" in rows[0]
    assert "model    mcp_ext_acme_tickets_lookup" in rows[0]
    assert "(not declared)" not in rows[0] and "(not granted" not in rows[0]
    assert "(not declared)" in rows[1] and "(not granted in permissions.rpc)" in rows[1]
    assert names.table([]) == []


def test_compare_with_nothing_declared_and_nothing_offered_finds_nothing():
    findings = Findings()
    _names().compare([], findings)
    assert (findings.problems, findings.warnings, findings.notes) == ([], [], [])


def test_a_tool_declared_with_its_id_prefix_or_without_matches_the_offered_bare_name():
    findings = Findings()
    _names(
        tools=[
            {"name": "acme.tickets.lookup", "annotations": HINTS},
            {"name": "close", "annotations": HINTS},
        ]
    ).compare(["lookup", "close"], findings)
    assert findings.problems == []
    assert findings.warnings == []


def test_a_declared_tool_that_is_not_offered_is_a_problem_naming_both_forms():
    findings = Findings()
    _names(tools=[{"name": "acme.tickets.gone", "annotations": HINTS}]).compare([], findings)
    assert findings.problems == [
        "permissions.tools declares 'acme.tickets.gone' and the process does not offer "
        "a tool called 'gone'"
    ]


def test_an_rpc_grant_of_the_whole_namespace_makes_an_undeclared_tool_page_only():
    findings = Findings()
    _names(rpc=["ext.acme.tickets.*"]).compare(["lookup"], findings)
    assert findings.warnings == []
    assert len(findings.notes) == 1 and "'lookup' is for the page only" in findings.notes[0]


def test_an_environment_is_not_inherited_from_the_test_process(tmp_path, monkeypatch):
    """The probe gives the child its own environment plus ``dev.env``; a
    variable set only here is the host's, not the extension's to rely on."""
    monkeypatch.setenv("FLAVOUR", "from-the-host")
    probe = _probe(_bundle(tmp_path, WHERE_SERVER))
    assert probe.error is not None
    assert "flavour=from-the-host" not in probe.error
    assert os.environ["FLAVOUR"] == "from-the-host"
