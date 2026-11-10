"""What igni observed outside the host model, told as signals.

The registry moving an extension, the workspace's HEAD and files, a
session switched — each is one typed signal, sent through the
:class:`~.signal_values.Emitter`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from igni_core.backend.workspace_watch import Head
from igni_core.extensions.busy import ExtensionBusy
from igni_core.extensions.events import HostTopic
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.signal_values import Emitter, Signal, Windowed
from igni_core.extensions.registry import ExtensionState, InstalledExtension


class ExtensionStateChanged(Signal):
    """An extension's state moved, for whoever may see every extension."""

    WIRE: ClassVar[str] = HostTopic.EXTENSION_STATE_CHANGED

    extension_id: str
    state: str
    reason: str

    @classmethod
    def of_entry(cls, entry: InstalledExtension, extension_id: str) -> ExtensionStateChanged:
        return cls(extension_id=extension_id, state=entry.state.value, reason=entry.reason)


class SelfStateChanged(ExtensionStateChanged):
    """The same change, told to the extension it is about."""

    WIRE: ClassVar[str] = "self.state_changed"


class HeadChanged(Signal):
    WIRE: ClassVar[str] = HostTopic.WORKSPACE_HEAD_CHANGED

    commit_sha: str
    branch: str


class FilesChanged(Signal):
    WIRE: ClassVar[str] = HostTopic.WORKSPACE_FILES_CHANGED

    paths: list[str]
    truncated: bool


class SessionSwitched(Windowed):
    WIRE: ClassVar[str] = HostTopic.SESSION_SWITCHED

    session_id: str
    window: str | None


class HostObservations:
    """Turns each observation into its signal."""

    def __init__(self, emitter: Emitter, busy: ExtensionBusy) -> None:
        self._emitter = emitter
        self._busy = busy

    def extension_changed(self, entry: InstalledExtension | None, extension_id: str) -> None:
        """The registry moved: an extension's state, for it and for whoever may see all."""
        if entry is None or entry.state is not ExtensionState.READY:
            # Stopped, failed or gone: it is working on nothing any more.
            self._busy.release_all(extension_id)
        if entry is None:
            return
        self._emitter.send(ExtensionStateChanged.of_entry(entry, extension_id))
        self._emitter.send(SelfStateChanged.of_entry(entry, extension_id), only=extension_id)

    def head_changed(self, head: Head) -> None:
        signal = HeadChanged.of(head)
        value = signal.model_dump()
        self._emitter.send(
            signal, content=lambda grants: value if grants.workspace.granted else None
        )

    def files_changed(self, paths: frozenset[str], cap: int) -> None:
        def visible(grants: HostGrants) -> dict[str, Any] | None:
            if not grants.workspace.granted:
                return None
            seen = sorted(p for p in paths if grants.workspace.permits(p))
            if not seen:
                return None
            return FilesChanged(paths=seen[:cap], truncated=len(seen) > cap).model_dump()

        self._emitter.value(FilesChanged.WIRE, None, content=visible)

    def session_switched(self, session_id: str, window: str | None) -> None:
        self._emitter.send(SessionSwitched(session_id=session_id, window=window))


__all__ = [
    "ExtensionStateChanged",
    "FilesChanged",
    "HeadChanged",
    "HostObservations",
    "SelfStateChanged",
    "SessionSwitched",
]
