"""An extension that keeps not answering is faulty, and costs nobody any more waiting.

Every call igni makes into an extension has a deadline — a hook event
(:class:`~igni_core.extensions.host.hooks.ExtensionHooks`, two seconds),
a tool call (the MCP session's read timeout), a page's call, a prompt
contribution, a health probe. A deadline bounds one wait; it does not
stop igni waiting on the same hung process every time. So the floor
counts: an extension whose process times out or crashes
:attr:`ExtensionFaults.LIMIT` times in a row — hooks and tool calls
alike — is marked **faulty** (``InstalledExtension.faulty``):

* its process is restarted;
* the person is told, and the extensions panel and the host model's
  ``ExtensionInfo`` say so;
* its hooks are not waited on: a fail-closed event (``PreToolUse``,
  ``UserPromptSubmit``) is refused at once with "<extension> isn't
  responding", a fail-open one goes ahead without it.

The first answer after that clears the mark. An answer is anything the
process said — a tool that reports an error has answered; a call that
timed out, or whose process went away, has not.

**The count belongs to the installed set.** One counter for the
extensions in a registry — reached through
:attr:`~igni_core.extensions.registry.ExtensionRegistry.faults` — so
that a hook that times out and a tool call that times out count
against the same extension, and so that two registries never share
one extension's failures.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    # The registry owns the counter and is what it counts; naming it
    # here at runtime would close a circle (registry → faults →
    # registry). The name is a type, and only the checker needs it.
    from igni_core.extensions.registry import ExtensionRegistry

logger = logging.getLogger(__name__)

#: Told when an extension becomes faulty: ``(extension_id, reason)``.
OnFaulty = Callable[[str, str], None]


class ExtensionFaults:
    """Counts each extension's failures in a row, and marks it faulty at the limit.

    The counter is per installed set: the registry it is given is the
    one whose extensions it counts, and whose ``faulty`` mark it sets
    and clears. Whoever counts failures — the hooks and the extension
    RPC — reaches the same one through that registry
    (:attr:`~igni_core.extensions.registry.ExtensionRegistry.faults`);
    a second counter over the same registry would count one
    extension's failures apart from the rest.
    """

    #: Timeouts or crashes in a row that make an extension faulty.
    LIMIT: ClassVar[int] = 3

    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry
        self._in_a_row: dict[str, int] = {}
        self._watchers: list[OnFaulty] = []

    def on_faulty(self, watcher: OnFaulty) -> None:
        """Be told when an extension becomes faulty — to restart it, and to tell the person."""
        self._watchers.append(watcher)

    # ── What igni saw ─────────────────────────────────────────────

    def failed(self, extension_id: str, what: str) -> None:
        """A call into it timed out, or its process crashed. ``what`` says which call."""
        count = self._in_a_row.get(extension_id, 0) + 1
        self._in_a_row[extension_id] = count
        if count < self.LIMIT or self.faulty(extension_id):
            return
        name = self._name(extension_id)
        reason = (
            f"{name} isn't responding: {count} calls in a row timed out or crashed "
            f"(the last, {what}). igni is restarting it."
        )
        logger.warning("%s", reason)
        self._registry.set_faulty(extension_id, reason)
        for watcher in list(self._watchers):
            try:
                watcher(extension_id, reason)
            except Exception:
                logger.exception("a faulty-extension watcher failed for %s", extension_id)

    def answered(self, extension_id: str) -> None:
        """It answered. The count starts again, and a faulty mark clears."""
        self._in_a_row.pop(extension_id, None)
        if self.faulty(extension_id):
            logger.info("%s is answering again", extension_id)
            self._registry.set_faulty(extension_id, "")

    # ── What callers ask ──────────────────────────────────────────

    def faulty(self, extension_id: str) -> bool:
        entry = self._registry.get(extension_id)
        return entry is not None and bool(entry.faulty)

    def not_responding(self, extension_id: str) -> str:
        """What a fail-closed event is refused with while it is faulty."""
        return f"{self._name(extension_id)} isn't responding."

    def _name(self, extension_id: str) -> str:
        entry = self._registry.get(extension_id)
        return entry.manifest.display_name if entry is not None else extension_id


__all__ = ["ExtensionFaults", "OnFaulty"]
