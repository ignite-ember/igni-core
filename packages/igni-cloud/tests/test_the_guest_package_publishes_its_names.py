"""``igni_cloud.guest`` publishes the guest contract's names from one place.

The package root is the front door of the guest contract: the cloud
layer, the SDKs and the node agent's tools import from it. So what it
publishes is behaviour — a name in ``__all__`` the module no longer
defines breaks ``from igni_cloud.guest import *`` at import time (the
whole package then fails to import, not just that one name), and a name
added to the package but left out of ``__all__`` is invisible to every
wildcard caller and to anything that reads the list.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import igni_cloud
import igni_cloud.guest as guest

#: The names the guest contract publishes, as the package promises them.
PUBLISHED = (
    "ActivityFile",
    "AtomicFile",
    "BindingAttempt",
    "CloudGuest",
    "GuestActivity",
    "GuestPaths",
    "ProcessEntropy",
    "ProducerBinding",
    "ProducerEnv",
    "ReadyFile",
    "RestoreWatch",
    "Restored",
    "TokenTenant",
    "UnboundRefusal",
    "WarmImports",
)


def test_the_package_publishes_exactly_the_guest_contract_names():
    assert tuple(guest.__all__) == PUBLISHED


def test_every_published_name_is_the_one_its_module_defines():
    for name in PUBLISHED:
        published = getattr(guest, name)
        module = sys.modules[published.__module__]
        assert getattr(module, name) is published, name


def test_a_wildcard_import_gives_exactly_the_published_names():
    namespace: dict[str, object] = {}
    exec("from igni_cloud.guest import *", namespace)  # noqa: S102 — the star import is the point
    assert {name for name in namespace if not name.startswith("__")} == set(PUBLISHED)


def test_the_package_imports_from_a_cold_interpreter():
    """The barrel imports eight siblings that import each other: no cycle."""
    source = str(Path(igni_cloud.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([source, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    done = subprocess.run(
        [sys.executable, "-c", "import igni_cloud.guest as g; print(len(g.__all__))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(len(PUBLISHED))
