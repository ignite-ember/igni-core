"""Fuzz the extension manifest reader an install runs on a bundle.

``ExtensionManifest.load`` reads the ``manifest.json`` of a bundle
somebody handed an administrator, so its input is whatever bytes are in
that file. Its contract is a manifest or a ``ManifestError`` that names
the problem; anything else escaping it is a crash. The bytes are written
to one file in a private temporary directory, the only write this
fuzzer makes. An accepted manifest's derived answers (packaging, display
name, RPC grants) must answer without raising.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports(include=["igni_core"]):
    from igni_core.extensions.manifest import ExtensionManifest
    from igni_core.extensions.manifest_errors import ManifestError

_MANIFEST = Path(tempfile.mkdtemp(prefix="igni-fuzz-manifest-")) / "manifest.json"


def TestOneInput(data: bytes) -> None:
    _MANIFEST.write_bytes(data)
    try:
        manifest = ExtensionManifest.load(_MANIFEST)
    except ManifestError as exc:
        assert str(exc), "a refusal has to say what is wrong"
        return

    assert manifest.packaging in ("sidecar", "endpoint", "python", "dev")
    assert isinstance(manifest.display_name, str) and manifest.display_name
    assert isinstance(manifest.runs_here, bool)
    assert isinstance(manifest.is_development_command, bool)
    for method in ("sessions.list", "", manifest.id):
        assert isinstance(manifest.grants_rpc(method), bool)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
