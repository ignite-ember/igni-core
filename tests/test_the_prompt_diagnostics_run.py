"""The two prompt diagnostics must run against a real project.

``scripts/debug_prompt_size.py`` and ``scripts/debug_prompt_real.py``
are what a maintainer reaches for when ``input_tokens`` looks too high.
Both import the settings loader by name, and both kept importing a
``load_settings`` that moved into ``SettingsLoader.load`` — so each died
with an ImportError before printing a single number, and nobody noticed
because nothing ran them.

They are run here for real, as a subprocess against a scratch project,
and the report a maintainer reads is checked: the sections and totals
for the size script, the system message and tool schemas for the real one.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SIZE = _ROOT / "scripts/debug_prompt_size.py"
_REAL = _ROOT / "scripts/debug_prompt_real.py"


def _run(script: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        timeout=180,
    )


class TestTheSizeReport:
    def test_it_reports_each_section_and_a_total_for_a_project(self, tmp_path):
        result = _run(_SIZE, str(tmp_path))

        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert f"Project: {tmp_path.resolve()}" in out
        assert "instructions[0]" in out
        assert "tools (" in out and "resolved)" in out
        assert "TOTAL" in out
        assert "conversation history" in out

    def test_the_sections_are_listed_largest_first(self, tmp_path):
        out = _run(_SIZE, str(tmp_path)).stdout

        sizes = [
            int(line.split(" chars")[0].split()[-1].replace(",", ""))
            for line in out.splitlines()
            if " chars  ~" in line and "TOTAL" not in line
        ]

        assert len(sizes) >= 2
        assert sizes == sorted(sizes, reverse=True)

    def test_a_path_that_is_not_a_directory_exits_one(self, tmp_path):
        a_file = tmp_path / "plain.txt"
        a_file.write_text("x")

        result = _run(_SIZE, str(a_file))

        assert result.returncode == 1
        assert f"not a directory: {a_file.resolve()}" in result.stdout


class TestTheRealPromptReport:
    def test_it_reports_the_system_message_the_tools_and_the_floor(self, tmp_path):
        result = _run(_REAL, str(tmp_path))

        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert f"Project: {tmp_path.resolve()}" in out
        assert "Agno-built system message" in out
        assert "Tool JSON schemas (" in out
        assert "Per-turn floor" in out
        assert "First 60 lines of the system message" in out
        assert "You are igni" in out
