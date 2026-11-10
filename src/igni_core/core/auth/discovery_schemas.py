"""Typed shapes for server discovery: the record a domain publishes,
and the answer discovery gives.

The record is a contract we define, published in two encodings — the
``/.well-known/igni.json`` document and the ``_igni`` TXT record — so
both parse into one :class:`PublishedRecord`, and the check of what it
names lives once, on that model.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from igni_core.core.auth.server_url import is_safe_url

logger = logging.getLogger(__name__)

#: How an answer was found. Empty when nothing was.
DiscoverySource = Literal["", "well-known", "dns"]


class Discovered(BaseModel):
    """What the domain said, or why it said nothing usable."""

    model_config = ConfigDict(frozen=True)

    api_url: str = ""
    #: Where a person signs in. Empty means "derive it from
    #: ``api_url``", which is right for a deployment serving both
    #: under one name.
    portal_url: str = ""
    #: How it was found, for the screen to say so.
    source: DiscoverySource = ""
    #: Why there is no answer. Shown only when somebody asks; the
    #: normal case is a domain with no record, which is not a fault.
    reason: str = ""

    @property
    def found(self) -> bool:
        return bool(self.api_url)

    @classmethod
    def nothing(cls, reason: str) -> Discovered:
        return cls(reason=reason)


class PublishedRecord(BaseModel):
    """The record a customer publishes, in either encoding.

    Unknown keys are ignored: a TXT name carries other records' words
    and a JSON document may grow fields this client does not read.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    api_url: str = ""
    portal_url: str = ""

    @field_validator("api_url", "portal_url", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> str:
        return str(value or "").strip().rstrip("/")

    @classmethod
    def from_json(cls, payload: bytes) -> PublishedRecord | Discovered:
        """The well-known document, or the reason it is unusable."""
        try:
            return cls.model_validate_json(payload)
        except ValidationError as exc:
            kinds = {error["type"] for error in exc.errors()}
            if "json_invalid" in kinds:
                return Discovered.nothing("The record there is not valid JSON.")
            return Discovered.nothing("The record there is not an object.")

    @classmethod
    def from_txt(cls, text: str) -> PublishedRecord:
        """``api_url=… portal_url=…``, words without ``=`` skipped."""
        pairs = (part.partition("=") for part in text.split())
        return cls.model_validate({key.strip(): value for key, sep, value in pairs if sep})

    def answer(self, source: DiscoverySource) -> Discovered:
        """What this record names, once checked, or why it is refused."""
        if not self.api_url:
            return Discovered.nothing("The record there does not name an api_url.")
        if not is_safe_url(self.api_url):
            return Discovered.nothing(
                f"The record there names {self.api_url!r}, which is not an https URL."
            )
        portal_url = self.portal_url
        if portal_url and not is_safe_url(portal_url):
            # The api_url is usable on its own and the portal is derived
            # when absent, so one bad field does not sink the lookup.
            logger.info("discovery: ignoring unsafe portal_url %r", portal_url)
            portal_url = ""
        return Discovered(api_url=self.api_url, portal_url=portal_url, source=source)
