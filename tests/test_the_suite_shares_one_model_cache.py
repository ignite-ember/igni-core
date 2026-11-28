"""Every test's decisions service finds Laya in one shared cache, never in the test's own home."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.conftest import SHARED_HF_CACHE


def test_a_process_a_test_starts_reads_the_shared_cache_not_its_home() -> None:
    seen = subprocess.run(
        [sys.executable, "-c", "from huggingface_hub import constants; print(constants.HF_HOME)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(seen) == SHARED_HF_CACHE
    assert not Path(seen).is_relative_to(Path.home())
