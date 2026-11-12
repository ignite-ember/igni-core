"""Importing igni_core disarms a torchvision that cannot be imported.

A torch/torchvision pair whose native bindings disagree makes
``import torchvision`` raise, and ``transformers`` turns that into a
misleading error for every user, Laya included. igni reads text only, so
the package marks torchvision unimportable and ``transformers`` takes its
text-only path. Each case runs in a fresh interpreter with a fake
``torchvision`` first on the path, because the shim acts at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_PROBE = textwrap.dedent(
    """
    import logging, sys
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(name)s|%(message)s")
    import igni_core
    print("in_modules", "torchvision" in sys.modules, sys.modules.get("torchvision", "absent"))
    try:
        import torchvision
        print("import ok", getattr(torchvision, "MARK", None))
    except ImportError as exc:
        print("import failed", type(exc).__name__)
    """
)


def _run(tmp_path: Path, torchvision_source: str | None, *, extra: str = "") -> str:
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    if torchvision_source is not None:
        package = fake / "torchvision"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text(torchvision_source, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(fake), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", extra + _PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_torchvision_that_raises_on_import_becomes_unimportable(tmp_path):
    out = _run(
        tmp_path,
        'raise RuntimeError("operator torchvision::nms does not exist")\n',
    )

    assert "in_modules True None" in out
    assert "import failed ModuleNotFoundError" in out
    assert "torchvision import failed (operator torchvision::nms does not exist)" in out


def test_a_torchvision_that_imports_is_left_alone(tmp_path):
    out = _run(tmp_path, "MARK = 'healthy'\n")

    assert "import ok healthy" in out
    assert "torchvision import failed" not in out


def test_a_missing_torchvision_is_left_alone(tmp_path):
    out = _run(tmp_path, None)

    assert "in_modules False absent" in out
    assert "import failed ModuleNotFoundError" in out
    assert "torchvision import failed" not in out


def test_a_torchvision_somebody_already_imported_is_not_probed_again(tmp_path):
    out = _run(
        tmp_path,
        'raise RuntimeError("must not be imported")\n',
        extra="import sys, types\nsys.modules['torchvision'] = types.ModuleType('torchvision')\n"
        "sys.modules['torchvision'].MARK = 'preset'\n",
    )

    assert "import ok preset" in out
    assert "torchvision import failed" not in out


def test_a_partial_install_whose_spec_lookup_raises_is_ignored(tmp_path):
    out = _run(
        tmp_path,
        "MARK = 'unused'\n",
        extra=(
            "import importlib.util\n"
            "_real = importlib.util.find_spec\n"
            "def _boom(name, *a, **k):\n"
            "    if name == 'torchvision':\n"
            "        raise ValueError('partial install')\n"
            "    return _real(name, *a, **k)\n"
            "importlib.util.find_spec = _boom\n"
        ),
    )

    assert "in_modules False absent" in out
    assert "torchvision import failed" not in out
