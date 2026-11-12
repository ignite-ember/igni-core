"""``scripts/check_group_session.py`` asks a booted Session what the group gave it.

``check_group_config.py`` proves each loader can read the group's
directories; this proves the session hands them over. Run against a real
HTTP server that answers like igni server, as a person running the gate
would, so a Session refactor that renames an attribute the script reads
fails here and not only in a manual run.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.group_pack_server import (
    AGENT,
    SKILL,
    entry,
    full_entries,
    group_pack_server,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_group_session.py"


def _run(url: str, tmp: Path) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("IGNI_")}
    environment.update(IGNI_API=url, TMPDIR=str(tmp))
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> subprocess.CompletedProcess[str]:
    """One run of the gate against a group that ships one entry of each kind."""
    with group_pack_server(full_entries()) as server:
        return _run(server.url, tmp_path_factory.mktemp("tmp"))


class TestAGroupThatShipsEverything:
    def test_every_check_passes_and_the_gate_exits_zero(self, run):
        assert run.returncode == 0, run.stdout + run.stderr
        assert "FAIL" not in run.stdout
        assert "10/10 checks passed" in run.stdout

    def test_the_session_sees_each_kind_the_group_shipped(self, run):
        for line in (
            "group agents in the pool (1)",
            "group skills in the skill pool (1)",
            "group output styles registered",
            "group MCP servers configured",
            "group hooks registered (group-guard.sh)",
            "group rule fires on a .py file",
            "group slash commands resolve",
        ):
            assert f"ok    {line}" in run.stdout

    def test_the_session_knows_what_the_group_ships(self, run):
        assert "ok    knows whether the group ships agents" in run.stdout
        assert "ok    knows whether the group ships hooks" in run.stdout

    def test_the_groups_agents_stay_out_of_the_project(self, run):
        assert "ok    the group's agents stay out of the project ([] leaked)" in run.stdout

    def test_it_leaves_no_throwaway_directory_behind(self, run, tmp_path_factory):
        left = [p.name for p in tmp_path_factory.getbasetemp().glob("tmp*/igni-session-*")]
        assert left == []


class TestWhenThereIsNoPack:
    def test_the_gate_fails_before_booting_a_session(self, tmp_path):
        with group_pack_server(None) as server:
            run = _run(server.url, tmp_path)

        assert run.returncode == 1
        assert "FAIL  no pack" in run.stdout
        assert "Booting a Session" not in run.stdout


class TestAThinGroup:
    def test_only_the_kinds_it_ships_are_checked(self, tmp_path):
        with group_pack_server([entry("agents", "group-reviewer", AGENT)]) as server:
            run = _run(server.url, tmp_path)

        assert run.returncode == 0, run.stdout + run.stderr
        assert "ok    group agents in the pool (1)" in run.stdout
        # An absent kind must be reported as absent, not as shipped.
        assert "ok    knows whether the group ships hooks" in run.stdout
        assert "skill pool" not in run.stdout
        assert "4/4 checks passed" in run.stdout

    def test_an_empty_group_only_compares_what_it_ships(self, tmp_path):
        with group_pack_server([]) as server:
            run = _run(server.url, tmp_path)

        assert run.returncode == 0, run.stdout + run.stderr
        assert "2/2 checks passed" in run.stdout


class TestWhenTheSessionMissesAnEntry:
    def test_the_gate_names_the_failure_and_exits_nonzero(self, tmp_path):
        renamed = SKILL.replace("name: group-skill", "name: something-else")
        with group_pack_server([entry("skills", "group-skill", renamed)]) as server:
            run = _run(server.url, tmp_path)

        assert run.returncode == 1
        assert "FAIL  group skills in the skill pool (0)" in run.stdout
        assert "  failed: group skills in the skill pool (0)" in run.stdout


class TestTheEnvironmentItBorrows:
    def test_home_is_put_back_and_the_throwaway_directory_removed(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.delenv("IGNI_NEO4J_DISABLED", raising=False)
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

        with group_pack_server([]) as server:
            monkeypatch.setenv("IGNI_API", server.url)
            spec = importlib.util.spec_from_file_location("check_group_session_probe", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            assert asyncio.run(module.main()) == 0

        assert os.environ["HOME"] == str(real_home)
        assert "IGNI_NEO4J_DISABLED" not in os.environ
        assert list(tmp_path.glob("igni-session-*")) == []
