"""Extensions answering igni's hook events.

igni's hook events (``docs/HOOKS.md``) are where anyone outside the
agent gets a say. A settings file's command or HTTP hook is one way in;
an extension's process is the other: it lists the events in
``permissions.hooks`` and answers them with ``@ext.hook`` in the
``igni-extension`` SDK, over the loopback socket igni hands it
(:mod:`.channel`).

The core never imports this. It asks
:data:`~igni_core.core.hooks.extension_gate.extension_hook_gate`, and the
backend installs :class:`ExtensionHooks` there. On a deciding event the
host asks every extension that hooks it, in the super admin's priority
order, and waits for each: allow, reject with a reason, or — where the
event has something to change — the changed value. A reject ends the
chain; a change is what the next one is handed. Every other event is
told, and nothing waits. Several extensions hooking one event never
collide: approving a service covers what its extension hooks, and the
priority order settles who goes first.

**The host decides what a silence means**, never the extension: an
extension that does not answer within :attr:`ExtensionHooks.TIMEOUT_S`,
whose process is not running or not connected, or that answers in a
shape the event does not take, has failed, and the event's fail mode
says what follows (:class:`~igni_core.core.hooks.extension_spec.HookFailMode`).
A page cannot answer a hook: igni would then wait on whatever tab
happens to be open.

The hooks are one instance, owned by whoever built the backend
(:class:`~igni_core.backend.extensions_runtime.ExtensionsRuntime`); the
processes it talks to are theirs
(:class:`~igni_core.extensions.host.hook_exchange.HookConnections`),
and what an answer means is read by
:class:`~igni_core.extensions.host.answers.HookAnswers`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import ExtensionHookVerdict
from igni_core.core.hooks.extension_spec import DECIDING, HookFailMode
from igni_core.core.hooks.extension_values import HookValueT
from igni_core.core.hooks.schemas import HookPayload
from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.host.answers import HookAnswers
from igni_core.extensions.host.hook_exchange import HookConnections, HookFailure
from igni_core.extensions.host.tools import ExtensionTools
from igni_core.extensions.host.wire import HookAnswer, HookFrameValue, HookMessage
from igni_core.extensions.principal import Principal
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState

logger = logging.getLogger(__name__)


class ExtensionHooks:
    """Every extension's hooks, and the answers they owe.

    The hooks of the extensions in one registry: which of them hook which
    event, in what order they are asked, and what their answers do to the
    event. The frames themselves go out over the processes' sockets, which
    are :class:`~igni_core.extensions.host.hook_exchange.HookConnections`'
    business.
    """

    #: How long one extension has to answer. igni's to set, never an
    #: extension's: an extension that could raise it could stall igni.
    TIMEOUT_S: ClassVar[float] = 2.0

    #: States in which a process that is not connected will not be:
    #: asking would only wait out the deadline.
    _NOT_RUNNING: ClassVar[frozenset[ExtensionState]] = frozenset(
        {ExtensionState.DISABLED, ExtensionState.FAILED}
    )

    def __init__(self, *, registry: ExtensionRegistry, timeout: float | None = None) -> None:
        """
        Args:
            registry: The installed extensions whose hooks these are. Who
                hooks what, and how often each of them failed to answer,
                is read from it.
            timeout: How long one extension has to answer; the default is
                :attr:`TIMEOUT_S`.
        """
        self._registry = registry
        self._tools = ExtensionTools(registry)
        self._timeout = self.TIMEOUT_S if timeout is None else timeout
        self._priority: list[str] = []
        self._principal: Callable[[], Principal | None] = lambda: None
        self._connections = HookConnections()

    # ── Wiring ────────────────────────────────────────────────────

    def set_priority(self, priority: list[str]) -> None:
        """The super admin's priority order of extensions, highest first: hooks run in it."""
        self._priority = list(priority)

    def set_principal(self, principal: Callable[[], Principal | None]) -> None:
        """Who an event is for: asked as each frame goes out, and sent on it."""
        self._principal = principal

    @property
    def _faults(self) -> ExtensionFaults:
        """The counter of the extensions these hooks answer for.

        The hooks are asked about the extensions in their registry, so
        how often one of them failed to answer is that registry's
        count — the same count the extension RPC keeps.
        """
        return self._registry.faults

    def connect(self, extension_id: str, send: Callable[[str], None]) -> None:
        """An extension's process connected: ``send`` puts a frame on its socket."""
        self._connections.connect(extension_id, send)

    def disconnect(self, extension_id: str, send: Callable[[str], None] | None = None) -> None:
        """Its process went away. Whatever it owed an answer to has failed."""
        self._connections.disconnect(extension_id, send)

    def answered(self, extension_id: str, answer: HookAnswer) -> bool:
        """An answer arrived. ``False`` when it answers nothing this extension was asked."""
        return self._connections.answered(extension_id, answer)

    # ── Who hooks what ────────────────────────────────────────────

    def hookers(self, event: HookEvent) -> list[str]:
        """Every extension that hooks ``event``, in the order they are asked."""
        found = []
        for extension_id in self._registry.ids():
            entry = self._registry.get(extension_id)
            if entry is None or entry.state is ExtensionState.DISABLED:
                continue
            if event in entry.manifest.permissions.hooks:
                found.append(extension_id)
        rank = {extension_id: i for i, extension_id in enumerate(self._priority)}
        found.sort(key=lambda extension_id: (rank.get(extension_id, len(rank)), extension_id))
        return found

    def hooks(self, event: HookEvent) -> bool:
        """Whether any extension hooks ``event`` — a fast no for the common case."""
        return bool(self.hookers(event))

    # ── Telling ───────────────────────────────────────────────────

    def tell(self, event: HookEvent, payload: HookPayload) -> None:
        """An event that decides nothing: sent to each extension that hooks it, never awaited."""
        frame = HookMessage(
            event=event.value,
            value=HookFrameValue(
                body=payload.body,
                tool_manifest_name=self._tools.of(payload.body),
                omit_none=True,
            ),
            principal=self._principal(),
        )
        text = frame.model_dump_json()
        for extension_id in self.hookers(event):
            if not self._connections.send(extension_id, text):
                logger.debug("%s hooks %s but is not connected; not told", extension_id, event)

    # ── Its tools ─────────────────────────────────────────────────

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        """The agent called a tool; counted for the extension it is from, if any."""
        extension_id = self._tools.owner(tool_name)
        if extension_id is None:
            return
        if answered:
            self._faults.answered(extension_id)
        else:
            self._faults.failed(extension_id, f"its tool {tool_name}: {detail or 'no answer'}")

    # ── Asking ────────────────────────────────────────────────────

    async def ask(self, value: HookValueT) -> ExtensionHookVerdict[HookValueT]:
        """Ask everyone who hooks the deciding event ``value`` is for.

        The verdict, and the value to go ahead with.
        """
        spec = DECIDING[value.hook_event]
        current = value
        for extension_id in self.hookers(spec.event):
            if self._faults.faulty(extension_id):
                # Not waited on: it has not answered LIMIT times in a row.
                if spec.fails is HookFailMode.OPEN:
                    continue
                return ExtensionHookVerdict(
                    allowed=False,
                    value=current,
                    by=extension_id,
                    reason=self._faults.not_responding(extension_id),
                )
            try:
                answer = await self._ask(extension_id, spec.event, current)
                verdict = HookAnswers.verdict(spec, extension_id, current, answer)
                self._faults.answered(extension_id)
            except HookFailure as exc:
                if exc.unanswered:
                    self._faults.failed(extension_id, f"its {spec.event.value} hook: {exc}")
                else:
                    self._faults.answered(extension_id)
                if spec.fails is HookFailMode.OPEN:
                    logger.warning(
                        "%s did not answer %s (%s); it goes ahead as if allowed",
                        extension_id,
                        spec.event.value,
                        exc,
                    )
                    continue
                return ExtensionHookVerdict(
                    allowed=False,
                    value=current,
                    by=extension_id,
                    reason=f"Not done: the extension {extension_id} checks "
                    f"{spec.event.value} and could not answer ({exc}).",
                )
            if not verdict.allowed:
                return verdict
            current = verdict.value
        return ExtensionHookVerdict(allowed=True, value=current)

    async def ask_one(
        self,
        extension_id: str,
        event: str,
        value: BaseModel,
        *,
        timeout: float,
        connect_timeout: float | None = None,
    ) -> HookAnswer:
        """Ask one extension about something that is its own, not one of igni's hook events.

        A service event (:mod:`igni_core.extensions.service_events`) is
        for the extension whose service sent it, so it is asked whatever
        its manifest hooks, with a deadline of the caller's — work it may
        do before answering is the point. ``connect_timeout`` bounds the
        wait for a process that is not connected yet. Raises
        :class:`HookFailure` when it does not answer.
        """
        return await self._ask(
            extension_id, event, value, timeout=timeout, connect_timeout=connect_timeout
        )

    async def _ask(
        self,
        extension_id: str,
        event: HookEvent | str,
        value: BaseModel,
        *,
        timeout: float | None = None,
        connect_timeout: float | None = None,
    ) -> HookAnswer:
        """Ask one extension, and wait for the answer it owes."""
        allowed = self._timeout if timeout is None else timeout
        if not self._connections.connected(extension_id):
            # Nothing to wait for: no process will connect.
            entry = self._registry.get(extension_id)
            if entry is None:
                raise HookFailure("it is not installed here", unanswered=True)
            if entry.state in self._NOT_RUNNING:
                detail = f": {entry.reason}" if entry.reason else ""
                raise HookFailure(f"it is not running{detail}", unanswered=True)
        hook_id = self._connections.next_hook_id()
        frame = HookMessage(
            id=hook_id,
            event=event if isinstance(event, str) else event.value,
            value=HookFrameValue(
                body=value,
                tool_manifest_name=self._tools.of(value),
            ),
            principal=self._principal(),
        )
        return await self._connections.await_answer(
            extension_id,
            hook_id,
            frame.model_dump_json(),
            timeout=allowed,
            connect_timeout=connect_timeout,
        )


__all__ = ["ExtensionHooks", "HookFailure"]
