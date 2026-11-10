"""The composition root: each translator built with only the collaborators it needs.

:meth:`Translators.of` builds them from the host's services and merges
their message tables into one; :class:`~.router.BroadcastSignals` routes
through what it builds.
"""

from __future__ import annotations

from igni_core.extensions.host.broadcasts.approval_signals import ApprovalSignals
from igni_core.extensions.host.broadcasts.chat_signals import ChatSignals
from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads
from igni_core.extensions.host.broadcasts.file_edits import FileEditSignals
from igni_core.extensions.host.broadcasts.push_signals import PushSignals
from igni_core.extensions.host.broadcasts.rpc_writes import RpcWriteSignals
from igni_core.extensions.host.broadcasts.run_signals import RunSignals
from igni_core.extensions.host.broadcasts.session_values import DisplayRead, HostSessionValues
from igni_core.extensions.host.broadcasts.signal import Handler, merged
from igni_core.extensions.host.broadcasts.status import StatusSignals
from igni_core.extensions.host.broadcasts.task_signals import TaskSignals
from igni_core.extensions.host.project_paths import ProjectRoot
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.host.signal_values import Emitter
from igni_core.protocol import messages as msg


class Translators:
    """Every translator, and the one table of the messages they translate."""

    def __init__(
        self,
        handlers: dict[type[msg.Message], Handler],
        push: PushSignals,
        rpc: RpcWriteSignals,
        approvals: ApprovalSignals,
    ) -> None:
        #: Each outbound message class, and the translator that owns it.
        self.handlers = handlers
        self.push = push
        self.rpc = rpc
        self.approvals = approvals

    @classmethod
    def of(cls, host: HostServices, *, display: DisplayRead) -> Translators:
        """
        Args:
            display: Reads ``app.display`` for a session, as
                :meth:`~igni_core.extensions.host.display.DisplayReader.read`
                does, as a :class:`~igni_core.extensions.host.display.Display`.
        """
        emitter = Emitter(host.bus.emit)
        state = host.state
        approvals = ApprovalSignals(emitter, state.approvals)
        reads = DeferredReads(
            host.bus.wanted,
            host.work.spawn,
            emitter,
            HostSessionValues(host.gateway, host.backend, display),
        )
        tasks = TaskSignals(emitter)
        runs = RunSignals(emitter, state.runs, state.approvals)
        files = FileEditSignals(emitter, state.runs, ProjectRoot(host.backend))
        push = PushSignals(emitter, files, tasks, reads)
        rpc = RpcWriteSignals(
            emitter, reads, tasks, host.extensions, host.observations.session_switched
        )
        handlers: dict[type[msg.Message], Handler] = merged(
            ChatSignals(emitter, host.windows).handlers(),
            runs.handlers(),
            approvals.handlers(),
            StatusSignals(emitter, state.status).handlers(),
            tasks.handlers(),
            {msg.PushNotification: push.push},
        )
        return cls(handlers, push, rpc, approvals)


__all__ = ["Translators"]
