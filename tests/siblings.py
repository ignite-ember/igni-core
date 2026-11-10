"""Where igni-sdk, igni-app and igni-server are, for the tests that check igni against them.

igni-core, igni-sdk and igni-app were one repository. Some of igni-core's
tests drive a real extension built with the SDK, read the example
extensions, or hold the web client and the desktop shell to what the
backend says. Those tests still run here, against checkouts of the other
two:

* ``IGNI_SDK_DIR`` — igni-sdk (default: ``../igni-sdk`` beside this repo);
* ``IGNI_APP_DIR`` — igni-app (default: ``../igni-app``).

igni server keeps a copy of igni's own command list and prompt targets,
and igni holds the server to it. That check needs a checkout of the
server, named by ``IGNI_SERVER_DIR``; it defaults to the sibling
directory called ``igni-server``, or to ``igni__server`` — the name the
repository had before it was renamed — when that is the one beside this
one.

Without a checkout the tests that need it skip. Under CI they do not: CI
checks the SDK and the app out, so a missing one is a broken job, not a
quiet skip (``tests/test_dormant_tests_are_declared.py``). The server is
not checked out by CI, so the tests that need it skip there.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

__all__ = [
    "IGNI_APP",
    "IGNI_SDK",
    "IGNI_SERVER",
    "REPO",
    "needs_app",
    "needs_sdk",
    "skip_module_without_sdk",
]

REPO = Path(__file__).resolve().parent.parent


#: The igni-sdk checkout: ``packages/``, ``examples/``.
IGNI_SDK = Path(os.environ.get("IGNI_SDK_DIR") or REPO.parent / "igni-sdk").expanduser().resolve()
#: The igni-app checkout: ``clients/web``, ``clients/tauri``.
IGNI_APP = Path(os.environ.get("IGNI_APP_DIR") or REPO.parent / "igni-app").expanduser().resolve()


def _server_checkout() -> Path:
    """Where igni-server is.

    The repository was renamed from ``igni__server``, so a checkout beside
    this one may carry either name; the current one wins, and the older
    name is the fallback. ``IGNI_SERVER_DIR`` says where to look when
    neither is beside this repository.
    """
    override = os.environ.get("IGNI_SERVER_DIR")
    if override:
        return Path(override).expanduser().resolve()
    for name in ("igni-server", "igni__server"):
        candidate = REPO.parent / name
        if candidate.is_dir():
            return candidate
    return REPO.parent / "igni-server"


#: The igni-server checkout: ``app/services/identity/``, ``tests/fixtures/``.
IGNI_SERVER = _server_checkout()


def _under_ci() -> bool:
    """Under CI a missing checkout is a broken job, so nothing skips."""
    return bool(os.environ.get("CI"))


needs_sdk = pytest.mark.skipif(
    not (IGNI_SDK / "packages" / "igni-extension").is_dir() and not _under_ci(),
    reason=f"no igni-sdk checkout at {IGNI_SDK} (set IGNI_SDK_DIR)",
)
needs_app = pytest.mark.skipif(
    not (IGNI_APP / "clients" / "web").is_dir() and not _under_ci(),
    reason=f"no igni-app checkout at {IGNI_APP} (set IGNI_APP_DIR)",
)


def skip_module_without_sdk() -> None:
    """For a module that imports an SDK at its top: skip it whole, the same way.

    ``needs_sdk`` marks tests, but an import from the SDK fails at collection,
    before any mark is read.
    """
    if not (IGNI_SDK / "packages" / "igni-extension").is_dir() and not _under_ci():
        pytest.skip(
            f"no igni-sdk checkout at {IGNI_SDK} (set IGNI_SDK_DIR)", allow_module_level=True
        )
