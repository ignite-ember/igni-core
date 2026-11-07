"""The host object model, served.

:class:`HostModel` answers an extension's requests — ``get``, ``set``,
``call``, ``subscribe`` — from both channels (the process's loopback
socket, :mod:`.channel`, and the page's bridge, the ``host_model`` RPC,
:mod:`.page_channel`), checks every one against the extension's grants
and the schema (:mod:`.request_binder`), and runs the member that
implements it (:mod:`.members`, :mod:`.member_table`). It also turns
what igni does into signals: every message igni sends a window, every
write a window makes, every extension state change (:mod:`.broadcasts`,
:mod:`.observations`, :mod:`.window_signals`).

The schema is the authority. A member it does not define does not
exist; a stage-2 member answers that it is not yet available; a member
not granted answers ``not_granted`` and its signals never arrive.

The model composes; the collaborators its members and broadcasts share
are one :class:`~.services.HostServices` record.
"""

from __future__ import annotations

import logging
from typing import Any

from igni_core.core.decisions import Laya
from igni_core.extensions.host.backend import HostBackend
from igni_core.extensions.host.broadcasts import BroadcastSignals
from igni_core.extensions.host.call_scope import CallScope
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.member_table import MemberTable
from igni_core.extensions.host.members import ALL_MEMBERS
from igni_core.extensions.host.members.app import AppMembers
from igni_core.extensions.host.page_channel import PageChannel
from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.request_binder import RequestBinder
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.host.session_target import SessionTargets
from igni_core.extensions.host.signals import Subscriber
from igni_core.extensions.host.spec import ErrorCode, HostSchema
from igni_core.extensions.host.wire import ErrorBody, Op, Reply, Request
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver

logger = logging.getLogger(__name__)


class HostModel:
    """Every extension's view of igni, through one schema."""

    def __init__(
        self,
        *,
        backend: HostBackend,
        registry: ExtensionRegistry,
        store: ExtensionStore | None = None,
        notices: ExtensionNoticeReceiver | None = None,
        decisions: Laya | None = None,
        schema: HostSchema = HOST_SCHEMA,
        strict: bool = False,
    ) -> None:
        """
        Args:
            notices: For ``host.notifications.notify`` — the same check a
                notice arriving over MCP passes.
            decisions: Laya, for ``host.decide``; the process's shared
                one unless a test brings its own.
            strict: Check every answer against the schema and fail loudly
                when it does not fit. For tests: in production an answer
                is sent as the backend gave it.
        """
        services = HostServices.build(
            backend=backend,
            registry=registry,
            store=store,
            notices=notices,
            decisions=decisions,
            schema=schema,
        )
        self.services = services
        self.broadcasts = BroadcastSignals(services, display=AppMembers(services).display_value)
        # Every write a member makes through the backend is a signal too.
        services.gateway.on_write(self.broadcasts.after_rpc)
        self.binder = RequestBinder(
            schema,
            services.types,
            CallScope(services.windows, backend, SessionTargets(services)),
        )
        self.members = MemberTable.build(ALL_MEMBERS, services, services.types, strict)
        self.pages = PageChannel(services.bus, services.pushes, self.handle)

    def implemented(self) -> set[tuple[str, str]]:
        """``(op, member path)`` for every built member, for the coverage test."""
        return self.members.implemented()

    # ── Requests ──────────────────────────────────────────────────

    async def handle(
        self,
        extension_id: str,
        request: Request,
        *,
        subscriber: Subscriber | None = None,
        home_window: str | None = None,
    ) -> Reply:
        """Answer one request. Never raises: a refusal is a reply.

        ``home_window`` is the window the caller lives in — a page's
        own window; ``None`` for a process, whose ``host.ui`` is the
        active window.
        """
        try:
            result = await self._handle(extension_id, request, subscriber, home_window)
        except HostModelError as exc:
            return Reply(
                id=request.id, ok=False, error=ErrorBody(code=exc.code, message=exc.message)
            )
        except Exception as exc:
            logger.exception("host model: %s %s failed", request.op.value, request.path)
            return Reply(
                id=request.id,
                ok=False,
                error=ErrorBody(code=ErrorCode.FAILED, message=f"{request.path}: {exc}"),
            )
        return Reply(id=request.id, ok=True, result=result)

    async def handle_page(self, extension_id: str, args: dict[str, Any]) -> Reply:
        """One request from an extension's page, through the bridge (:class:`PageChannel`)."""
        return await self.pages.answer(extension_id, args)

    async def _handle(
        self,
        extension_id: str,
        request: Request,
        subscriber: Subscriber | None,
        home_window: str | None,
    ) -> Any:
        grants = self.services.extensions.grants_for(extension_id)
        if grants is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no extension {extension_id!r} is installed")
        if request.op is Op.ANSWER:
            raise HostModelError(
                ErrorCode.INVALID, "only an extension's process answers a hook event; a page cannot"
            )
        if request.op in (Op.SUBSCRIBE, Op.UNSUBSCRIBE):
            return self._subscription(request, subscriber)
        bound = await self.binder.bind(extension_id, grants, request, home_window)
        # The one place a member's answer becomes plain JSON for the wire.
        return plain(await self.members.run(request, bound))

    def _subscription(self, request: Request, subscriber: Subscriber | None) -> Any:
        if subscriber is None:
            raise HostModelError(ErrorCode.INVALID, "this channel does not take subscriptions")
        if request.op is Op.SUBSCRIBE:
            return dict(self.services.bus.subscribe(subscriber, request.signals))
        self.services.bus.unsubscribe(subscriber, request.signals)
        return None


__all__ = ["HostModel"]
