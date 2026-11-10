"""Which URLs we are willing to write down as the server.

One rule, asked of a discovered address and of one somebody typed by
hand alike: the answer must not differ by how it arrived, because a
plaintext host decides where sign-in codes are sent either way.
"""

from __future__ import annotations

from urllib.parse import urlparse

#: Hosts allowed to speak plaintext, for developing against a local
#: deployment. Nothing else.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def is_safe_url(url: str) -> bool:
    """Whether ``url`` is https, or http to a local deployment."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    host = parsed.hostname or ""
    return parsed.scheme == "http" and host in LOCAL_HOSTS
