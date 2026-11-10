"""How a session is put together — one module per concern.

:class:`~igni_core.core.session.core.Session` is the aggregate that
holds the sub-systems; this package builds them. Each module owns one
concern and exposes one builder class that composes the session;
:class:`~igni_core.core.session.parts.composer.SessionComposer` is the
order they are built in.
"""

from igni_core.core.session.parts.composer import SessionComposer
from igni_core.core.session.parts.foundation import SessionDeps

__all__ = ["SessionComposer", "SessionDeps"]
