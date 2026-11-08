"""Monitor config — one background-monitor declaration as written in a
``.monitors.json`` file. Structured identically to the LSP config:

    {
      "monitors": {
        "build-watcher": {
          "command": "npm",
          "args": ["run", "watch"],
          "cwd": "frontend",
          "env": {"NODE_ENV": "development"},
          "restart": "on_crash"
        }
      }
    }

Discovering and merging the files across tiers is
:class:`~igni_core.core.monitors.loader.MonitorConfigLoader`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


RestartPolicy = Literal["never", "on_crash", "always"]


class MonitorConfig(BaseModel):
    """One background-monitor declaration. Required: ``name`` and
    ``command``. Restart behaviour defaults to ``on_crash``:
    monitors are usually long-running watchers that should
    survive transient failures but not infinite-loop on a hard
    error (the manager backs off and gives up after repeated
    crashes — see ``MonitorManager``).

    Validation is permissive where a hand-written file is likely to be
    sloppy: an unknown ``restart`` falls back to ``on_crash``, a non-string
    ``cwd`` is dropped, a non-mapping ``env`` is empty and its values are
    stringified, and keys the schema does not know are ignored. A blank
    ``command`` is still an error — there is nothing to run.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    # Working directory for the process. Relative paths resolve
    # against the session's ``project_dir`` at launch time;
    # absolute paths are honoured as-is.
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    restart: RestartPolicy = "on_crash"

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be blank")
        return value

    @field_validator("cwd", mode="before")
    @classmethod
    def _cwd_string_or_none(cls, value: object) -> str | None:
        return value if isinstance(value, str) else None

    @field_validator("env", mode="before")
    @classmethod
    def _env_as_strings(cls, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            return {}
        return {str(k): str(v) for k, v in value.items()}

    @field_validator("restart", mode="before")
    @classmethod
    def _restart_or_default(cls, value: object) -> object:
        if value in get_args(RestartPolicy):
            return value
        logger.debug("Unknown monitor restart policy %r — defaulting to on_crash", value)
        return "on_crash"

    def resolve_cwd(self, project_dir: Path) -> str:
        """Resolve ``self.cwd`` against ``project_dir``.

        Absent → the project root itself. Relative → joined onto
        the project root. Absolute → honoured as-is. Returns a
        ``str`` (``asyncio.create_subprocess_exec`` accepts either
        but a string keeps the launch site free of ``PathLike``
        coercion).
        """
        if not self.cwd:
            return str(project_dir)
        cwd = Path(self.cwd)
        if not cwd.is_absolute():
            cwd = project_dir / cwd
        return str(cwd)

    def resolve_env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Merge ``base_env`` (defaults to ``os.environ``) with the
        monitor-specific overrides declared in ``self.env``.

        Kept simple — anything explicitly *unset* by the manifest
        would need a sentinel value. The documented use cases only
        need additive overrides.
        """
        if base_env is None:
            base_env = os.environ
        env: dict[str, str] = dict(base_env)
        env.update(self.env)
        return env
