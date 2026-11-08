"""The VM's own credential, as the guest contract keeps it while igni serves.

``docs/CLOUD.md`` §9. Only a VM bound through ``producer.env`` keeps one
(:class:`~igni_cloud.guest.credential.VmCredentials`); its token is the
one igni server's clients read (:class:`~igni_cloud.credentials.EnvironmentCredentials`),
each change of its state is pushed to the windows and rewritten into
``activity.json``, and it is renewed after a restore.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest.binding import ProducerEnv
from igni_cloud.guest.credential import (
    CredentialFile,
    CredentialState,
    StateChange,
    VmCredentialExchange,
    VmCredentials,
)
from igni_cloud.guest.paths import GuestPaths
from igni_cloud.guest.ports import GuestTransport
from igni_cloud.guest.resume import Restored, RestoreStep, RestoreWatch
from igni_core.core.activity import background
from igni_core.protocol import messages as msg

logger = logging.getLogger(__name__)


class GuestCredential:
    """The VM credential's keeper while igni serves, and who hears of it."""

    def __init__(
        self, tokens: EnvironmentCredentials, *, after_wake: Callable[..., Awaitable[None]]
    ) -> None:
        #: The credentials igni server's clients read: told who keeps the token while serving.
        self.tokens = tokens
        self._after_wake = after_wake
        #: The keeper, once a VM bound through ``producer.env`` serves.
        self.keeper: VmCredentials | None = None
        self._transport: GuestTransport | None = None
        self._moved: Callable[[], None] = lambda: None

    def open(
        self,
        bound: ProducerEnv | None,
        api_url: Callable[[], str | None] | None,
        *,
        transport: GuestTransport,
        moved: Callable[[], None],
    ) -> None:
        """Make the keeper, for a VM bound to a producer through ``producer.env``.

        ``moved`` is called after each change of its state has been pushed.
        """
        self._transport = transport
        self._moved = moved
        if bound is None or api_url is None:
            return
        # Read now: ``producer.env`` named the files after igni started.
        paths = GuestPaths.from_env()
        self.keeper = VmCredentials(
            credential_file=CredentialFile(paths.vm_credential),
            token_file=paths.token,
            exchange=VmCredentialExchange(api_url),
            producer=bound.producer,
            on_state=self._state_moved,
            after_wake=self._after_wake,
        )

    def serve(self, restores: RestoreWatch) -> None:
        """Start keeping the token, renewed after every restore."""
        keeper = self.keeper
        if keeper is None:
            return
        # Its token is the one igni server's clients read: never handed out expired.
        self.tokens.kept_by(keeper)
        restores.on_restore(self._renew, when=RestoreStep.LATER)
        background.start(keeper.start(), holder="vm-credential", reason="a token from it")

    def state(self) -> str | None:
        """What ``activity.json`` says of the credential."""
        return self.keeper.state.value if self.keeper is not None else None

    async def sign_in(self, session_token: str, organisation: str | None = None) -> StateChange:
        """The gateway handed the person's session (``vm_sign_in``): get the VM its credential."""
        if self.keeper is None:
            return StateChange(state=CredentialState.NEEDS_SIGN_IN, message="not a producer's VM")
        return await self.keeper.sign_in(session_token, organisation)

    async def stop(self) -> None:
        if self.keeper is not None:
            self.tokens.kept_by(None)
            await self.keeper.stop()

    def _state_moved(self, change: StateChange) -> None:
        """Tell the windows, and the node agent through ``activity.json``."""
        if change.state is not CredentialState.READY:
            logger.warning("VM credential: %s (%s)", change.state.value, change.message)
        if self._transport is not None:
            background.daemon(
                self._transport.send(
                    msg.PushNotification(channel="vm_credential", payload=change.model_dump())
                ),
                name="push-vm-credential",
            )
        self._moved()

    async def _renew(self, _restored: Restored) -> None:
        if self.keeper is not None:
            await self.keeper.refresh(force=False)


__all__ = ["GuestCredential"]
