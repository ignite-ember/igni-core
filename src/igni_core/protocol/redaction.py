"""What a result may say about a secret: that there is one, never what it is."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def url_without_credentials(url: str) -> str:
    """``url`` without what can carry a credential: the user and password
    before the host, and the query string (``?key=...``). A value that is
    not a URL comes back unchanged."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if not parts.netloc:
        return url
    host = parts.hostname or ""
    if ":" in host:
        # An IPv6 literal: ``hostname`` drops its brackets, a URL needs them.
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


__all__ = ["url_without_credentials"]
