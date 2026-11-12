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
