"""The wire arguments of the GUI-panel RPCs, validated before
:class:`~igni_core.backend.rpc_handlers.panels.PanelRpcHandler` sees them."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class PanelArgs(BaseModel):
    """Base for every panel RPC's arguments: keys a newer frontend adds
    are ignored rather than refused."""

    model_config = ConfigDict(extra="ignore")


class CompleteFilesArgs(PanelArgs):
    """``complete_files``: the typed prefix and how many matches to return."""

    query: str = ""
    limit: int = 50


class ReadFileArgs(PanelArgs):
    """``read_file``: the project file to read."""

    path: str


class SearchCodeArgs(PanelArgs):
    """``search_code``: the snippet to find across the project.

    A key it does not name is refused, not ignored: a call that sent the
    snippet under another name (``snippet``) was told only that ``query``
    was missing, and searched for nothing."""

    model_config = ConfigDict(extra="forbid")

    query: str


class RunShellArgs(PanelArgs):
    """``run_shell``: the ``$``-prefixed command line."""

    command: str = ""


class ListDirsArgs(PanelArgs):
    """``list_dirs``: the folder to list (the home folder when absent or
    empty) and whether dot-dirs are shown."""

    path: str | None = None
    show_hidden: bool = False

    @property
    def root(self) -> Path:
        return Path(self.path) if self.path else Path.home()


class PickDirArgs(PanelArgs):
    """``pick_dir_native``: where the OS picker opens (the project folder
    when absent or empty)."""

    start: str | None = None

    def start_dir(self, project_dir: Path) -> str:
        """The folder the picker opens in, or ``""`` when the asked one
        isn't a directory (the picker then chooses its own)."""
        start = Path(self.start or project_dir).expanduser()
        return str(start) if start.is_dir() else ""


class PromoteAgentArgs(PanelArgs):
    """``promote_ephemeral_agent``: the agent, and ``"project"`` (the
    default) or ``"personal"``."""

    name: str
    target: str | None = None

    @property
    def destination(self) -> str:
        return self.target or "project"


class AgentNameArgs(PanelArgs):
    """``discard_ephemeral_agent``: the ephemeral agent to drop."""

    name: str


class VisualizationActionArgs(PanelArgs):
    """``dispatch_visualization_action``: a component action and the
    JSON params the view attached to it."""

    action: str = ""
    params: dict[str, JsonValue] | None = None


class ProcessTailArgs(PanelArgs):
    """``read_process_tail``: the background process and how many lines
    (at least one: ``0`` used to answer the whole output)."""

    pid: int
    tail: int = Field(200, ge=1)


class ProcessArgs(PanelArgs):
    """``stop_background_process``: the background process to stop."""

    pid: int
