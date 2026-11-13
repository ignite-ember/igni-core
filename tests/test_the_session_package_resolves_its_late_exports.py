"""``igni_core.core.session``: the two exports resolved on first use, and the rest.

``SingleMessageRun`` and ``run_single_message`` install the headless
extensions, and the extension host's channel reaches back into this
package, so importing them with the package would close a loop. They are
resolved by ``__getattr__`` instead — same names, later moment. Each
check runs in a fresh interpreter, because import order is the point.
"""

from __future__ import annotations

import subprocess
import sys


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


def test_importing_the_package_does_not_load_the_two_late_modules() -> None:
    result = _run(
        "import sys, igni_core.core.session\n"
        "late = [m for m in ('igni_core.core.session.single_message_run',"
        " 'igni_core.core.session.runner') if m in sys.modules]\n"
        "assert not late, late\n"
    )

    assert result.returncode == 0, result.stderr


def test_the_late_names_resolve_to_the_real_classes_on_first_use_and_every_time() -> None:
    result = _run(
        "import igni_core.core.session as pkg\n"
        "from igni_core.core.session.single_message_run import SingleMessageRun\n"
        "from igni_core.core.session.runner import run_single_message\n"
        "assert pkg.SingleMessageRun is SingleMessageRun\n"
        "assert pkg.run_single_message is run_single_message\n"
        "from igni_core.core.session import SingleMessageRun as again\n"
        "assert again is SingleMessageRun\n"
    )

    assert result.returncode == 0, result.stderr


def test_an_unknown_name_is_an_attribute_error_naming_the_module() -> None:
    result = _run(
        "import igni_core.core.session as pkg\n"
        "try:\n"
        "    pkg.NoSuchThing\n"
        "except AttributeError as exc:\n"
        "    assert str(exc) == \"module 'igni_core.core.session' has no attribute 'NoSuchThing'\","
        " str(exc)\n"
        "else:\n"
        "    raise SystemExit('no AttributeError')\n"
        "assert not hasattr(pkg, 'NoSuchThing')\n"
    )

    assert result.returncode == 0, result.stderr


def test_every_name_in_all_resolves() -> None:
    result = _run(
        "import igni_core.core.session as pkg\n"
        "missing = [n for n in pkg.__all__ if getattr(pkg, n, None) is None]\n"
        "assert not missing, missing\n"
        "assert 'SingleMessageRun' in pkg.__all__ and 'run_single_message' in pkg.__all__\n"
    )

    assert result.returncode == 0, result.stderr


def test_the_extension_host_channel_can_be_imported_before_the_session_package() -> None:
    for first in (
        "igni_core.backend.headless_extensions",
        "igni_core.core.session.single_message_run",
        "igni_core.core.session.runner",
        "igni_core.core.session",
    ):
        result = _run(
            f"import {first}\nimport igni_core.core.session as pkg\npkg.SingleMessageRun\n"
        )

        assert result.returncode == 0, f"{first}: {result.stderr}"
