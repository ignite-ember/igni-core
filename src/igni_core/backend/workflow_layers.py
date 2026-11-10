"""Where workflow files are looked for, and which copy of a name wins."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from igni_core.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

DEFAULT_WORKFLOW_DIR = Path(CONFIG_DIR) / "workflows"
FALLBACK_WORKFLOW_DIR = Path(".claude") / "workflows"


class FallbackLayerNotice:
    """Says once that ``.claude/workflows`` is deprecated.

    One instance per discovery, so a session that lists workflows on every
    panel open says it once.
    """

    def __init__(self) -> None:
        self.noted = False

    def note(self, fallback: Path, project: Path) -> None:
        if self.noted:
            return
        self.noted = True
        logger.warning(
            "Reading workflows from %s, which is deprecated: move them to %s "
            "(it wins over .claude/workflows when both define a name).",
            fallback,
            project,
        )


class WorkflowLayers(BaseModel):
    """The workflow directories, later ones winning a name collision:

    1. ``~/.igni/workflows`` — the person's own, in every project.
    2. The group policy cache, when the person's group ships workflows.
       Read from there rather than copied into the project.
    3. ``<project>/.claude/workflows`` — the sibling tool's directory, a
       deprecated fallback that is still read, with a one-time note.
    4. ``<project>/.igni/workflows`` — the project's own, committed with it.

    The two project layers are read only once the folder is trusted:
    discovery *runs* each file to read its ``meta``.
    """

    model_config = ConfigDict(frozen=True)

    project_dir: Path
    user: Path
    group: Path | None = None

    @classmethod
    def for_project(
        cls,
        project_dir: Path,
        *,
        group_dir: Path | None = None,
        home_dir: Path | None = None,
    ) -> WorkflowLayers:
        home = Path(home_dir) if home_dir is not None else Path.home()
        return cls(
            project_dir=Path(project_dir),
            user=home / DEFAULT_WORKFLOW_DIR,
            group=Path(group_dir) if group_dir is not None else None,
        )

    @property
    def project(self) -> Path:
        return self.project_dir / DEFAULT_WORKFLOW_DIR

    @property
    def fallback(self) -> Path:
        return self.project_dir / FALLBACK_WORKFLOW_DIR

    def paths(self, *, trusted: bool, notice: FallbackLayerNotice) -> list[Path]:
        """Every ``*.mjs`` file across the layers, in shadowing order."""
        layers = [self.user]
        if self.group is not None:
            layers.append(self.group)
        if trusted:
            layers += [self.fallback, self.project]
        out: list[Path] = []
        for directory in layers:
            if directory.is_dir():
                found = sorted(directory.glob("*.mjs"))
                if found and directory == self.fallback:
                    notice.note(self.fallback, self.project)
                out.extend(found)
        return out

    @staticmethod
    def shadow(paths: list[Path]) -> dict[str, Path]:
        """``{name: path}``, a later path winning its name (the file stem)."""
        return {path.stem: path for path in paths}

    def display_path(self, path: Path) -> str:
        """Relative to the project when inside it, absolute otherwise.

        The runner joins this onto the project directory, which leaves an
        absolute path as it is.
        """
        try:
            return str(path.relative_to(self.project_dir))
        except ValueError:
            return str(path)

    def not_found_message(self, name: str, *, trusted: bool) -> str:
        """What ``run_workflow`` says about a name no layer defines."""
        message = f"workflow '{name}' not found in {self.project}"
        if not trusted:
            message += " (this folder is not trusted, so its workflows are not read)"
        return message + f" or {self.user}"
