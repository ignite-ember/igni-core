"""The group-policy package imports in any order, without a cycle through the auth package.

``core.auth`` imports ``portal_client``, which imports the pack's wire models
(``answers``, ``pack``, ``entry``, ``jwks``); ``trust`` imports
``core.auth.credentials``. That is only acyclic while the wire models never
reach back for ``trust`` or anything built on it, so both halves are checked
here: every submodule imports first in a fresh interpreter, and the wire
models pull in none of the modules that need the auth package.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

PACKAGE = "igni_core.core.config.group_policy"

SUBMODULES = [
    "trust",
    "verifier",
    "settings_fetcher",
    "cache",
    "refresher",
    "transport",
    "signed_store",
    "materializer",
    "kinds",
    "plugins",
    "hooks",
    "meta",
    "layout",
    "jwks",
    "answers",
    "pack",
    "entry",
    "gate",
]

WIRE_MODELS = ["answers", "pack", "entry", "jwks"]
NEEDS_AUTH = ["trust", "verifier", "cache", "transport", "settings_fetcher"]


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


@pytest.mark.parametrize("name", SUBMODULES)
def test_each_submodule_imports_first(name: str):
    result = _run(f"import {PACKAGE}.{name}")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", WIRE_MODELS)
def test_the_wire_models_do_not_reach_for_trust(name: str):
    needs_auth = [f"{PACKAGE}.{module}" for module in NEEDS_AUTH]
    result = _run(
        "import sys\n"
        f"import {PACKAGE}.{name}\n"
        f"print([m for m in {needs_auth!r} if m in sys.modules])"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_auth_package_then_the_trust_code():
    result = _run(f"import igni_core.core.auth\nimport {PACKAGE}.settings_fetcher")

    assert result.returncode == 0, result.stderr
