"""A run started, finished or failed — and the files it edited on the way."""

from __future__ import annotations

from typing import ClassVar

from igni_core.extensions.host.broadcasts.approvals import ApprovalMirror
from igni_core.extensions.host.broadcasts.project_paths import ProjectRoot
from igni_core.extensions.host.broadcasts.push_payloads import FileEditedPush
from igni_core.extensions.host.broadcasts.runs import RunLedger
from igni_core.extensions.host.broadcasts.signal import Handler, RunRef
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.protocol import messages as msg


class RunStartedSignal(RunRef):
    WIRE: ClassVar[str] = "chat.run_started"

    agent_name: str
    model: str


class RunCompletedSignal(RunRef):
    WIRE: ClassVar[str] = "chat.run_completed"

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    duration: float
    #: Every file the run edited; :meth:`seen_by` narrows it per listener.
    changed_files: list[str]

    @classmethod
    def finished(cls, m: msg.RunCompleted, files: frozenset[str]) -> RunCompletedSignal:
        return cls(
            session_id=m.session_id,
            run_id=m.run_id,
            parent_run_id=m.parent_run_id,
            input_tokens=m.input_tokens,
            output_tokens=m.output_tokens,
            reasoning_tokens=m.reasoning_tokens,
            duration=float(m.duration),
            changed_files=sorted(files),
        )

    def seen_by(self, grants: HostGrants) -> dict[str, object]:
        """The files this listener's workspace grant lets it see."""
        visible = [f for f in self.changed_files if grants.workspace.permits(f)]
        return self.model_copy(update={"changed_files": visible}).model_dump()


class RunErrorSignal(Signal):
    WIRE: ClassVar[str] = "chat.run_error"

    session_id: str
    error: str


class FileEditedSignal(Signal):
    WIRE: ClassVar[str] = "chat.file_edited"

    session_id: str
    path: str

    def seen_by(self, grants: HostGrants) -> dict[str, object] | None:
        return self.model_dump() if grants.workspace.permits(self.path) else None


class RunSignals:
    """Translates a run's life, keeping the ledger and the approval mirror in step."""

    def __init__(
        self, emitter: Emitter, ledger: RunLedger, mirror: ApprovalMirror, root: ProjectRoot
    ) -> None:
        self._emitter = emitter
        self._ledger = ledger
        self._mirror = mirror
        self._root = root

    def handlers(self) -> dict[type[msg.Message], Handler]:
        return {
            msg.RunStarted: self.run_started,
            msg.RunCompleted: self.run_completed,
            msg.RunError: self.run_error,
        }

    def run_started(self, m: msg.RunStarted) -> None:
        self._ledger.started(m)
        self._emitter.send(RunStartedSignal.run_of(m))

    def run_completed(self, m: msg.RunCompleted) -> None:
        files: frozenset[str] = frozenset()
        if not m.parent_run_id:
            files = self._ledger.finished(m.session_id)
            self._mirror.clear(m.session_id)
        signal = RunCompletedSignal.finished(m, files)
        self._emitter.send(signal, content=signal.seen_by)

    def run_error(self, m: msg.RunError) -> None:
        self._mirror.clear(m.session_id)
        self._ledger.dropped(m.session_id)
        self._emitter.send(RunErrorSignal.of(m))

    def file_edited(self, m: msg.PushNotification) -> None:
        """The ``file_edited`` push: told, and kept for the run's ``changed_files``."""
        push = FileEditedPush.model_validate(m.payload)
        found = self._root.relative(m.session_id, push.path)
        if not found.ok:
            return
        self._ledger.edited(m.session_id, found.relative)
        signal = FileEditedSignal(session_id=m.session_id, path=found.relative)
        self._emitter.send(signal, content=signal.seen_by)


__all__ = [
    "FileEditedSignal",
    "RunCompletedSignal",
    "RunErrorSignal",
    "RunSignals",
    "RunStartedSignal",
]
