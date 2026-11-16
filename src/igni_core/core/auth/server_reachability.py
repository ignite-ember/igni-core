"""Whether the igni server was there the last time igni really asked it.

The account chip says whether the person is signed in to igni server.
Being signed in is not the same as igni server being *there*: with the
server down, the token on disk is still valid, so the chip kept saying
"Account" while every cloud call failed (campaign R638).

igni does not probe the server to find out — a probe is traffic nobody
asked for, on a timer, forever. Instead every real request igni makes
to igni server records what it learned, and the chip reads the answer:

* a request that never got an answer — connection refused, timeout, no
  DNS — says the server was not there;
* a 5xx says the server was there but broken;
* any other answer says the server was there. A 401 is a credentials
  problem, not a reachability one: the expired token is already handled
  by the credentials themselves, and calling a refused token "unreachable"
  would send somebody to look at the wrong thing.

One recorder per server, shared by every layer that asks one: the
session's cloud model catalogue, the signed-in client the backend uses,
the desktop's event pull, the notification stream. The read side is
:class:`~igni_core.core.session.cloud_auth.SessionCloudAuth.connected`.
"""

from __future__ import annotations


class ServerReachability:
    """What the last real request to one igni server said about it.

    A fresh recorder knows nothing, and reads as reachable: igni has
    not asked yet, so it has no reason to doubt the server. The first
    real request settles it.
    """

    def __init__(self) -> None:
        self._reachable = True
        self._detail = ""

    @property
    def reachable(self) -> bool:
        """Whether the server answered the last real request."""
        return self._reachable

    @property
    def detail(self) -> str:
        """What the last request found out, for logs and the status line."""
        return self._detail

    def record_answered(self, status_code: int) -> None:
        """The server answered. A 5xx is an answer, but a broken one."""
        self._reachable = status_code < 500
        self._detail = f"HTTP {status_code}"

    def record_unreachable(self, reason: str) -> None:
        """The request never got an answer at all."""
        self._reachable = False
        self._detail = reason


#: One recorder per server URL, for the life of the process. Reachability
#: is a property of the server, not of one session or one layer: the
#: session's catalogue fetch and the backend's notification stream were
#: asking the same machine, and the chip has to read what either of them
#: found.
_REACHABILITY: dict[str, ServerReachability] = {}


def reachability_of(api_url: str | None) -> ServerReachability | None:
    """The one recorder for this igni server, shared by every layer.

    ``None`` when no server is configured: there is nothing to ask, and
    nothing to record.
    """
    if not api_url or not api_url.strip():
        return None
    key = api_url.strip().rstrip("/")
    recorder = _REACHABILITY.get(key)
    if recorder is None:
        recorder = _REACHABILITY[key] = ServerReachability()
    return recorder


def record_answered(api_url: str | None, status_code: int) -> None:
    """The server answered this request — note how."""
    recorder = reachability_of(api_url)
    if recorder is not None:
        recorder.record_answered(status_code)


def record_unreachable(api_url: str | None, reason: str) -> None:
    """This request never got an answer — note why."""
    recorder = reachability_of(api_url)
    if recorder is not None:
        recorder.record_unreachable(reason)


def reset_reachability() -> None:
    """Forget every recorder.

    For tests: the recorders live for the life of the process, so a
    test that records a failure would otherwise leak into the next one.
    """
    _REACHABILITY.clear()


__all__ = [
    "ServerReachability",
    "reachability_of",
    "record_answered",
    "record_unreachable",
    "reset_reachability",
]
