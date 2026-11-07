"""Authentication module — OTP login, token storage, and credential management.

Public surface:

* :class:`~igni_core.core.auth.portal_client.PortalClient` +
  :class:`~igni_core.core.auth.callback_server.CallbackServer`
  drive the browser-callback CLI-auth flow.
* :class:`~igni_core.core.auth.schemas.UserInfo`,
  :class:`~igni_core.core.auth.schemas.LoginResult`,
  :class:`~igni_core.core.auth.schemas.ValidateResult`,
  :class:`~igni_core.core.auth.schemas.LoadCredentialsResult`, and
  :class:`~igni_core.core.auth.schemas.JwtClaims` are the Pydantic
  result shapes callers unpack.
* :class:`CloudCredentials` / :class:`Credentials` +
  :class:`CredentialsStore` cover on-disk storage — everything
  used to be a free function is now a method on the coordinator
  class (:meth:`CredentialsStore.save` / :meth:`.load` / :meth:`.clear`,
  :meth:`Credentials.new` / :meth:`Credentials.is_expired`,
  :meth:`JwtClaims.decode`).
"""

from igni_core.core.auth.callback_server import CallbackServer
from igni_core.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    CredentialsStore,
    EmptyCloudCredentials,
)
from igni_core.core.auth.portal_client import (
    PortalClient,
)
from igni_core.core.auth.schemas import (
    JwtClaims,
    LoadCredentialsResult,
    LoginResult,
    UserInfo,
    ValidateResult,
)

__all__ = [
    "CallbackServer",
    "CloudCredentials",
    "Credentials",
    "CredentialsStore",
    "EmptyCloudCredentials",
    "JwtClaims",
    "LoadCredentialsResult",
    "LoginResult",
    "PortalClient",
    "UserInfo",
    "ValidateResult",
]
