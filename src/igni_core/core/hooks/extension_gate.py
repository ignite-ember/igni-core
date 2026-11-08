"""Extensions' hooks: igni's hook events, answered by extensions too.

igni's hook events (:class:`~igni_core.core.hooks.events.HookEvent`) are
where anyone outside the agent gets a say — a command or HTTP hook from
the settings files, and an extension's process (``@ext.hook`` in the
``igni-extension`` SDK). This module is the extensions' half, as a seam:
the core does not import extensions. It asks the process's one
:class:`ExtensionHookGate`, :data:`extension_hook_gate`, and the backend
installs what answers (the host's
:class:`~igni_core.extensions.host.hooks.ExtensionHooks`). Nothing
installed, nobody objects.

Five events **decide** something
(:data:`~igni_core.core.hooks.extension_spec.DECIDING`), and an
extension's answer counts on them. Extensions are asked first, one at a
time in the super admin's priority order, each handed what the one
before left (:mod:`~igni_core.core.hooks.extension_values`); then igni's
own configured hooks run on the result, as plugin hooks run before
project hooks so the project keeps the last word. Every other event is
**told**: the extensions that hook it hear it, and nothing waits on them.
"""

from __future__ import annotations

from typing import Generic, Protocol

from pydantic import BaseModel, ConfigDict

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_spec import DECIDING
from igni_core.core.hooks.extension_values import HookValueT
from igni_core.core.hooks.schemas import HookPayload


class ExtensionHookVerdict(BaseModel, Generic[HookValueT]):
    """Whether a deciding event goes ahead, and with what."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    #: The value it goes ahead with, as the last extension left it.
    value: HookValueT
    #: Why not, when it does not.
    reason: str = ""
    #: The extension that stopped it.
    by: str = ""


class ExtensionHookAnswerer(Protocol):
    """Whoever answers for the extensions: the host, in igni's backend."""

    def hooks(self, event: HookEvent) -> bool:
        """Whether any extension hooks ``event`` — a fast no for the common case."""
        ...

    async def ask(self, value: HookValueT) -> ExtensionHookVerdict[HookValueT]:
        """Ask every extension that hooks the deciding event ``value`` is for, in priority order."""
        ...

    def tell(self, event: HookEvent, payload: HookPayload) -> None:
        """Tell every extension that hooks an event that decides nothing. Waits on nobody."""
        ...

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        """The agent called a tool; whether its server answered, for the extension it is from."""
        ...


class ExtensionHookGate:
    """Where the core asks and tells the extensions. The backend installs what answers it."""

    def __init__(self) -> None:
        self._answerer: ExtensionHookAnswerer | None = None

    def install(self, answerer: ExtensionHookAnswerer | None) -> None:
        self._answerer = answerer

    def hooked(self, event: HookEvent) -> bool:
        """Whether any extension hooks ``event``."""
        return self._answerer is not None and self._answerer.hooks(event)

    async def ask(self, value: HookValueT) -> ExtensionHookVerdict[HookValueT]:
        """Ask before a deciding event happens; it goes ahead as it is when nobody hooks it."""
        if self._answerer is None or not self._answerer.hooks(value.hook_event):
            return ExtensionHookVerdict(allowed=True, value=value)
        return await self._answerer.ask(value)

    def tell(self, event: str, payload: HookPayload) -> None:
        """Tell the extensions that hook an event that decides nothing."""
        if self._answerer is None:
            return
        try:
            hook_event = HookEvent(event)
        except ValueError:
            return
        if hook_event in DECIDING or not self._answerer.hooks(hook_event):
            return
        self._answerer.tell(hook_event, payload)

    def tool_called(self, tool_name: str, *, answered: bool, detail: str = "") -> None:
        """Say whether a tool the agent called answered.

        An extension whose tools time out or crash again and again is
        faulty, and the host counts it here; a tool that is not an
        extension's is nobody's business.
        """
        if self._answerer is not None:
            self._answerer.tool_called(tool_name, answered=answered, detail=detail)


#: The process's one gate: the backend installs the host here, the core asks it.
extension_hook_gate = ExtensionHookGate()


__all__ = [
    "ExtensionHookAnswerer",
    "ExtensionHookGate",
    "ExtensionHookVerdict",
    "extension_hook_gate",
]
