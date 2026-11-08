"""igni's half of the guest contract, as the backend runs it.

``docs/CLOUD.md`` §9 and ``igni__cloudvm``'s ``docs/guest-protocol.md``.
The cloud layer's :class:`~igni_core.core.layer.BackendHooks`; the desktop
never builds one. In boot order:

1. :meth:`CloudGuest.starting` — a ``ready`` file left by an earlier igni
   in this VM is removed before anything else;
2. :meth:`CloudGuest.listening` — once the transport listens, what a
   bound backend's first run would import is imported
   (:class:`~igni_cloud.guest.warm.WarmImports`), a warm base answers
   once on its own listener
   (:class:`~igni_cloud.guest.practice.FirstAnswerPractice`), what is
   there is frozen and made immortal, and ``ready`` is written: the node
   agent may now take a warm base snapshot;
3. :meth:`CloudGuest.bind` — when no producer is known yet, igni waits
   for ``producer.env``, refusing requests meanwhile
   (:class:`~igni_cloud.guest.binding.ProducerBinding`); the
   backend loads settings and opens the database only after it returns,
   and the process's pseudo-random state is reseeded
   (:class:`~igni_cloud.guest.entropy.ProcessEntropy`);
4. :meth:`CloudGuest.serving` — the activity file is kept true, a
   restore (``/run/igni/restored``) is reseeded, reconnected and re-timed
   after (the log writer held until its first answer and burst are
   through), what igni built is frozen for the collector, and a ``producer.env``
   that comes to name another producer shuts igni down;
   and the before-suspend hook's ``suspending`` is answered once igni
   has handed its free heap back (:mod:`igni_cloud.guest.suspend`);
5. :meth:`CloudGuest.stop` — the files igni keeps are removed.

The binding, the restore and the suspend watch share one inotify watch
per directory (:class:`~igni_cloud.guest.watch.DirectoryWatch`), each
woken only by its own file: a restore wakes the restore watch alone,
and igni's own ``activity.json`` wakes none.
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Callable

from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest.activity_file import ActivityFile
from igni_cloud.guest.binding import ProducerBinding
from igni_cloud.guest.credential import StateChange
from igni_cloud.guest.entropy import ProcessEntropy
from igni_cloud.guest.files import ReadyFile
from igni_cloud.guest.immortal import BaseObjects
from igni_cloud.guest.paths import GuestPaths
from igni_cloud.guest.ports import (
    GuestTransport,
    HostModelPort,
    PoolRpcMount,
    ResumingNotifications,
)
from igni_cloud.guest.practice import FirstAnswerPractice
from igni_cloud.guest.restore_steps import RestoreSteps
from igni_cloud.guest.resume import NextAnswer, RestoreWatch
from igni_cloud.guest.sign_in import GuestCredential
from igni_cloud.guest.suspend import SuspendWatch
from igni_cloud.guest.warm import WarmImports
from igni_cloud.guest.watch import DirectoryWatches
from igni_cloud.logs import StdoutLog
from igni_core.backend.activity import Activity
from igni_core.backend.pool_rpcs import VmSignInRpc
from igni_core.core.activity import ActivityRegistry
from igni_core.core.db.engine import cache
from igni_core.core.db.scope import Tenant
from igni_core.core.layer import BackendHooks
from igni_core.protocol import messages as msg

logger = logging.getLogger(__name__)


class CloudGuest(BackendHooks):
    """The ready file, the late binding, the activity file and the restore watch."""

    def __init__(
        self,
        paths: GuestPaths,
        *,
        binding: ProducerBinding | None = None,
        restores: RestoreWatch | None = None,
        suspends: SuspendWatch | None = None,
        warm: WarmImports | None = None,
        gc_freeze: bool = True,
        practise: bool = True,
        tokens: EnvironmentCredentials | None = None,
        logs: StdoutLog | None = None,
        activities: ActivityRegistry | None = None,
    ) -> None:
        self.paths = paths
        self.ready = ReadyFile(paths.ready)
        directories = DirectoryWatches()
        self.binding = (
            binding
            if binding is not None
            else ProducerBinding(
                paths.producer_env, directory=directories.of(paths.producer_env.parent)
            )
        )
        self.restores = (
            restores
            if restores is not None
            else RestoreWatch(paths.restored, directory=directories.of(paths.restored.parent))
        )
        self.suspends = (
            suspends
            if suspends is not None
            else SuspendWatch(
                paths.suspending,
                paths.suspend_ready,
                directory=directories.of(paths.suspending.parent),
            )
        )
        self.warm = warm if warm is not None else WarmImports()
        self.entropy = ProcessEntropy()
        #: igni's next answer after a wake (a pause tells igni nothing).
        self.answers = NextAnswer()
        self._logs = logs if logs is not None else StdoutLog()
        #: The VM's own credential, kept while a bound producer is served.
        self.credential = GuestCredential(
            tokens if tokens is not None else EnvironmentCredentials(),
            after_wake=self.answers.after,
        )
        self._after_restore = RestoreSteps(
            logs=self._logs,
            activities=activities if activities is not None else ActivityRegistry.shared(),
            entropy=self.entropy,
        )
        self._gc_freeze = gc_freeze
        self._practise = practise
        self.activity: ActivityFile | None = None

    @classmethod
    def from_env(
        cls, *, tokens: EnvironmentCredentials | None = None, logs: StdoutLog | None = None
    ) -> CloudGuest:
        return cls(GuestPaths.from_env(), tokens=tokens, logs=logs)

    @property
    def tokens(self) -> EnvironmentCredentials:
        """The credentials igni server's clients read."""
        return self.credential.tokens

    @property
    def tenant(self) -> Tenant | None:
        """Whose VM this is, once bound."""
        return self.binding.tenant

    def starting(self) -> None:
        try:
            self.ready.clear()
        except OSError as exc:
            logger.warning("cannot clear %s: %s", self.ready.path, exc)

    async def listening(self, transport: GuestTransport | None) -> None:
        """Warm the imports, practise the first answer, freeze it all, then say ``ready``."""
        self.warm.run()
        if self._practise and self.binding.needed():
            # A warm base: its first answer is every producer's
            # (:mod:`igni_cloud.guest.practice`).
            result = await FirstAnswerPractice(transport).run()
            log = logger.info if result.ok else logger.warning
            log(
                "first answer practised" if result.ok else "first answer not practised",
                extra={"fields": {"practice": result.model_dump(mode="json")}},
            )
        if self._gc_freeze:
            # What the warm base holds is every producer's. Immortal, using
            # it writes no reference count; frozen, a collection while
            # binding does not walk it. Either write would make the page
            # this producer's own (:mod:`igni_cloud.guest.immortal`).
            gc.collect()
            BaseObjects.immortalise()
            gc.freeze()
        try:
            self.ready.mark()
        except OSError as exc:
            logger.warning("cannot write %s: %s", self.ready.path, exc)

    async def bind(self, transport: GuestTransport | None) -> bool:
        """Wait for a producer, unless the environment already names one. ``True`` if bound here."""
        if not self.binding.needed():
            return False
        logger.info("no producer yet; waiting for %s", self.binding.path)
        await self.binding.bind(transport)
        # The first moment this VM is one producer's: it must not share
        # the warm base's pseudo-random state with every other.
        self.entropy.reseed()
        return True

    def serving(
        self,
        *,
        model: HostModelPort,
        transport: GuestTransport,
        orchestrator: PoolRpcMount | None = None,
        notifications: ResumingNotifications | None = None,
        on_rebind: Callable[[], None] | None = None,
        api_url: Callable[[], str | None] | None = None,
    ) -> None:
        """Start what runs while igni serves the producer."""
        self.credential.open(
            self.binding.bound, api_url, transport=transport, moved=self._rewrite_now
        )
        if orchestrator is not None:
            orchestrator.mount(VmSignInRpc(self.vm_sign_in))
        backend = model.services.backend
        self.activity = activity = ActivityFile(
            self.paths.activity,
            activity=lambda: Activity.model_validate(backend.activity()),
            clients=lambda: transport.client_count,
            credential=self.credential.state,
        )
        model.services.activity_watch.listen(activity.changed)
        # A window coming or going changes ``clients``.
        transport.watch_connections(self._connection_moved)
        transport.observe(self._sent)
        activity.start()
        self._after_restore.register(self.restores, activity=activity, notifications=notifications)
        self.restores.start()
        # A paused VM holds no database connection: a node pauses about a
        # thousand, and each producer's own login cannot share a pooler.
        self.suspends.before_suspend(self._close_connections)
        self.suspends.start()
        if self.binding.bound is not None and on_rebind is not None:
            self.binding.watch(lambda _env: on_rebind())
        self.credential.serve(self.restores)
        # Everything igni built to serve this producer stays: frozen, the
        # collector no longer walks (and so writes to) it while igni idles.
        if self._gc_freeze:
            gc.collect()
            gc.freeze()

    async def vm_sign_in(self, session_token: str, organisation: str | None = None) -> StateChange:
        """The gateway handed the person's session (``vm_sign_in``): get the VM its credential."""
        return await self.credential.sign_in(session_token, organisation)

    async def stop(self) -> None:
        await self.credential.stop()
        await self.restores.stop()
        self._logs.release()
        await self.suspends.stop()
        await self.binding.stop()
        if self.activity is not None:
            await self.activity.stop()
        try:
            self.ready.clear()
        except OSError as exc:
            logger.debug("cannot clear %s: %s", self.ready.path, exc)

    def _connection_moved(self, event: str, _client: str) -> None:
        timeline = self.restores.timeline
        if timeline is not None and not timeline.done:
            timeline.mark(f"window {event}")
        if self.restores.waiting_for_answer:
            # Rewritten after the first answer, with the rest of the
            # restore's steps: not on the path of that answer.
            return
        self._rewrite_now()

    def _sent(self, message: msg.Message) -> None:
        """An answer: the first after a restore or a wake lets the waiting steps run."""
        if isinstance(message, msg.RPCResponse):
            self.restores.answered()
            self.answers.answered()

    def _rewrite_now(self) -> None:
        if self.activity is not None:
            self.activity.refresh()

    async def _close_connections(self) -> None:
        await cache.close_connections()


__all__ = ["CloudGuest"]
