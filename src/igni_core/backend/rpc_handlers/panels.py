"""GUI-panel RPC handlers (file picker, shell, folder browse, agents,
slash commands, todos, viz, background processes, output styles).

This is the biggest handler cluster by method count because it maps
to a single conceptual thing — "everything the GUI panels need to
render themselves that isn't in another well-defined subsystem". If
this class approaches the Pattern-8 ceiling in the future, split off
the watcher/panel cluster.
"""

from __future__ import annotations

from igni_core.backend.dir_scanner import DirScanner
from igni_core.backend.folder_picker import NativeFolderPicker
from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.rpc_handlers.panel_args import (
    AgentNameArgs,
    CompleteFilesArgs,
    ListDirsArgs,
    PickDirArgs,
    ProcessArgs,
    ProcessTailArgs,
    PromoteAgentArgs,
    ReadFileArgs,
    RunShellArgs,
    SearchCodeArgs,
    VisualizationActionArgs,
)
from igni_core.backend.schemas_panels import (
    DiscardEphemeralResult,
    OutputStylesResult,
    PromoteEphemeralResult,
    SlashCommandEntry,
)
from igni_core.backend.schemas_processes import ProcessRow, ProcessTailResult, StopProcessResult
from igni_core.backend.schemas_rpc import (
    DirListResult,
    FileCompletion,
    GroupPolicyPackResult,
    PickDirResult,
    RunShellResult,
)
from igni_core.backend.schemas_search import SearchCodeResult
from igni_core.backend.schemas_visualization import (
    VisualizationAction,
    VisualizationActionResult,
)
from igni_core.backend.server_files import ReadFileResult
from igni_core.core.agents import AgentInfo
from igni_core.core.tools.todo import TodoItemWire
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class PanelRpcHandler(RpcHandler):
    """GUI panel + watcher RPC surface."""

    @rpc(
        RpcMethod.COMPLETE_FILES,
        duration=RpcDuration.IO,
        args=CompleteFilesArgs,
        scope=RpcScope.SESSION,
    )
    async def complete_files(self, args: CompleteFilesArgs) -> FileCompletion:
        """Complete a file path typed after ``@``, from this session's project.

        ``query`` is what was typed so far (empty matches everything);
        ``limit`` caps the matches returned (default 50). Returns the
        matching project-relative paths, best first, and ``total``: how
        many matched before the cap. The project's file list is read once
        per session.
        """
        return await self._ctx.file_completion.complete(args.query, args.limit)

    @rpc(RpcMethod.READ_FILE, duration=RpcDuration.IO, args=ReadFileArgs, scope=RpcScope.SESSION)
    def read_file(self, args: ReadFileArgs) -> ReadFileResult:
        """Read a file for preview: one of this session's project, igni's data, or an attachment.

        ``path`` is relative to the project, absolute, or an
        ``attachment:<id>`` reference. Returns the resolved ``path``, its
        ``contents`` and ``size``, and a ``language`` hint for highlighting;
        or ``error`` when the file is missing, a directory, too large to
        preview (256 KiB), unreadable, or outside the folders igni lets a
        client read.
        """
        return self._ctx.backend.read_file(args.path)

    @rpc(
        RpcMethod.SEARCH_CODE, duration=RpcDuration.IO, args=SearchCodeArgs, scope=RpcScope.SESSION
    )
    async def search_code(self, args: SearchCodeArgs) -> SearchCodeResult:
        """Find where a snippet of code appears in this session's project.

        ``query`` is the snippet, matched exactly; one shorter than five
        characters finds nothing. Returns up to 20 ``matches`` (path, first
        and last line, a preview), ``truncated`` when there may be more or
        the search timed out, and ``error`` when it failed.
        """
        return await self._controllers.search.search_code(args.query)

    @rpc(
        RpcMethod.RUN_SHELL,
        duration=RpcDuration.LONG,
        args=RunShellArgs,
        scope=RpcScope.SESSION,
        streams=(
            PushChannel.PROCESS_STARTED,
            PushChannel.PROCESS_LINE,
            PushChannel.PROCESS_EXITED,
            PushChannel.BACKGROUND_PROCESS_DONE,
        ),
    )
    async def run_shell(self, args: RunShellArgs) -> RunShellResult:
        """Run a shell command in this session's project folder and return its output.

        ``command`` is the command line (what follows ``$`` in the
        composer); blank runs nothing. Output is captured, not interactive.
        Returns ``output`` (the tail, at most 100 KB) and ``exit_code``
        (``-1`` when it could not start). A command still running after two
        minutes moves to the background: the answer then comes at once with
        ``exit_code`` 0 and a note, and the process goes on as a background
        process — ``process_started``, a ``process_line`` per line,
        ``process_exited`` and ``background_process_done``.
        """
        return await self._ctx.shell_runner.run(args.command)

    @rpc(RpcMethod.LIST_DIRS, duration=RpcDuration.IO, args=ListDirsArgs, scope=RpcScope.IGNI)
    async def list_dirs(self, args: ListDirsArgs) -> DirListResult:
        """List the folders inside a folder on igni's machine, for picking a project.

        ``path`` is the folder to list (the home folder when absent or
        empty); ``show_hidden`` includes folders whose names start with a
        dot. Returns the resolved ``path``, its ``parent`` (empty at the
        root), the folder names in it (``dirs``, sorted), the home folder,
        and ``error`` when it could not be read.
        """
        return await DirScanner(args.root, args.show_hidden).scan_async()

    @rpc(
        RpcMethod.PICK_DIR_NATIVE,
        duration=RpcDuration.PERSON,
        args=PickDirArgs,
        scope=RpcScope.SESSION,
    )
    async def pick_dir_native(self, args: PickDirArgs) -> PickDirResult:
        """Open the operating system's folder picker on igni's machine and return the choice.

        ``start`` is the folder the picker opens in (this session's project
        when absent or empty). igni runs on the person's machine, so the
        dialog appears on their desktop; it waits as long as they take, so
        give the call a long timeout. Returns the chosen ``path``, or
        ``cancelled``, or ``error`` when no native dialog is available.
        """
        start_dir = args.start_dir(self._ctx.backend.project_dir)
        return await NativeFolderPicker.for_platform(start_dir).pick()

    @rpc(RpcMethod.GET_AGENT_DETAILS, args=NoArgs, scope=RpcScope.SESSION)
    def get_agent_details(self, args: NoArgs) -> list[AgentInfo]:
        """List the agents this session can hand work to.

        Takes no arguments. Returns each agent's name, description, tools,
        model, MCP servers, tags, system prompt, where it was defined, and
        whether it is ephemeral (made during this session, not yet saved).
        """
        return self._ctx.backend.get_agent_details()

    @rpc(
        RpcMethod.PROMOTE_EPHEMERAL_AGENT,
        duration=RpcDuration.IO,
        args=PromoteAgentArgs,
        scope=RpcScope.SESSION,
    )
    def promote_ephemeral_agent(self, args: PromoteAgentArgs) -> PromoteEphemeralResult:
        """Keep an agent made during this session, as a project or personal agent.

        ``name`` is the ephemeral agent; ``target`` is ``project`` (the
        default: the project's agents) or ``personal`` (the person's own).
        Returns ``ok`` with the agent's ``name`` and where it was saved
        (``dest``); or ``ok: false`` with ``reason`` when the agent is
        unknown, not ephemeral, or the destination exists.
        """
        return self._ctx.backend.promote_ephemeral_agent(args.name, args.destination)

    @rpc(
        RpcMethod.DISCARD_EPHEMERAL_AGENT,
        duration=RpcDuration.IO,
        args=AgentNameArgs,
        scope=RpcScope.SESSION,
    )
    def discard_ephemeral_agent(self, args: AgentNameArgs) -> DiscardEphemeralResult:
        """Delete an agent made during this session.

        ``name`` is the ephemeral agent. Returns ``ok`` with its ``name``;
        or ``ok: false`` with ``reason`` when it is unknown or not
        ephemeral.
        """
        return self._controllers.panels.discard_ephemeral_agent(args.name)

    @rpc(RpcMethod.GET_SLASH_COMMANDS, args=NoArgs, scope=RpcScope.SESSION)
    def get_slash_commands(self, args: NoArgs) -> list[SlashCommandEntry]:
        """List every slash command this session takes.

        Takes no arguments. Returns igni's built-in commands, commands
        written as markdown files, skills a person can invoke, and
        extensions' commands, told apart by ``source``; each with its
        ``name``, ``description`` and ``argument_hint``.
        """
        return self._ctx.backend.get_slash_commands()

    @rpc(RpcMethod.GET_TODOS, args=NoArgs, scope=RpcScope.SESSION)
    def get_todos(self, args: NoArgs) -> list[TodoItemWire]:
        """Read the agent's to-do list in this session.

        Takes no arguments. Returns each item's ``content``, ``status``
        (``pending``, ``in_progress``, ``completed``) and ``activeForm``
        (how it reads while in progress). ``todos_updated`` pushes the list
        whenever the agent changes it.
        """
        return self._ctx.backend.get_todos()

    @rpc(
        RpcMethod.DISPATCH_VISUALIZATION_ACTION,
        args=VisualizationActionArgs,
        scope=RpcScope.SESSION,
        streams=(PushChannel.VISUALIZATION_ACTION_DISPATCHED,),
    )
    def dispatch_visualization_action(
        self, args: VisualizationActionArgs
    ) -> VisualizationActionResult:
        """Tell this session the person used a control in a view the agent drew.

        ``action`` is the control's action name; ``params`` the values the
        view attached to it (JSON). The action is kept for the agent and
        pushed to every window as ``visualization_action_dispatched``.
        Returns the action and params as recorded, with ``ok``.
        """
        return self._ctx.backend.dispatch_visualization_action(
            VisualizationAction(action=args.action, params=args.params or {})
        )

    @rpc(RpcMethod.LIST_BACKGROUND_PROCESSES, args=NoArgs, scope=RpcScope.IGNI)
    def list_bg_processes(self, args: NoArgs) -> list[ProcessRow]:
        """List the background processes igni is tracking, in every session.

        Takes no arguments. Returns each process's ``pid``, command, how
        long it has run, whether it is still running, and its exit code
        once it has ended.
        """
        return self._ctx.backend.list_background_processes()

    @rpc(RpcMethod.READ_PROCESS_TAIL, args=ProcessTailArgs, scope=RpcScope.IGNI)
    def read_process_tail(self, args: ProcessTailArgs) -> ProcessTailResult:
        """Read the last lines a background process printed.

        ``pid`` is the process; ``tail`` how many lines (default 200).
        Returns its ``output``, whether it ``is_running``, and its
        ``exit_code`` once it has ended. An unknown ``pid`` answers empty
        output, not running.
        """
        return self._controllers.processes.read_tail(pid=args.pid, tail=args.tail)

    @rpc(
        RpcMethod.STOP_BACKGROUND_PROCESS,
        duration=RpcDuration.IO,
        args=ProcessArgs,
        scope=RpcScope.IGNI,
        streams=(
            PushChannel.PROCESS_LINE,
            PushChannel.PROCESS_EXITED,
            PushChannel.BACKGROUND_PROCESS_DONE,
        ),
    )
    async def stop_bg_process(self, args: ProcessArgs) -> StopProcessResult:
        """Stop a background process.

        ``pid`` is the process. Returns ``killed: true`` once it was told to
        stop — its last lines, ``process_exited`` and
        ``background_process_done`` follow — or ``killed: false`` with a
        ``message`` when igni is not tracking it or it had already ended.
        """
        return await self._controllers.processes.stop(pid=args.pid)

    @rpc(RpcMethod.GET_OUTPUT_STYLES, args=NoArgs, scope=RpcScope.SESSION)
    def get_output_styles(self, args: NoArgs) -> OutputStylesResult:
        """List the output styles this session can answer in, and the active one.

        Takes no arguments. Returns the ``active`` style's name (empty for
        none) and every style's ``name`` and ``description``.
        """
        return self._ctx.backend.get_output_styles()

    @rpc(RpcMethod.GET_GROUP_POLICY, duration=RpcDuration.IO, args=NoArgs, scope=RpcScope.IGNI)
    def get_group_policy(self, args: NoArgs) -> GroupPolicyPackResult:
        """Read which organisation group's policy igni follows.

        Takes no arguments. Returns the group's id and name, when its
        policy pack was fetched, how many entries it has and the default
        model it sets; nulls and zero when igni follows no group.
        """
        return self._ctx.backend.get_group_policy()
