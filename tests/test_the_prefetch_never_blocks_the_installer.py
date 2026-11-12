"""``python -m igni_core.prefetch_models`` is an install step that must not fail the install.

The managed-runtime bootstrap runs it to fetch Laya. A model that cannot
be fetched is reported on stderr and the exit code stays 0: Laya loads on
first use instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run(tmp_path, code: str | None = None, **env: str):
    argv = (
        [sys.executable, "-c", code]
        if code
        else [sys.executable, "-m", "igni_core.prefetch_models"]
    )
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env={**os.environ, "HF_HOME": str(tmp_path / "hf"), **env},
        timeout=120,
    )


def test_a_model_that_cannot_be_fetched_is_reported_on_stderr_and_exits_zero(tmp_path):
    # Air-gapped with an empty cache: the real model refuses to load.
    result = _run(tmp_path, IGNI_AIR_GAPPED="1", HF_HUB_OFFLINE="1")

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "prefetch: loading igni's decision model (~1.7 GB on first run)"
    ]
    assert "prefetch: decision model not fetched: DecisionsUnavailable(" in result.stderr
    assert "ready" not in result.stdout


def test_a_model_that_loads_is_announced_and_exits_zero(tmp_path):
    code = textwrap.dedent(
        """
        import runpy, sys
        from igni_core.decisions_server import model
        model.LayaModel.load = lambda self: None
        sys.argv = ["prefetch_models"]
        runpy.run_module("igni_core.prefetch_models", run_name="__main__")
        """
    )

    result = _run(tmp_path, code)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "prefetch: loading igni's decision model (~1.7 GB on first run)",
        "prefetch: decision model ready",
    ]
    assert result.stderr == ""
