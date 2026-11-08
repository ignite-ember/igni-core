"""The cloud's credentials: what the VM keeps, never ``credentials.json``.

``docs/CLOUD.md`` §9. The token igni uses is the one it keeps from the
VM's own credential (:mod:`igni_cloud.guest.credential`), in the file
``IGNI_ACCESS_TOKEN_FILE`` names; read on every credentials load, so a
renewed token reaches every part of igni without a restart.
``IGNI_ACCESS_TOKEN`` (fixed for the process) is for a VM started without
the guest contract. Saving and clearing write nothing.

The file's token is never handed out once it has expired: a VM paused
past its token's life wakes with an expired one on the tmpfs, and its
renewal waits for the first answer
(:class:`~igni_cloud.guest.credential.VmCredentials`). Until it is
renewed a load finds no token, and :meth:`EnvironmentCredentials.ready`
— which igni server's clients await before they send — renews it at once.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import ClassVar, Protocol

from igni_core.core.auth.credentials import Credentials, CredentialSource
from igni_core.core.auth.schemas import LoadCredentialsResult

logger = logging.getLogger(__name__)


class TokenKeeper(Protocol):
    """What keeps the token file's token (:class:`~igni_cloud.guest.credential.VmCredentials`)."""

    def expired(self) -> bool:
        """The token in the file may no longer be sent."""
        ...

    async def ready(self) -> None:
        """Until the token in the file may be sent: a renewal under way, or one now."""
        ...


class EnvironmentCredentials(CredentialSource):
    """The token in the environment, never a ``credentials.json``. The first set wins:

    * ``IGNI_ACCESS_TOKEN_FILE`` — a file holding the token, rewritten
      (temporary file, rename) whenever it is renewed — by the VM's
      credential keeper. Read on every credentials load, so a fresh token
      reaches a running VM — after a resume too — without a restart;
    * ``IGNI_ACCESS_TOKEN`` — the token itself, fixed for the process's
      life.

    ``IGNI_ACCESS_TOKEN_EXPIRES_AT`` and ``IGNI_EMAIL`` are optional.

    The cloud layer holds the process's one instance and hands it to the
    guest contract, which tells it who keeps the file's token while it serves.
    """

    TOKEN_FILE_ENV: ClassVar[str] = "IGNI_ACCESS_TOKEN_FILE"  # noqa: S105 — an environment variable's name, not a secret
    TOKEN_ENV: ClassVar[str] = "IGNI_ACCESS_TOKEN"  # noqa: S105 — an environment variable's name, not a secret
    EXPIRES_ENV: ClassVar[str] = "IGNI_ACCESS_TOKEN_EXPIRES_AT"
    EMAIL_ENV: ClassVar[str] = "IGNI_EMAIL"

    def __init__(self) -> None:
        #: The keeper of the file's token, while the guest contract serves.
        self._keeper: TokenKeeper | None = None

    def kept_by(self, keeper: TokenKeeper | None) -> None:
        """Whose token the file holds (``None`` when it stops keeping it)."""
        self._keeper = keeper

    def token(self) -> str:
        path = os.environ.get(self.TOKEN_FILE_ENV, "").strip()
        if path:
            keeper = self._keeper
            if keeper is not None and keeper.expired():
                # Never an expired token: ``ready`` renews it.
                return ""
            try:
                return Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("cannot read the token at %s: %s", path, exc)
                return ""
        return os.environ.get(self.TOKEN_ENV, "").strip()

    async def ready(self) -> None:
        keeper = self._keeper
        if keeper is not None:
            await keeper.ready()

    def load(self) -> LoadCredentialsResult:
        token = self.token()
        if not token:
            return LoadCredentialsResult(ok=False, reason="no_file")
        return LoadCredentialsResult(
            ok=True,
            creds=Credentials(
                access_token=token,
                email=os.environ.get(self.EMAIL_ENV, ""),
                expires_at=os.environ.get(self.EXPIRES_ENV, ""),
            ),
        )


__all__ = ["EnvironmentCredentials", "TokenKeeper"]
