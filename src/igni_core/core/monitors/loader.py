"""Discovers and merges ``.monitors.json`` files across tiers.

Precedence (lower → higher, last write wins on collision):
1. ``~/.igni/monitors.json`` (user)
2. ``<project>/.monitors.json`` (project — only once the folder is trusted)
3. Plugin-bundled ``.monitors.json`` (namespaced ``<plugin>:<name>``)
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from igni_core.core.monitors.config import MonitorConfig
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.workspace_trust import WorkspaceTrust

logger = logging.getLogger(__name__)

MONITORS_FILE = ".monitors.json"


class MonitorsFile(BaseModel):
    """One ``.monitors.json`` file: a ``monitors`` mapping of name → row.

    Rows stay raw here so one malformed row is dropped on its own in
    :meth:`configs` instead of sinking the whole file.
    """

    model_config = ConfigDict(extra="ignore")

    monitors: dict[str, object] = Field(default_factory=dict)

    @field_validator("monitors", mode="before")
    @classmethod
    def _mapping_or_empty(cls, value: object) -> object:
        return value if isinstance(value, dict) else {}

    @classmethod
    def read(cls, path: Path) -> MonitorsFile:
        """Permissive read — an unreadable, unparsable or wrongly shaped
        file is an empty one, so the merge keeps going."""
        try:
            return cls.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            logger.debug("Monitor config read %s failed: %s", path, exc)
            return cls()

    def configs(self, namespace: str = "") -> dict[str, MonitorConfig]:
        """Typed configs, keyed by name. ``namespace`` (plugin tier) is
        prepended as ``<ns>:<name>`` so the same simple name from several
        plugins doesn't collide. Malformed rows are dropped with a debug log."""
        out: dict[str, MonitorConfig] = {}
        for name, entry in self.monitors.items():
            if not isinstance(entry, dict):
                continue
            full_name = f"{namespace}:{name}" if namespace else name
            try:
                out[full_name] = MonitorConfig.model_validate({**entry, "name": full_name})
            except ValidationError as exc:
                logger.debug("Monitor %s: invalid row — skipping: %s", full_name, exc)
        return out


class MonitorConfigLoader:
    """Loads the monitor configs a session should run.

    ``plugin_roots`` is a list of ``(plugin_path, plugin_name)`` tuples
    (built by ``PluginLoader.collect_monitor_roots``). ``trust`` gates the
    project tier: a folder's ``.monitors.json`` commands start only once it
    is trusted (``core/workspace_trust``).
    """

    def __init__(
        self,
        project_dir: Path,
        trust: WorkspaceTrust,
        plugin_roots: list[tuple[Path, str]] | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._trust = trust
        self._plugin_roots = plugin_roots or []
        self._user_path = Path.home() / CONFIG_DIR / "monitors.json"

    def load(self) -> dict[str, MonitorConfig]:
        """Merge every tier into ``{name → config}``."""
        out: dict[str, MonitorConfig] = {}

        if self._user_path.is_file():
            out.update(MonitorsFile.read(self._user_path).configs())

        project_path = self._project_dir / MONITORS_FILE
        if project_path.is_file() and self._trust.is_trusted(
            self._project_dir, what="monitors (.monitors.json)"
        ):
            out.update(MonitorsFile.read(project_path).configs())

        for root, plugin_name in self._plugin_roots:
            plugin_path = root / MONITORS_FILE
            if plugin_path.is_file():
                out.update(MonitorsFile.read(plugin_path).configs(namespace=plugin_name))

        return out
