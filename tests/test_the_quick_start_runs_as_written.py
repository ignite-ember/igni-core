"""The README's Quick Start command runs as written, headless.

`igni -m` has nobody to ask, and igni reads files with the shell, which
asks by default: the headline `igni -m "In one sentence, what does this
project do?"` stopped at its first `cat` with "Run paused for
confirmation" and exited 1 on a fresh install. The Quick Start's own
command has to be one that answers.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else None]


def test_the_readme_quick_start_question_runs_read_only():
    quick_start = _section((_ROOT / "README.md").read_text(encoding="utf-8"), "## Quick Start")
    (fence,) = re.findall(r"```bash\n(.*?)```", quick_start, re.S)[:1]
    runs = [line for line in fence.splitlines() if re.match(r"\s*igni\b.*\s-m\s", line)]

    assert runs, "the Quick Start has no `igni -m` command"
    for line in runs:
        assert "--read-only" in line, f"`{line.strip()}` pauses for confirmation headless"
