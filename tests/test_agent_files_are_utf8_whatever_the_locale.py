"""Agent files are UTF-8, whatever the machine's locale says.

The files are written and read with an explicit encoding: an agent whose name,
description or prompt is not ASCII must come back as written even where the
process locale is ASCII (a container with no ``LANG``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run(script: str, arg: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    return subprocess.run(
        [sys.executable, "-c", script, arg], env=env, capture_output=True, text=True, timeout=120
    )


def test_an_ephemeral_agent_with_unicode_survives_the_locale(tmp_path) -> None:
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from igni_core.core.agents import AgentPool
        from igni_core.core.config.settings import Settings

        project = Path(sys.argv[1])
        with patch("igni_core.core.agents.builder.AgentBuilder.build") as build:
            build.return_value = MagicMock()
            pool = AgentPool(settings=Settings(), project_dir=project)
            pool.init_ephemeral(project)
            pool.ephemeral.register(
                name="jäger", description="日本語 ✓", system_prompt="Grüße ✓"
            )
        definition = pool.get_entry("jäger").definition
        assert definition.description == "日本語 ✓", definition.description
        assert definition.system_prompt == "Grüße ✓", definition.system_prompt
        """
    )
    done = _run(script, str(tmp_path))
    assert done.returncode == 0, done.stderr


def test_an_agent_file_with_unicode_is_read_as_utf8_under_an_ascii_locale(tmp_path) -> None:
    path = tmp_path / "a.md"
    path.write_text("---\nname: jäger\ndescription: 日本語\n---\nGrüße ✓\n", encoding="utf-8")
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from igni_core.core.agents.markdown import AgentMarkdownFile
        from igni_core.core.prompts import PromptFragments, PromptLoader

        md = AgentMarkdownFile(Path(sys.argv[1]), prompt_loader=PromptLoader(PromptFragments()))
        definition = md.parse()
        assert definition.name == "jäger", definition.name
        assert definition.system_prompt == "Grüße ✓", definition.system_prompt
        """
    )
    done = _run(script, str(tmp_path / "a.md"))
    assert done.returncode == 0, done.stderr
