"""The product is igni; the maintenance scripts must not say otherwise.

``scripts/debug_prompt_real.py`` fell back to the agent id ``"ember"``
when the team had none, which a diagnostic would then print into the
session it built. The scripts audited here are checked for the old name
in any case.
"""

from __future__ import annotations

import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
_NAMES = [
    "codewrite_judge.py",
    "debug_prompt_real.py",
    "debug_prompt_size.py",
    "dump_wire_schema.py",
    "export_contracts.py",
    "measure_warm_base.py",
    "onboarding-smoke.ps1",
    "onboarding-smoke.sh",
    "probe_rate_limits.py",
    "profile_run.py",
    "run_claude_code_eval.py",
]


@pytest.mark.parametrize("name", _NAMES)
def test_a_script_does_not_use_the_old_product_name(name):
    text = (_SCRIPTS / name).read_text().lower()

    assert "ember" not in text, f"{name} still names the product by its old name"


# Other repositories, spelled in pieces so this guard does not trip on itself.
_OTHER_REPOS = [
    "igni" + "-server",
    "igni" + "__",
    "igni" + "-app",
    "igni" + "-sdk",
    "code" + "-index",
    "code" + "index",
]
# reindex_hnsw.py is being moved out of the repository separately.
_EXEMPT = {"reindex_hnsw.py"}


@pytest.mark.parametrize(
    "path",
    sorted(p for p in _SCRIPTS.iterdir() if p.is_file() and p.name not in _EXEMPT),
    ids=lambda p: p.name,
)
def test_a_script_names_no_other_repository(path):
    text = path.read_text().lower()

    found = [name for name in _OTHER_REPOS if name in text]
    assert not found, f"{path.name} names another repository: {found}"
