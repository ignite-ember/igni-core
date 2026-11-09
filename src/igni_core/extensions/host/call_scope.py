"""Which window and which session a request is about."""

from __future__ import annotations

import logging

from igni_core.extensions.host.backend import HostBackend
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.session_target import SessionTarget, SessionTargets
from igni_core.extensions.host.spec import (
    ErrorCode,
    MethodSpec,
    ObjectSpec,
    PropertySpec,
    SignalSpec,
)
from igni_core.extensions.host.windows import Windows, WindowStateError
from igni_core.extensions.host.wire import Request

logger = logging.getLogger(__name__)


class CallScope:
    """Resolves a request's window, then the session it acts in."""

    def __init__(self, windows: Windows, backend: HostBackend, targets: SessionTargets) -> None:
        self._windows = windows
        self._backend = backend
        self._targets = targets

    def window(self, spec: ObjectSpec, request: Request, home: str | None) -> str | None:
        """The window named, else the caller's own, else the active one."""
        if request.window is not None:
            if not spec.per_window:
                raise HostModelError(
                    ErrorCode.INVALID, f"'{spec.path}' does not belong to a window"
                )
            if not self._windows.known(request.window):
                raise HostModelError(ErrorCode.NOT_FOUND, f"no window {request.window!r} is open")
            return request.window
        if home is not None and self._windows.known(home):
            return home
        return self._windows.active()

    async def session(
        self,
        *,
        grants: HostGrants,
        spec: ObjectSpec,
        member: PropertySpec | SignalSpec | MethodSpec,
        request: Request,
        window: str | None,
    ) -> SessionTarget:
        """The session the request names, if it may reach it; else the one on screen."""
        return await self._targets.resolve(
            grants=grants,
            spec=spec,
            member=member,
            request=request,
            on_screen=self._on_screen(window),
        )

    def _on_screen(self, window: str | None) -> str:
        """The session the window is looking at.

        A window's state that cannot be read is the window's problem,
        not a reason to refuse every extension call: the extension's
        work goes on, in the session the run started with. The state
        that was asked for is still refused where it was asked for.
        """
        if window is not None:
            try:
                bound = self._windows.session(window)
            except WindowStateError:
                logger.exception("the state of window %r could not be read", window)
            else:
                if bound:
                    return bound
        return self._backend.default_session()


__all__ = ["CallScope"]
