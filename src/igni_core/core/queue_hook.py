"""Deprecation shim — moved to :mod:`igni_core.core.hooks.queue`.

The queue-hook implementation now lives in the
:mod:`igni_core.core.hooks.queue` package alongside the other
Agno-boundary hook code (``core/hooks/executor.py``,
``core/hooks/tool_hook.py``, …). New code should import from there.

This module re-exports the public surface for one release so external
callers (out-of-tree plugins, older ``backend/team_wiring.py``
snapshots) keep working. A :class:`DeprecationWarning` is emitted on
import to nudge the migration.

Removal ticket: track alongside the next minor release.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable

from igni_core.core.hooks.queue import (
    USER_NOTE_HEADER,
    InjectedMessage,
    InjectedNote,
    InjectedRunBuffer,
    PostHookInvocation,
    QueueBridge,
    QueueCallbacks,
    QueueInjectorHook,
    QueuePersisterHook,
    SupportsRunOutput,
    ToolHookInvocation,
)

warnings.warn(
    "igni_core.core.queue_hook is deprecated; import from igni_core.core.hooks.queue instead.",
    DeprecationWarning,
    stacklevel=2,
)


def create_queue_hook(
    queue: list[str],
    on_inject: Callable[[str], None] | None = None,
    on_queue_changed: Callable[[], None] | None = None,
) -> tuple[QueueInjectorHook, QueuePersisterHook]:
    """Deprecated — construct :class:`QueueBridge` directly instead.

    Kept for one release so external callers don't break at import
    time. Internally builds a :class:`QueueBridge` and returns its
    two hooks.
    """
    bridge = QueueBridge(
        queue=queue,
        on_inject=on_inject,
        on_queue_changed=on_queue_changed,
    )
    return bridge.injector, bridge.persister


__all__ = [
    "USER_NOTE_HEADER",
    "InjectedMessage",
    "InjectedNote",
    "InjectedRunBuffer",
    "PostHookInvocation",
    "QueueBridge",
    "QueueCallbacks",
    "QueueInjectorHook",
    "QueuePersisterHook",
    "SupportsRunOutput",
    "ToolHookInvocation",
    "create_queue_hook",
]
