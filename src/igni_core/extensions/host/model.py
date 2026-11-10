"""The host object model, served: its composition root and its request dispatch.

:meth:`HostModel.build` wires the model from a
:class:`~.model_config.HostModelConfig`. :meth:`HostModel.handle`
answers one request from one :class:`~.caller.Caller`: a subscription
goes to the signal bus; anything else is bound against the grants and
the schema and run by the member that implements it. A refusal is a
reply, never a raise.
"""

from __future__ import annotations

import logging
from typing import Any

from igni_core.extensions.host.broadcasts import BroadcastSignals
from igni_core.extensions.host.call_scope import CallScope
from igni_core.extensions.host.caller import Caller
from igni_core.extensions.host.display import DisplayReader
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.member_table import MemberTable
from igni_core.extensions.host.members import ALL_MEMBERS
from igni_core.extensions.host.members.tag import MemberKey
from igni_core.extensions.host.model_config import HostModelConfig
from igni_core.extensions.host.page_channel import PageChannel
from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.request_binder import RequestBinder
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.host.session_target import SessionTargets
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import ErrorBody, Op, Reply, Request

logger = logging.getLogger(__name__)


class HostModel:
    """Every extension's view of igni, through one schema."""

    def __init__(
        self,
        *,
        services: HostServices,
        broadcasts: BroadcastSignals,
        binder: RequestBinder,
        members: MemberTable,
    ) -> None:
        self.services = services
        self.broadcasts = broadcasts
        #: Public: a test swaps its schema for one with a stage-2 member.
        self.binder = binder
        self._members = members
        self._pages = PageChannel(services.bus, services.pushes, self.handle)

    @classmethod
    def build(cls, config: HostModelConfig) -> HostModel:
        """The composition root: every collaborator, built once and wired."""
        services = HostServices.build(config)
        broadcasts = BroadcastSignals(services, display=DisplayReader(services.gateway).read)
        # Every write a member makes through the backend is a signal too.
        services.gateway.on_write(broadcasts.after_rpc)
        binder = RequestBinder(
            config.host_schema,
            services.types,
            CallScope(services.windows, config.backend, SessionTargets(services)),
        )
        built = [each(services) for each in ALL_MEMBERS]
        members = MemberTable.of(built, services.types, config.strict)
        return cls(services=services, broadcasts=broadcasts, binder=binder, members=members)

    def implemented(self) -> set[MemberKey]:
        """Every built member, for the coverage test."""
        return self._members.implemented()

    # ── Requests ──────────────────────────────────────────────────

    async def handle(self, request: Request, caller: Caller) -> Reply:
        """Answer one request. Never raises: a refusal is a reply."""
        try:
            return await self._answer(request, caller)
        except HostModelError as exc:
            return Reply.refused(request.id, exc.body())
        except Exception as exc:
            logger.exception("host model: %s %s failed", request.op.value, request.path)
            return Reply.refused(
                request.id, ErrorBody(code=ErrorCode.FAILED, message=f"{request.path}: {exc}")
            )

    async def handle_page(self, extension_id: str, args: dict[str, Any]) -> Reply:
        """One request from an extension's page, through the bridge (:class:`PageChannel`)."""
        return await self._pages.answer(extension_id, args)

    async def _answer(self, request: Request, caller: Caller) -> Reply:
        grants = self.services.extensions.require_grants(caller.extension_id)
        if isinstance(grants, ErrorBody):
            return Reply.refused(request.id, grants)
        if request.op is Op.ANSWER:
            # The process's socket (HostChannel) and the bridge (PageChannel)
            # each take an answer before it reaches the model.
            return Reply.refused(
                request.id,
                ErrorBody(code=ErrorCode.INVALID, message="answer is not a host-model request"),
            )
        if request.op in (Op.SUBSCRIBE, Op.UNSUBSCRIBE):
            if caller.subscriber is None:
                return Reply.refused(
                    request.id,
                    ErrorBody(
                        code=ErrorCode.INVALID, message="this channel does not take subscriptions"
                    ),
                )
            outcome = self.services.bus.answer(caller.subscriber, request)
            return Reply.answered(request.id, plain(outcome))
        bound = await self.binder.bind(caller, grants, request)
        # The one place a member's answer becomes plain JSON for the wire.
        return Reply.answered(request.id, plain(await self._members.run(request, bound)))


__all__ = ["HostModel"]
