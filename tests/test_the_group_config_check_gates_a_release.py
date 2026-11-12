"""``scripts/check_group_config.py`` is the release gate for group configuration.

It runs igni's own client and loaders against a live server, so these
tests run it as a subprocess against a real HTTP server that answers like
igni server's pack endpoint. What they pin is what a person running the
gate sees: the exit code, the line per check, and the summary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.group_pack_server import (
    SKILL,
    entry,
    full_entries,
    group_pack_server,
    hook_entry,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_group_config.py"


def _run(url: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("IGNI_")}
    environment.update(IGNI_API=url, **env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def run() -> subprocess.CompletedProcess[str]:
    """One run of the gate against a group that ships one entry of each kind."""
    with group_pack_server(full_entries()) as server:
        return _run(server.url)


class TestAGroupThatShipsEverything:
    def test_every_check_passes_and_the_gate_exits_zero(self, run):
        assert run.returncode == 0, run.stdout + run.stderr
        assert "FAIL" not in run.stdout
        assert "17/17 checks passed" in run.stdout

    def test_it_says_what_the_group_shipped(self, run):
        assert "group 'Engineering', 8 entries" in run.stdout
        for kind in ("agents", "skills", "commands", "output-styles", "rules", "workflows"):
            assert f"    {kind}: 1" in run.stdout

    def test_each_kind_is_written_to_its_own_directory(self, run):
        for kind in ("agents", "skills", "commands", "output-styles", "rules", "workflows"):
            assert f"ok    {kind} written to {kind}/" in run.stdout

    def test_each_loader_finds_what_the_group_shipped(self, run):
        for line in (
            "every agent loads under its own name",
            "1 skills load",
            "commands load",
            "output styles load",
            "the group's own hooks register (group-guard.sh)",
            "a path-scoped rule fires on a matching file",
            "workflows are discovered",
        ):
            assert f"ok    {line}" in run.stdout

    def test_a_second_materialise_moves_nothing(self, run):
        assert "ok    re-materialising the same pack is a no-op" in run.stdout
        assert "ok    and nothing of the group's was written into the project" in run.stdout


class TestWhenThereIsNoPack:
    def test_the_gate_fails_before_any_check(self):
        with group_pack_server(None) as server:
            run = _run(server.url)

        assert run.returncode == 1
        assert "FAIL  no pack came back" in run.stdout
        assert "checks passed" not in run.stdout


class TestWhenALoaderMissesAnEntry:
    def test_the_gate_names_the_failure_and_exits_nonzero(self):
        # The entry is written to the right directory, but the skill
        # inside it answers to another name, so the pool has no
        # "group-skill".
        renamed = SKILL.replace("name: group-skill", "name: something-else")
        entries = [entry("skills", "group-skill", renamed)]
        with group_pack_server(entries) as server:
            run = _run(server.url)

        assert run.returncode == 1
        assert "ok    skills written to skills/" in run.stdout
        assert "FAIL  0 skills load" in run.stdout
        assert "  failed: 0 skills load" in run.stdout
        assert "checks passed" in run.stdout


class TestAHookThatIsNotJson:
    def test_it_is_a_named_failure_and_the_other_checks_still_run(self):
        entries = [
            entry("hooks", "broken", "this is { not json", "json"),
            entry("skills", "group-skill", SKILL),
        ]
        with group_pack_server(entries) as server:
            run = _run(server.url)

        assert run.returncode == 1, run.stdout + run.stderr
        assert "Traceback" not in run.stderr
        assert "FAIL  hooks/broken is not valid JSON" in run.stdout
        assert "ok    1 skills load" in run.stdout
        assert "checks passed" in run.stdout
        assert "  failed: hooks/broken is not valid JSON" in run.stdout


class TestAThinGroup:
    def test_kinds_the_group_does_not_ship_are_skipped(self):
        with group_pack_server([entry("skills", "group-skill", SKILL)]) as server:
            run = _run(server.url)

        assert run.returncode == 0, run.stdout + run.stderr
        assert "ok    1 skills load" in run.stdout
        for skipped in ("agents", "commands", "output styles", "hooks", "rule", "workflows"):
            assert (
                skipped not in run.stdout.split("Loading, as a session would")[1].split("Second")[0]
            )
        # Materialised check + skills + the two second-pass checks.
        assert "4/4 checks passed" in run.stdout

    def test_an_empty_group_still_runs_the_second_pass(self):
        with group_pack_server([]) as server:
            run = _run(server.url)

        assert run.returncode == 0
        assert "group 'Engineering', 0 entries" in run.stdout
        assert "2/2 checks passed" in run.stdout

    def test_a_group_shipping_only_hooks_checks_the_hooks(self):
        with group_pack_server([hook_entry("only-this.sh")]) as server:
            run = _run(server.url)

        assert run.returncode == 0, run.stdout + run.stderr
        assert "the group's own hooks register (only-this.sh)" in run.stdout


class TestWhoItAsksAs:
    def test_the_token_defaults_to_the_local_bypass(self):
        with group_pack_server([]) as server:
            _run(server.url)
        assert server.authorizations == ["Bearer local-bypass"]

    def test_igni_token_is_the_token_it_sends(self):
        with group_pack_server([]) as server:
            _run(server.url, IGNI_TOKEN="a-real-token")
        assert server.authorizations == ["Bearer a-real-token"]
