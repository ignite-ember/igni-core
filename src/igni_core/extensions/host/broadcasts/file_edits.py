"""A file an agent edited: told, and kept for its run's ``changed_files``."""

from __future__ import annotations

from typing import ClassVar

from igni_core.extensions.host.broadcasts.push_reads import read_push
from igni_core.extensions.host.broadcasts.signal import PushHandler
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.project_paths import ProjectRoot
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.extensions.host.state.runs import RunLedger
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push_payloads import FileEditedPayload


class FileEditedSignal(Signal):
    WIRE: ClassVar[str] = "chat.file_edited"

    session_id: str
    path: str

    def seen_by(self, grants: HostGrants) -> dict[str, object] | None:
        return self.model_dump() if grants.workspace.permits(self.path) else None


class FileEditSignals:
    """Translates the ``file_edited`` push, keeping the run ledger in step."""

    def __init__(self, emitter: Emitter, ledger: RunLedger, root: ProjectRoot) -> None:
        self._emitter = emitter
        self._ledger = ledger
        self._root = root

    def push_handlers(self) -> dict[PushChannel, PushHandler]:
        return {PushChannel.FILE_EDITED: self.file_edited}

    def file_edited(self, m: msg.PushNotification) -> None:
        """Outside the session's project, the edit is nothing an extension is told."""
        payload = read_push(FileEditedPayload, m.payload, channel=m.channel)
        if payload is None:
            return
        found = self._root.relative(m.session_id, payload.path)
        if not found.ok:
            return
        self._ledger.edited(m.session_id, found.relative)
        signal = FileEditedSignal(session_id=m.session_id, path=found.relative)
        self._emitter.send(signal, content=signal.seen_by)


__all__ = ["FileEditSignals", "FileEditedSignal"]
