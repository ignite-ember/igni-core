"""What igni does after a restore while it serves, each at its step.

``docs/CLOUD.md`` §9 and :mod:`igni_cloud.guest.resume`. The first
answer goes out before anything that can wait: the log writer is held
until that answer and its burst are through, the pools' connections are
only marked stale, and the pseudo-random state is reseeded the moment
the answer is out; the notifications, the timers and ``activity.json``
come after the window's first burst.
"""

from __future__ import annotations

from igni_cloud.guest.activity_file import ActivityFile
from igni_cloud.guest.entropy import ProcessEntropy
from igni_cloud.guest.ports import ResumingNotifications
from igni_cloud.guest.resume import Restored, RestoreStep, RestoreWatch
from igni_cloud.logs import StdoutLog
from igni_core.core.activity import ActivityRegistry
from igni_core.core.db.engine import engines


class RestoreSteps:
    """The serving igni's steps after a restore, registered on a :class:`RestoreWatch`."""

    def __init__(
        self, *, logs: StdoutLog, activities: ActivityRegistry, entropy: ProcessEntropy
    ) -> None:
        self._logs = logs
        self._activities = activities
        self._entropy = entropy
        self._activity: ActivityFile | None = None

    def register(
        self,
        restores: RestoreWatch,
        *,
        activity: ActivityFile,
        notifications: ResumingNotifications | None,
    ) -> None:
        self._activity = activity
        # The log writer sleeps until the first answer and its burst are
        # through: first of the steps after the restore, and first of the later ones.
        restores.on_restore(self._hold_logs)
        restores.on_restore(self._release_logs, when=RestoreStep.LATER)
        # Before any query: the pools' connections marked stale (touches nothing).
        restores.on_restore(self._reset_pools)
        # The moment the first answer is out.
        restores.on_restore(self._entropy.after_restore, when=RestoreStep.AT_FIRST_ANSWER)
        # After the window's first burst: what can wait a moment.
        if notifications is not None:
            restores.on_restore(notifications.resumed, when=RestoreStep.LATER)
        restores.on_restore(self._rearm_timers, when=RestoreStep.LATER)
        restores.on_restore(self._rewrite_activity, when=RestoreStep.LATER)

    async def _hold_logs(self, _restored: Restored) -> None:
        self._logs.hold()

    async def _release_logs(self, _restored: Restored) -> None:
        self._logs.release()

    async def _reset_pools(self, _restored: Restored) -> None:
        await engines.reset_pools()

    async def _rearm_timers(self, _restored: Restored) -> None:
        self._activities.rearm()

    async def _rewrite_activity(self, _restored: Restored) -> None:
        if self._activity is not None:
            self._activity.refresh(force=True)


__all__ = ["RestoreSteps"]
