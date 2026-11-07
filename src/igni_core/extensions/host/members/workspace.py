"""``host.workspace``: the session's project, as far as the grant reaches."""

from __future__ import annotations

import asyncio
from typing import Any

from igni_core.backend.file_opener import FileOpener
from igni_core.backend.workspace_watch import GitWorkspace
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import WORKSPACE
from igni_core.extensions.host.members.base import Call, Members, call, get
from igni_core.extensions.host.members.rpc_args import CompleteArgs, PathArgs, QueryArgs
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import HeadInfo
from igni_core.extensions.host.workspace_scope import WorkspaceScope
from igni_core.protocol.rpc import RpcMethod


class WorkspaceMembers(Members):
    def scope(self, c: Call) -> WorkspaceScope:
        return WorkspaceScope(
            self.host.backend.project_dir_of(c.session_id), c.grants.workspace, c.extension_id
        )

    @get("workspace.root")
    async def root_value(self, c: Call) -> str:
        return str(self.scope(c).root)

    @get("workspace.head")
    async def head(self, c: Call) -> HeadInfo | None:
        head = GitWorkspace(self.scope(c).root).head()
        return None if head is None else HeadInfo.of(head)

    @call("workspace.read")
    async def read(self, c: Call) -> Any:
        relative = self.scope(c).relative(str(c.params["path"]))
        return await self.rpc(RpcMethod.READ_FILE, PathArgs(path=relative), c=c)

    @call("workspace.list")
    async def list_dir(self, c: Call) -> Any:
        scope = self.scope(c)
        relative = scope.relative(str(c.params.get("dir") or "."), directory=True)
        found = await self.rpc(RpcMethod.LIST_DIRS, PathArgs(path=str(scope.root / relative)), c=c)
        return scope.visible_dirs(relative, found)

    @call("workspace.complete")
    async def complete(self, c: Call) -> Any:
        args = CompleteArgs(query=c.params["prefix"], limit=50)
        return self.scope(c).visible_completions(
            await self.rpc(RpcMethod.COMPLETE_FILES, args, c=c)
        )

    @call("workspace.search")
    async def search(self, c: Call) -> Any:
        args = QueryArgs(query=c.params["query"])
        return self.scope(c).visible_hits(await self.rpc(RpcMethod.SEARCH_CODE, args, c=c))

    @call("workspace.pick_dir")
    async def pick_dir(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.PICK_DIR_NATIVE, c=c)

    @call("workspace.open")
    async def open_file(self, c: Call) -> None:
        """Open a document the workspace grant reaches, as the person would.

        The extension's own process is sandboxed and cannot launch
        anything; igni can, so it opens the file — and only a document,
        never a program, a script or a bundle, because whatever opens it
        runs outside that sandbox.
        """
        if not c.grants.workspace.granted:
            raise HostModelError.not_granted(c.extension_id, "workspace.open", WORKSPACE)
        scope = self.scope(c)
        relative = scope.relative(str(c.params["path"]))
        target = scope.document(relative)
        if not await asyncio.to_thread(FileOpener.open, target):
            raise HostModelError(ErrorCode.FAILED, f"nothing on this machine opened {relative!r}")


__all__ = ["WorkspaceMembers"]
