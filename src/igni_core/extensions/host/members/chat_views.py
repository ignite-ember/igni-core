"""``host.chat``'s json-render views: an extension's own, and acting on any."""

from __future__ import annotations

import json
import uuid
from typing import Any

from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.extensions.host.broadcasts.views import ViewShown
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.chat import DispatchViewArgs
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod


class ChatViewMembers(Members):
    """The views in a chat: building the event, minting ids, recording owners."""

    @call("chat.append")
    async def append(self, c: Call) -> str:
        # A new card each time: two appends of the same view are two views.
        view_id = f"ext-{uuid.uuid4().hex[:12]}"
        await self.show(c, view_id, c.params["view"], str(c.params.get("title") or ""))
        self.host.state.view_owners.own(view_id, c.extension_id)
        return view_id

    @call("chat.replace")
    async def replace(self, c: Call) -> None:
        view_id = str(c.params["view_id"])
        owner = self.host.state.view_owners.owner_of(view_id)
        if owner is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no view {view_id!r} was appended")
        if owner != c.extension_id:
            raise HostModelError(
                ErrorCode.REFUSED, f"view {view_id!r} belongs to another extension"
            )
        await self.show(c, view_id, c.params["view"], str(c.params.get("title") or ""))

    async def show(self, c: Call, view_id: str, spec: Any, title: str) -> None:
        if not isinstance(spec, dict) or not isinstance(spec.get("root"), str):
            raise HostModelError(
                ErrorCode.INVALID,
                "a view is a json-render spec: {root: <id>, elements: {<id>: {type, props, children}}}",
            )
        # The card as the session's log keeps it: igni's own
        # ``visualization_delta``, final, labelled with its title and source.
        card = VisualizationDeltaPayload(
            agent_path=c.own_client_id,
            spec_id=view_id,
            spec_json=json.dumps(spec),
            final=True,
            title=title,
            source=c.extension_id,
        )
        await self.host.backend.show_view(
            session_id=c.session_id,
            payload=card,
            run_id=self.host.state.runs.last_run_of(c.session_id),
        )

    @get("chat.views.list")
    async def view_list(self, c: Call) -> list[ViewShown]:
        return self.host.backend.views(c.session_id)

    @call("chat.views.dispatch")
    async def dispatch(self, c: Call) -> Any:
        args = DispatchViewArgs(
            action=c.params["action"],
            params=dict(c.params.get("params") or {}),
            view_id=c.params["view_id"],
        )
        return await self.rpc(RpcMethod.DISPATCH_VISUALIZATION_ACTION, args, c=c)


__all__ = ["ChatViewMembers"]
