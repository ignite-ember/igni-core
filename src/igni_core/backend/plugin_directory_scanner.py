"""Filesystem walker that builds a :class:`PluginContents` inventory.

Each per-subdir step (skills / agents / hooks / MCP servers / tools /
README) is its own method so every reader is independently testable.
Third-party file shapes are validated by the models in
:mod:`igni_core.backend.plugin_bundle_files`; a file that cannot be read
or does not match its shape leaves its section empty and is logged with
the reason.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from igni_core.backend.plugin_bundle_files import (
    MarkdownFrontmatter,
    PluginHooksFile,
    PluginMCPFile,
)
from igni_core.backend.plugin_schemas import (
    PluginAgentInfo,
    PluginContents,
    PluginSkillInfo,
    PluginToolInfo,
)

logger = logging.getLogger(__name__)


class PluginDirectoryScanner:
    """Walk one plugin directory and accumulate its bundled contents.

    Each ``_scan_*`` method mutates :attr:`_result` in place — the
    scanner owns the accumulator instead of threading it through
    every helper as an argument.

    :param root: The plugin directory to scan (either an installed
        plugin root or a temp-clone root during marketplace preview).
    :param name: Display name to embed in :attr:`PluginContents.name`.
    :param readme_cap: Byte cap on README excerpt to prevent a runaway
        file blowing up the wire — plugin READMEs in the wild top out
        well under the default 200_000.
    """

    def __init__(self, root: Path, name: str, readme_cap: int = 200_000) -> None:
        self._root = root
        self._name = name
        self._readme_cap = readme_cap
        self._result = PluginContents(name=name, root_path=str(root))

    def scan(self) -> PluginContents:
        """Run every per-subdir scan step and return the built model."""
        self._scan_skills()
        self._scan_agents()
        self._scan_hooks()
        self._scan_mcp_servers()
        self._scan_tools()
        self._load_readme()
        return self._result

    # ── Per-subdir scan steps ──────────────────────────────────────

    def _scan_skills(self) -> None:
        """Enumerate ``skills/*/SKILL.md`` — descriptions pulled from
        the SKILL.md frontmatter."""
        skills_dir = self._root / "skills"
        if not skills_dir.is_dir():
            return
        for sd in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = sd / "SKILL.md"
            desc = self._description(skill_md) if skill_md.is_file() else ""
            self._result.skills.append(PluginSkillInfo(name=sd.name, description=desc))

    def _scan_agents(self) -> None:
        """Enumerate ``agents/*.md`` — descriptions pulled from
        agent-file frontmatter."""
        agents_dir = self._root / "agents"
        if not agents_dir.is_dir():
            return
        for af in sorted(agents_dir.glob("*.md")):
            self._result.agents.append(
                PluginAgentInfo(name=af.stem, description=self._description(af))
            )

    def _scan_hooks(self) -> None:
        """Read ``hooks/hooks.json`` and record one entry per event
        with the handler count."""
        hooks_json = self._root / "hooks" / "hooks.json"
        if not hooks_json.is_file():
            return
        try:
            hooks_file = PluginHooksFile.model_validate_json(hooks_json.read_text())
        except (OSError, ValidationError) as exc:
            logger.warning("Plugin %s: skipping unreadable %s: %s", self._name, hooks_json, exc)
            return
        self._result.hooks.extend(hooks_file.hook_infos())

    def _scan_mcp_servers(self) -> None:
        """Read ``.mcp.json`` (or ``mcp.json``) and record one entry
        per server. Stops at the first file found."""
        for mcp_name in (".mcp.json", "mcp.json"):
            mcp_path = self._root / mcp_name
            if not mcp_path.is_file():
                continue
            try:
                mcp_file = PluginMCPFile.model_validate_json(mcp_path.read_text())
            except (OSError, ValidationError) as exc:
                logger.warning("Plugin %s: skipping unreadable %s: %s", self._name, mcp_path, exc)
                return
            self._result.mcp_servers.extend(mcp_file.server_infos())
            return

    def _scan_tools(self) -> None:
        """Enumerate ``tools/*.py`` (dunder / underscored modules
        skipped as internal)."""
        tools_dir = self._root / "tools"
        if not tools_dir.is_dir():
            return
        for tf in sorted(tools_dir.glob("*.py")):
            if tf.name.startswith("_"):
                continue
            self._result.tools.append(PluginToolInfo(name=tf.stem))

    def _load_readme(self) -> None:
        """Load the first README variant found. Truncated at
        :attr:`_readme_cap` with a marker so the wire never carries a
        pathologically large blob."""
        for readme_name in ("README.md", "Readme.md", "readme.md"):
            rp = self._root / readme_name
            if not rp.is_file():
                continue
            try:
                text = rp.read_text(errors="replace")
            except OSError as exc:
                logger.warning("Plugin %s: skipping unreadable %s: %s", self._name, rp, exc)
                return
            if len(text) > self._readme_cap:
                text = (
                    text[: self._readme_cap]
                    + "\n\n_…README truncated — open the source repo for the rest._"
                )
            self._result.readme = text
            return

    # ── Shared reading helper ──────────────────────────────────────

    def _description(self, md_path: Path) -> str:
        """The ``description`` frontmatter value of *md_path*, or ``""``
        when the file has none or cannot be read."""
        try:
            text = md_path.read_text(errors="replace")
        except OSError as exc:
            logger.warning("Plugin %s: skipping unreadable %s: %s", self._name, md_path, exc)
            return ""
        return MarkdownFrontmatter.parse(text).value("description")
