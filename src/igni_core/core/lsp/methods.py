"""Which LSP methods are notifications.

JSON-RPC draws a line the LSP spec keeps: a *request* carries an
``id`` and gets a reply, a *notification* carries none and expects
none. The agent tool issued requests only, so a notification method
sent as a request is refused by the server with ``-32601 Unknown
request`` — the document is never opened, and the diagnostics that
depend on an open document come back empty.

This module names the methods the LSP spec defines as notifications,
so the tool can send them the way the spec says. Only these are
routed as notifications; every other method keeps its current
request behaviour, so a method this list has never heard of still
reaches the server as a request and still reports the server's own
answer rather than one of ours.

``initialized`` and ``exit`` are deliberately absent. The client
sends both itself as part of the lifecycle, and a stray ``exit``
would shut the language server down under the session.
"""

from __future__ import annotations

NOTIFICATION_METHODS: frozenset[str] = frozenset(
    {
        # The document lifecycle. ``didOpen`` is the one that makes
        # diagnostics work: a server answers a diagnostics request
        # for a document it has been told about.
        "textDocument/didOpen",
        "textDocument/didChange",
        "textDocument/didClose",
        "textDocument/didSave",
        # Workspace state the client keeps the server informed of.
        "workspace/didChangeConfiguration",
        "workspace/didChangeWatchedFiles",
        # The ``$/`` family — by the spec, a server must ignore any
        # ``$/`` method it does not know, which is why these read as
        # notifications rather than requests.
        "$/cancelRequest",
        "$/setTrace",
    }
)


def is_notification_method(method: str) -> bool:
    """True when ``method`` is one the LSP spec defines as a
    notification — sent without an ``id``, answered by nothing."""
    return method in NOTIFICATION_METHODS
