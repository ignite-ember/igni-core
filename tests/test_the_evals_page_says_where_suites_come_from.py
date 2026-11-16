"""EVALS.md says where /evals finds suites, and claims no installed ones.

It said "Built-in agents ship with built-in evals in ``<install>/evals/``".
The wheel carries no eval YAML and the loader reads no install directory:
in a project without ``.igni/evals/`` or ``evals/``, ``/evals`` answers
"No eval suites found" (campaign R056).
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.evals.loader import EvalSuite

_ROOT = Path(__file__).resolve().parent.parent


def test_the_package_ships_no_eval_suites():
    assert not list((_ROOT / "src/igni_core").rglob("evals/*.yaml"))


def test_a_project_without_eval_folders_has_no_suites(tmp_path):
    assert EvalSuite.load_all(tmp_path) == []


def test_the_page_names_no_install_directory_and_both_project_folders():
    text = (_ROOT / "docs/EVALS.md").read_text()
    assert "<install>/evals" not in text
    assert "`.igni/evals/`" in text and "`evals/`" in text
