"""``scripts/_generate_specialist_evals.py`` writes the specialist eval suites.

Run from a scratch directory, so the repository's own ``evals/`` is never
touched. What is pinned is what a person sees: which files appear, what
the YAML looks like, and what the script says about itself.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "_generate_specialist_evals.py"

OLD_NAME = "em" + "ber"

SPECIALISTS = {
    "explorer",
    "architect",
    "debugger",
    "docs",
    "qa",
    "reviewer",
    "security",
    "simplifier",
}


def _generate(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=cwd, capture_output=True, text=True, timeout=60
    )


def _module():
    spec = importlib.util.spec_from_file_location("specialist_evals_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> tuple[Path, subprocess.CompletedProcess[str]]:
    cwd = tmp_path_factory.mktemp("repo")
    (cwd / "evals").mkdir()
    return cwd, _generate(cwd)


class TestARun:
    def test_it_writes_exactly_one_suite_per_specialist(self, generated):
        cwd, run = generated
        assert run.returncode == 0, run.stderr
        assert {p.stem for p in (cwd / "evals").glob("*.yaml")} == SPECIALISTS

    def test_it_reports_each_file_and_its_case_count(self, generated):
        cwd, run = generated
        for name in SPECIALISTS:
            cases = len(yaml.safe_load((cwd / "evals" / f"{name}.yaml").read_text())["cases"])
            assert f"  wrote evals/{name}.yaml — {cases} cases" in run.stdout

    def test_a_suite_has_its_keys_in_the_documented_order(self, generated):
        cwd, _ = generated
        suite = yaml.safe_load((cwd / "evals" / "qa.yaml").read_text())
        assert list(suite) == ["agent", "description", "fixtures", "cases"]
        assert suite["agent"] == "qa"
        assert suite["fixtures"] == [{"source": "sample_project", "target": "."}]
        assert suite["description"].startswith("Pilot eval suite.")

    def test_every_case_names_its_input_and_how_to_judge_it(self, generated):
        cwd, _ = generated
        for name in SPECIALISTS:
            for case in yaml.safe_load((cwd / "evals" / f"{name}.yaml").read_text())["cases"]:
                assert case["name"] and case["input"] and case["judge_guidelines"], (name, case)

    def test_an_empty_list_of_forbidden_tools_is_not_written(self, generated):
        cwd, _ = generated
        for name in SPECIALISTS:
            for case in yaml.safe_load((cwd / "evals" / f"{name}.yaml").read_text())["cases"]:
                assert case.get("unexpected_tool_calls", ["x"]) != []

    def test_the_text_is_not_wrapped_or_escaped(self, generated):
        cwd, _ = generated
        text = "".join(p.read_text() for p in (cwd / "evals").glob("*.yaml"))
        longest = max(len(line) for line in text.splitlines())
        assert longest > 120
        assert "\\u" not in text

    def test_case_names_are_unique_across_every_suite(self, generated):
        cwd, _ = generated
        names = [
            case["name"]
            for name in SPECIALISTS
            for case in yaml.safe_load((cwd / "evals" / f"{name}.yaml").read_text())["cases"]
        ]
        assert len(names) == len(set(names))

    def test_a_read_only_role_forbids_the_tools_that_change_things(self, generated):
        cwd, _ = generated
        cases = yaml.safe_load((cwd / "evals" / "explorer.yaml").read_text())["cases"]
        forbidden = {t for c in cases for t in c.get("unexpected_tool_calls", [])}
        assert {"Edit", "Write"} <= forbidden


class TestWhereItWrites:
    def test_it_keeps_a_suite_whose_content_was_edited_by_hand(self, tmp_path):
        (tmp_path / "evals").mkdir()
        (tmp_path / "evals" / "qa.yaml").write_text("hand edited\n")

        run = _generate(tmp_path)

        assert run.returncode == 0, run.stderr
        assert (tmp_path / "evals" / "qa.yaml").read_text() == "hand edited\n"
        assert "skipped evals/qa.yaml" in run.stdout
        assert "--force" in run.stdout
        assert "wrote evals/explorer.yaml" in run.stdout

    def test_force_overwrites_a_suite_that_is_already_there(self, tmp_path):
        (tmp_path / "evals").mkdir()
        (tmp_path / "evals" / "qa.yaml").write_text("hand edited\n")

        run = subprocess.run(
            [sys.executable, str(SCRIPT), "--force"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert run.returncode == 0, run.stderr
        assert yaml.safe_load((tmp_path / "evals" / "qa.yaml").read_text())["agent"] == "qa"

    def test_an_identical_suite_is_rewritten_without_a_skip(self, tmp_path):
        (tmp_path / "evals").mkdir()
        _generate(tmp_path)

        run = _generate(tmp_path)

        assert "skipped" not in run.stdout

    def test_without_an_evals_directory_it_fails_and_writes_nothing(self, tmp_path):
        run = _generate(tmp_path)

        assert run.returncode != 0
        assert "FileNotFoundError" in run.stderr
        assert list(tmp_path.iterdir()) == []


class TestWhatItSaysAboutItself:
    def test_the_docstring_counts_the_suites_it_writes(self):
        module = _module()
        claimed = int(re.search(r"for the (\d+) specialist agents", module.__doc__).group(1))
        assert claimed == len(module.CASE_GENERATORS) == len(SPECIALISTS)

    def test_no_text_it_writes_names_the_old_product(self, generated):
        cwd, _ = generated
        for path in (cwd / "evals").glob("*.yaml"):
            assert OLD_NAME not in path.read_text().lower(), path.name

    def test_the_project_file_the_docs_suite_points_at_exists(self):
        suite = (REPO / "evals" / "docs.yaml").read_text()
        assert "igni.md" in suite and (REPO / "igni.md").is_file()
