"""The harness's constants."""

from __future__ import annotations

import tempfile
from pathlib import Path

# The rig's permissions: the tool and background states' sleeps run without asking.
RIG_CONFIG = (
    "permissions:\n"
    "  allow:\n"
    '    - "Bash(sleep *)"\n'
    "safety:\n"
    "  bypass_guard:\n"
    "    use_model: false\n"
)

# Planted in every secret file of the rig; no request to the model may contain it.
SECRET_CANARY = "IGNI-CANARY-7f3a9c1e-do-not-send"

# The prompt tokens the scripted model reports for every call.
PROMPT_TOKENS = 100

# Run frames: what a working run sends. None may follow a cancel.
RUN_FRAMES = frozenset(
    {
        "run_started",
        "content_delta",
        "tool_started",
        "tool_completed",
        "model_completed",
        "run_paused",
    }
)

# Shared by every backend the harness starts (``HF_HOME``), so models download once.
SHARED_HF_CACHE = Path(tempfile.gettempdir()) / "igni-e2e-hf-cache"

# What a streaming turn says while the person acts.
LONG_TEXT = "Here is a long answer that keeps streaming while the person acts. " * 4
