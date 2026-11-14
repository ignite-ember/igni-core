"""A markdown command is UTF-8 text whatever the machine's locale."""

from __future__ import annotations

import os
import subprocess
import sys


def test_a_command_with_non_ascii_text_is_read_as_utf8_on_a_machine_whose_locale_is_not(tmp_path):
    path = tmp_path / "greet.md"
    path.write_text("naïve — “quoted” ✓", encoding="utf-8")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from igni_core.core.utils.markdown_commands import MarkdownCommand\n"
        "cmd = MarkdownCommand.load(Path(sys.argv[1]))\n"
        "sys.stdout.buffer.write(('none' if cmd is None else cmd.body).encode('utf-8'))\n"
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(
        [sys.executable, "-c", code, str(path)], env=env, capture_output=True, check=True
    )
    assert done.stdout.decode("utf-8") == "naïve — “quoted” ✓"
