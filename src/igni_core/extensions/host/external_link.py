"""A link an extension asks igni to open in the person's browser."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExternalLink(BaseModel):
    """A link an extension asks igni to open in the person's browser.

    Absolute ``https`` only, with a host and no credentials: nothing
    that runs on this machine (``file:``, a custom scheme), nothing
    readable in transit, and nothing that hides who it signs in as.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _https(cls, url: str) -> str:
        if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
            raise ValueError("a link has no whitespace or control characters")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError(f"only https links open outside igni, not {parts.scheme or url!r}")
        if not parts.hostname:
            raise ValueError("a link names its host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("a link carries no credentials")
        return url


__all__ = ["ExternalLink"]
