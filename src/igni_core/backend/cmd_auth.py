"""Authentication slash commands: ``/login``, ``/logout``, ``/whoami``.

Extracted from :mod:`igni_core.backend.command_handler` — three
commands for the igni server auth surface.

* ``/login`` — dispatches the login action; the transport
  layer opens the browser + handles the OAuth callback.
* ``/logout`` — clears credentials on disk, resets in-memory
  cloud state, and rebuilds the main agent. If the currently-
  selected model was cloud-backed (``api_key: "cloud_token"``),
  falls back to the first model that has its own credentials
  so the session doesn't brick.
* ``/whoami`` — read the credentials file, report identity +
  token-expiry status.

Behaviour lives on :class:`AuthCommand`. Three module-level
``cmd_login`` / ``cmd_logout`` / ``cmd_whoami`` shims construct
an :class:`AuthCommand` from ``handler.session`` and delegate to
the matching method — kept as free functions to match
:class:`BuiltinCommandRegistry`'s dispatch-of-callables contract.
Tests wanting to inject a fake :class:`CredentialsStore`
construct :class:`AuthCommand` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.cloud_sign_out import switch_off_cloud
from igni_core.backend.command_result import CommandResult
from igni_core.backend.schemas_auth import LogoutOutcome
from igni_core.core.auth.credentials import CredentialsStore
from igni_core.protocol.messages import CommandAction

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.core.auth.credentials import LoadCredentialsResult
    from igni_core.core.session import Session


class AuthCommand:
    """Coordinator for the ``/login`` / ``/logout`` / ``/whoami``
    slash-command family. Holds a :class:`Session` reference so
    we never reach into ``handler._session`` from the coordinator.

    The credentials-file access is threaded through a
    :class:`CredentialsStore` collaborator (defaults to
    ``CredentialsStore()`` — the default path) so tests can inject
    a store pointed at a tmp path.
    """

    def __init__(
        self,
        session: Session,
        store: CredentialsStore | None = None,
    ) -> None:
        self._session = session
        self._store: CredentialsStore = store or CredentialsStore()

    async def login(self) -> CommandResult:
        """Dispatch the login action — transport handles the OAuth flow."""
        return CommandResult.for_action(CommandAction.LOGIN)

    async def logout(self) -> CommandResult:
        """Clear credentials, reset in-memory cloud state, rebuild agent.

        Three cohesive steps composed into a :class:`LogoutOutcome`:
          1. drop credentials on disk + in-memory cloud state,
          2. swap the default model off cloud when applicable,
          3. lower the composed outcome into a :class:`CommandResult`.
        """
        load_result = self._store.load()
        self._store.clear()
        self._session.clear_cloud_credentials()

        switch = switch_off_cloud(self._session)
        outcome = LogoutOutcome(
            identity_message=self._identity_message(load_result),
            fallback_model=switch.fallback_model,
            warning=switch.warning,
        )
        return outcome.to_command_result()

    async def whoami(self) -> CommandResult:
        """Report the logged-in identity + token expiry."""
        result = self._store.load()
        if not result.ok or result.creds is None:
            return CommandResult.info("Not logged in. Use /login to authenticate.")
        creds = result.creds
        if creds.is_expired():
            return CommandResult.info(
                f"Session expired for {creds.email}. Use /login to re-authenticate."
            )
        expires = creds.expires_at[:19] if creds.expires_at else "unknown"
        return CommandResult.info(f"Logged in as {creds.email} (expires: {expires})")

    # ── private helpers (logout decomposition) ─────────────────────

    @staticmethod
    def _identity_message(load_result: LoadCredentialsResult) -> str:
        """Format the "who was logged out" line from a
        :class:`CredentialsLoadResult`. Handles the not-logged-in
        case as a distinct message so the outcome carries a real
        identity string in both branches."""
        creds = load_result.creds if load_result.ok else None
        if creds is None:
            return "Not logged in."
        return f"Logged out ({creds.email})."


async def cmd_login(handler: CommandHandler) -> CommandResult:
    """See :meth:`AuthCommand.login`."""
    return await AuthCommand(handler.session).login()


async def cmd_logout(handler: CommandHandler) -> CommandResult:
    """See :meth:`AuthCommand.logout`."""
    return await AuthCommand(handler.session).logout()


async def cmd_whoami(handler: CommandHandler) -> CommandResult:
    """See :meth:`AuthCommand.whoami`."""
    return await AuthCommand(handler.session).whoami()
