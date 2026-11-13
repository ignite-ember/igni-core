"""Per-project persistence for the "disabled MCP tools" list.

Extracted from :mod:`igni_core.core.mcp.client` so the on-disk
representation (``.igni/mcp-tool-state.json``) has a single owner
and the ``MCPClientManager`` can focus on transport / connection
lifecycle. The state is a plain ``{server: set[tool_name]}``
mapping; the file is a JSON blob keyed under ``"disabled"``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from igni_core.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)


class MCPToolStateStore:
    """File-backed store for the per-project disabled-tools list.

    Lives at ``<project>/.igni/mcp-tool-state.json``. When
    ``project_dir`` is ``None`` (headless / test) the store is a
    no-op — ``load`` returns an empty dict and ``save`` silently
    drops. That mirrors the manager's behaviour before this class
    existed.
    """

    def __init__(self, project_dir: Path | None):
        self._project_dir = project_dir

    def path(self) -> Path | None:
        if self._project_dir is None:
            return None
        return self._project_dir / CONFIG_DIR / "mcp-tool-state.json"

    def load(self) -> dict[str, set[str]]:
        """Return ``{server: set[tool_name]}`` from disk. Empty when
        the file is missing, unreadable, or malformed — the store
        degrades to "no disabled tools" rather than raising."""
        path = self.path()
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text() or "{}")
        except (OSError, ValueError) as exc:
            logger.debug("MCP tool state read failed (%s): %s", path, exc)
            return {}
        disabled = data.get("disabled") if isinstance(data, dict) else None
        if not isinstance(disabled, dict):
            return {}
        out: dict[str, set[str]] = {}
        for server, tools in disabled.items():
            if isinstance(tools, list):
                out[server] = {str(t) for t in tools}
        return out

    def save(self, disabled: dict[str, set[str]]) -> None:
        """Write the state file. Empty inner sets are pruned so the
        blob only carries servers with at least one disabled tool.
        Raises :class:`OSError` when the file cannot be written: a
        choice that was not kept must not be reported as saved."""
        path = self.path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "disabled": {server: sorted(tools) for server, tools in disabled.items() if tools}
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")
