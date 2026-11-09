"""The host model's backend protocol can be imported first.

``igni_core.extensions.host.backend`` names the broadcasts package, and
the broadcasts package reads this protocol back — its router needs the
services, which need this. Importing this module first therefore closed
a circle and failed, however the application happened to order its
imports. The view it names is a type, and only the checker needs it
while the module is being read.
"""

from __future__ import annotations

import subprocess
import sys


def test_the_backend_protocol_can_be_imported_on_its_own() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import igni_core.extensions.host.backend"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
