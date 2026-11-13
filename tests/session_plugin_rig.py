"""A plugin on disk that ships one of everything a session reads from it.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

``write_plugin`` puts a plugin under the project's own plugin root, so a
session built on that project discovers it; ``disable_plugin`` records it
in the plugins state file of the session's data directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.plugins.state import PluginsState, save_state


def write_plugin(project: Path, name: str) -> Path:
    """A project plugin shipping an agent, a skill, an MCP server, a language
    server, a monitor and an output style, each named after the plugin."""
    root = project / CONFIG_DIR / "plugins" / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"})
    )
    (root / "agents").mkdir()
    (root / "agents" / f"{name}-agent.md").write_text(
        f"---\nname: {name}-agent\ndescription: Agent of {name}\n---\n\nDo the {name} work.\n"
    )
    (root / "skills" / f"{name}-skill").mkdir(parents=True)
    (root / "skills" / f"{name}-skill" / "SKILL.md").write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n\nHow to {name}.\n"
    )
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"srv": {"command": "echo", "args": [name]}}})
    )
    (root / ".lsp.json").write_text(
        json.dumps(
            {"lspServers": {"ls": {"command": "echo", "args": [name], "languages": ["python"]}}}
        )
    )
    (root / ".monitors.json").write_text(
        json.dumps({"monitors": {"mon": {"command": "echo", "args": [name]}}})
    )
    (root / "output-styles").mkdir()
    (root / "output-styles" / f"{name}-style.md").write_text(
        f"---\nname: {name}-style\ndescription: Style of {name}\n---\n\nSpeak as {name}.\n"
    )
    return root


def disable_plugin(data_dir: Path, *names: str) -> None:
    """Record *names* as disabled in the plugins state file under *data_dir*."""
    save_state(PluginsState(disabled=list(names)), data_dir)
