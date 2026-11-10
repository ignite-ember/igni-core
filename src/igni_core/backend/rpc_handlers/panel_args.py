"""The wire arguments of the GUI-panel RPCs, validated before
:class:`~igni_core.backend.rpc_handlers.panels.PanelRpcHandler` sees them."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class PanelArgs(BaseModel):
    """Base for every panel RPC's arguments: keys a newer client adds
    are ignored rather than refused."""

    model_config = ConfigDict(extra="ignore")


class CompleteFilesArgs(PanelArgs):
    """Arguments for completing a file path typed after ``@``."""

    query: str = Field(default="", description="What was typed so far; empty matches every file.")
    limit: int = Field(default=50, description="Maximum number of matches to return.")


class ReadFileArgs(PanelArgs):
    """Arguments for reading a file to preview it."""

    path: str = Field(
        description="The file: relative to the project, absolute, or an `attachment:<id>` reference."
    )


class SearchCodeArgs(PanelArgs):
    """Arguments for finding where a code snippet appears in the project.

    Unknown keys are refused."""

    # A key it does not name is refused, not ignored: a call that sent the
    # snippet under another name (``snippet``) was told only that ``query``
    # was missing, and searched for nothing.
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        description="The snippet, matched exactly; shorter than 5 characters finds nothing."
    )


class RunShellArgs(PanelArgs):
    """Arguments for running a shell command in the project folder."""

    command: str = Field(
        default="",
        description="The command line to run (without a leading `$`); blank runs nothing.",
    )


class ListDirsArgs(PanelArgs):
    """Arguments for listing the folders inside a folder."""

    path: str | None = Field(
        default=None, description="The folder to list; the home folder when absent or empty."
    )
    show_hidden: bool = Field(
        default=False, description="Include folders whose names start with a dot."
    )

    @property
    def root(self) -> Path:
        return Path(self.path) if self.path else Path.home()


class PickDirArgs(PanelArgs):
    """Arguments for opening the operating system's folder picker."""

    start: str | None = Field(
        default=None,
        description="Folder the picker opens in; the session's project folder when absent or empty.",
    )

    def start_dir(self, project_dir: Path) -> str:
        """The folder the picker opens in, or ``""`` when the asked one
        isn't a directory (the picker then chooses its own)."""
        start = Path(self.start or project_dir).expanduser()
        return str(start) if start.is_dir() else ""


class PromoteAgentArgs(PanelArgs):
    """Arguments for keeping an agent created during the session."""

    name: str = Field(description="Name of the agent created during the session.")
    target: str | None = Field(
        default=None,
        description=(
            "Where to save it: `project` (the project's agents; the default) or "
            "`personal` (the user's own agents)."
        ),
    )

    @property
    def destination(self) -> str:
        return self.target or "project"


class AgentNameArgs(PanelArgs):
    """Arguments naming an agent created during the session."""

    name: str = Field(description="Name of the agent created during the session.")


class VisualizationActionArgs(PanelArgs):
    """Arguments for reporting that the user used a control in a
    visualization the agent drew."""

    action: str = Field(default="", description="The control's action name.")
    params: dict[str, JsonValue] | None = Field(
        default=None, description="Values the visualization attached to the action; omit for none."
    )


class ProcessTailArgs(PanelArgs):
    """Arguments for reading the latest output of a background process."""

    # ``tail`` is at least one: ``0`` used to answer the whole output.
    pid: int = Field(description="Operating-system process id of the background process.")
    tail: int = Field(
        default=200, ge=1, description="How many of the last lines to return (at least 1)."
    )


class ProcessArgs(PanelArgs):
    """Arguments naming a background process."""

    pid: int = Field(description="Operating-system process id of the background process.")
