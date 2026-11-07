"""The one synchronous fetch a settings load may make: ``required``, and no usable pack."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from igni_core.core.activity import http as busy_http
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.pack import GroupPolicyPack, SignedPolicy
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import AcceptedPack
from igni_core.core.config.group_policy_signature import jwks_url_for

logger = logging.getLogger(__name__)


class PackResponseBody(BaseModel):
    """The part of ``GET /v1/portal/me/group/pack`` that is applied: the signed form."""

    model_config = ConfigDict(extra="ignore")

    signed_pack: str | None = None


class LiveFetchResult(BaseModel):
    """Whether a verified pack (or signed absence) was stored, and why not."""

    model_config = ConfigDict(frozen=True)

    stored: bool
    reason: str = ""


class SignedPackTransport:
    """Fetches, verifies and stores a pack synchronously.

    A required policy is worth a start-up round trip, an optional one is not.
    Only the signed pack is stored; the caller rebuilds the file-shaped kinds
    from it.
    """

    def __init__(self, trust: PackTrust, timeout: float = 10.0) -> None:
        self._trust = trust
        self._timeout = timeout

    def fetch_into(self, cache: GroupPolicyCache) -> LiveFetchResult:
        token, api_url = self._trust.session.token, self._trust.api_url
        if not (token and api_url):
            return LiveFetchResult(stored=False, reason="nobody is signed in")
        if not self._trust.pins and not self._trust.may_fetch_unpinned_keys():
            logger.warning(
                "group-policy: %s is not HTTPS and no key is pinned; not fetching a pack", api_url
            )
            return LiveFetchResult(stored=False, reason=f"{api_url} is not HTTPS")
        try:
            with busy_http.BusyClient(timeout=self._timeout, busy_holder="igni-server") as client:
                response = client.get(
                    f"{api_url.rstrip('/')}/v1/portal/me/group/pack",
                    headers=self._authorization(token),
                )
                if response.status_code != 200:
                    logger.warning("group-policy: igni server answered %s", response.status_code)
                    return LiveFetchResult(
                        stored=False, reason=f"igni server answered {response.status_code}"
                    )
                body = self._body(response.json())
                jwks = Jwks.parse(client.get(jwks_url_for(api_url)).json())
        except (httpx.HTTPError, OSError, ValueError) as exc:  # offline is what this is for
            logger.warning("group-policy: could not reach igni server (%s)", exc)
            return LiveFetchResult(stored=False, reason=f"could not reach igni server ({exc})")
        compact = body.signed_pack or ""
        verdict = self._trust.verify(compact, jwks)
        if verdict.verified is None or jwks is None:
            logger.warning(
                "group-policy: the pack igni server sent was refused (%s)", verdict.reason
            )
            return LiveFetchResult(stored=False, reason=verdict.reason)
        verified = verdict.verified
        try:
            pack = (
                None
                if verified.policy is None
                else GroupPolicyPack.from_signed(
                    SignedPolicy.model_validate(verified.policy), signed_pack=compact
                )
            )
        except ValidationError as exc:
            logger.warning("group-policy: the signed pack does not parse (%s)", exc)
            return LiveFetchResult(stored=False, reason=f"the signed pack does not parse: {exc}")
        cache.signed.store(
            AcceptedPack(pack=pack, compact=compact, jwks=jwks, version=verified.version)
        )
        if verified.absent:
            cache.clear_derived()
        return LiveFetchResult(stored=True)

    @staticmethod
    def _authorization(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _body(raw: object) -> PackResponseBody:
        if not isinstance(raw, dict):
            return PackResponseBody()
        try:
            return PackResponseBody.model_validate(raw)
        except ValidationError:
            return PackResponseBody()
