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
from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
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
from igni_core.backend.schemas_visualization import VisualizationActionResult
from igni_core.backend.server_files import ReadFileResult
from igni_core.core.agents import AgentInfo
from igni_core.core.tools.todo import TodoItemWire
from igni_core.protocol.rpc import RpcMethod


class PanelRpcHandler(RpcHandler):
    """GUI panel + watcher RPC surface."""

    @rpc(RpcMethod.COMPLETE_FILES, args=CompleteFilesArgs)
    async def complete_files(self, args: CompleteFilesArgs) -> FileCompletion:
        return await self._ctx.file_completion.complete(args.query, args.limit)

    @rpc(RpcMethod.READ_FILE, args=ReadFileArgs)
    def read_file(self, args: ReadFileArgs) -> ReadFileResult:
        return self._ctx.backend.read_file(args.path)

    @rpc(RpcMethod.SEARCH_CODE, args=SearchCodeArgs)
    def search_code(self, args: SearchCodeArgs) -> SearchCodeResult:
        return self._controllers.search.search_code(args.query)

    @rpc(RpcMethod.RUN_SHELL, args=RunShellArgs)
    async def run_shell(self, args: RunShellArgs) -> RunShellResult:
        """``$``-prefix shell mode. Captured (non-interactive) by
        design — parity with the TUI's inline shell for the common
        cases."""
        return await self._ctx.shell_runner.run(args.command)

    @rpc(RpcMethod.LIST_DIRS, args=ListDirsArgs)
    async def list_dirs(self, args: ListDirsArgs) -> DirListResult:
        """Subdirectory listing for the GUI folder browser.

        Same trust level as ``run_shell`` (local user over loopback).
        Dot-dirs are filtered unless ``show_hidden`` — the browser is
        for picking project roots, not spelunking.
        """
        return await DirScanner(args.root, args.show_hidden).scan_async()

    @rpc(RpcMethod.PICK_DIR_NATIVE, args=PickDirArgs)
    async def pick_dir_native(self, args: PickDirArgs) -> PickDirResult:
        """Open the OS folder picker on this machine, return the path.

        The BE always runs on the user's machine (loopback-only
        transport), so the dialog appears on their desktop — even
        when the view is a plain browser tab that could never get a
        real path out of its own sandboxed file dialogs. No timeout:
        the user may take their time; the FE uses a long RPC timeout
        for this call.
        """
        start_dir = args.start_dir(self._ctx.backend.project_dir)
        return await NativeFolderPicker.for_platform(start_dir).pick()

    @rpc(RpcMethod.GET_AGENT_DETAILS)
    def get_agent_details(self, args: WireArgs) -> list[AgentInfo]:
        return self._ctx.backend.get_agent_details()

    @rpc(RpcMethod.PROMOTE_EPHEMERAL_AGENT, args=PromoteAgentArgs)
    def promote_ephemeral_agent(self, args: PromoteAgentArgs) -> PromoteEphemeralResult:
        return self._ctx.backend.promote_ephemeral_agent(args.name, args.destination)

    @rpc(RpcMethod.DISCARD_EPHEMERAL_AGENT, args=AgentNameArgs)
    def discard_ephemeral_agent(self, args: AgentNameArgs) -> DiscardEphemeralResult:
        return self._controllers.panels.discard_ephemeral_agent(args.name)

    @rpc(RpcMethod.GET_SLASH_COMMANDS)
    def get_slash_commands(self, args: WireArgs) -> list[SlashCommandEntry]:
        return self._ctx.backend.get_slash_commands()

    @rpc(RpcMethod.GET_TODOS)
    def get_todos(self, args: WireArgs) -> list[TodoItemWire]:
        return self._ctx.backend.get_todos()

    @rpc(RpcMethod.DISPATCH_VISUALIZATION_ACTION, args=VisualizationActionArgs)
    def dispatch_visualization_action(
        self, args: VisualizationActionArgs
    ) -> VisualizationActionResult:
        return self._ctx.backend.dispatch_visualization_action(args.action, args.params)

    @rpc(RpcMethod.LIST_BACKGROUND_PROCESSES)
    def list_bg_processes(self, args: WireArgs) -> list[ProcessRow]:
        return self._ctx.backend.list_background_processes()

    @rpc(RpcMethod.READ_PROCESS_TAIL, args=ProcessTailArgs)
    def read_process_tail(self, args: ProcessTailArgs) -> ProcessTailResult:
        return self._controllers.processes.read_tail(pid=args.pid, tail=args.tail)

    @rpc(RpcMethod.STOP_BACKGROUND_PROCESS, args=ProcessArgs)
    async def stop_bg_process(self, args: ProcessArgs) -> StopProcessResult:
        return await self._controllers.processes.stop(pid=args.pid)

    @rpc(RpcMethod.GET_OUTPUT_STYLES)
    def get_output_styles(self, args: WireArgs) -> OutputStylesResult:
        return self._ctx.backend.get_output_styles()

    @rpc(RpcMethod.GET_GROUP_POLICY)
    def get_group_policy(self, args: WireArgs) -> GroupPolicyPackResult:
        return self._ctx.backend.get_group_policy()
