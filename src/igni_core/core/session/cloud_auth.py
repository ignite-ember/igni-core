"""Cloud auth coordinator for :class:`Session`.

Owns the session's cloud credentials and server URL, the
public accessors (``cloud_connected`` / ``cloud_org_id`` /
``cloud_org_name`` / ``cloud_access_token`` / ``cloud_server_url``)
plus the credential-swap invariant (``replace_cloud_credentials``
/ ``clear_cloud_credentials``) plus the model catalog refresh
(``refresh_cloud_models``) all migrate to one class here.

Owns the "swap credentials → rebuild main team" invariant so
callers can express "log out this session" or "swap in a fresh
JWT" without needing to know the order matters.

Rule 6 (oop_offender #4): a coordinator class replaces the six
sprawled fields / methods on the Session god-class.
"""

from __future__ import annotations

from collections.abc import Callable

from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.session.cloud_catalog import CloudModelCatalog


class SessionCloudAuth:
    """Owns igni server credentials + the model-catalog refresh.

    Constructor takes narrow deps:

    * ``creds`` — the initial :class:`CloudCredentials` loaded from
      ``settings.auth.credentials_file`` in ``Session.__init__``.
    * ``server_url`` — the ``settings.api_url`` string used by
      cloud-routed tools.
    * ``rebuild_team`` — a callable that rebuilds ``session.main_team``.
      Invoked after every credential change so main-agent prompt
      / tool bindings pick up the new token.
    * ``catalog`` — the :class:`CloudModelCatalog` composed on
      :class:`Session` (shared with the model picker so both surfaces
      hit the same cache).
    """

    def __init__(
        self,
        creds: CloudCredentials,
        server_url: str,
        rebuild_team: Callable[[], None],
        catalog: CloudModelCatalog,
    ) -> None:
        self._creds = creds
        self._server_url = server_url
        self._rebuild_team = rebuild_team
        self._catalog = catalog

    # ── Read accessors (Session forwards to these) ──────────────

    @property
    def credentials(self) -> CloudCredentials:
        """Return the live :class:`CloudCredentials` object."""
        return self._creds

    @property
    def connected(self) -> bool:
        """Whether the session is authenticated with an igni server."""
        return self._creds.is_authenticated

    @property
    def org_id(self) -> str | None:
        """The organization ID from the igni server JWT."""
        return self._creds.org_id

    @property
    def org_name(self) -> str | None:
        """The organization display name from the igni server JWT."""
        return self._creds.org_name

    @property
    def access_token(self) -> str | None:
        """The igni server access token (``None`` when logged out)."""
        return self._creds.access_token

    @property
    def email(self) -> str | None:
        """The signed-in account's address, straight off the
        credentials file.

        No network. The address is on disk from the moment somebody
        logs in, which is what lets the header name the account
        immediately rather than after a round trip — a header that
        fills in a second late reads as a glitch.
        """
        return self._creds.email

    @property
    def server_url(self) -> str:
        """The igni server API root URL used by cloud-routed tools."""
        return self._server_url

    # ── Mutations ───────────────────────────────────────────────

    def use_server(self, server_url: str) -> None:
        """The igni server chosen since: cloud-routed tools built from now on use it."""
        self._server_url = server_url

    def replace(self, creds: CloudCredentials) -> None:
        """Swap in a fresh :class:`CloudCredentials` and rebuild
        the main team so cloud-routed prompts / tool bindings pick
        up the new token.

        Owns the two-line invariant (assign creds first, then
        rebuild) in one place.
        """
        self._creds = creds
        self._rebuild_team()

    def clear(self) -> None:
        """Drop the current credentials and rebuild the main team
        in the logged-out state."""
        self.replace(CloudCredentials.empty())

    def refresh_models(self) -> int:
        """Best-effort: fetch the cloud key pool's catalogue and
        merge into ``settings.models.registry``. Returns the count
        of newly-added entries.
        """
        return self._catalog.refresh()
