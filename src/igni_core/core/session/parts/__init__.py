"""How a session is put together — one module per concern.

:class:`~igni_core.core.session.core.Session` is the aggregate that
holds the sub-systems; this package builds them. Each module owns one
concern and exposes one builder class that composes the session;
:class:`~igni_core.core.session.parts.composer.SessionComposer` is the
order they are built in.

The order is not incidental. The hooks are loaded before the MCP
manager exists, the identity before the persister that keys its rows,
the pools and the learning manager before the main team that draws on
them. Where a sub-system needs one that is not built yet, it is given
a callable to read rather than a value, so the late binding is visible
in the code instead of implied by the order.
"""

from igni_core.core.session.parts.composer import SessionComposer, SessionComposerDeps

__all__ = ["SessionComposer", "SessionComposerDeps"]
