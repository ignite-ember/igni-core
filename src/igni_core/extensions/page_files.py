"""In the cloud, igni's own port serves the extensions' page files to the gateway.

``docs/design/local-cloud-switch.md`` §15.3. On the desktop each
extension's page has a loopback origin of its own
(:mod:`igni_core.extensions.origin`), which igni's window frames directly.
In a VM those origins listen where the desktop cannot reach: the node
agent forwards one port, igni's WebSocket. So the gateway fetches a
page's files over that port, as plain HTTP before any upgrade:

    GET /.igni/pages/<extension id>/<path> HTTP/1.1
    Host: 127.0.0.1:<igni's port>
    X-Igni-Gateway: pages

and the desktop serves them to the frame from a loopback origin of its
own, one per extension. What is answered, and how, is the extension's own
:class:`~igni_core.extensions.origin.ExtensionOrigin` — the same file
resolution, the same refusal of every escape from the root, the same
content types and the same CSP — so a page in the cloud is held to what
it is held to on the desktop.

Three checks come first, each answered as plainly as a missing file:

* the stream must carry the node agent's ``pages`` mark: fetching an
  extension's page and speaking for an extension are one attested
  property of a stream (``backend/message_admission.py``); without it,
  ``404``;
* the request must name igni's own address as its ``Host``, or ``421``,
  as an origin's listener answers;
* the extension must have a running origin, and the file must exist
  inside its root and be at most :attr:`PageFiles.MAX_BYTES`, or ``404``.

``websockets`` cannot stream a response body, so a file is read whole;
the size cap is what bounds that. ``websockets`` hands this hook only what
could be a handshake — a ``GET`` — so nothing else is answerable here.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from igni_core.extensions.manifest import EXTENSION_ID

if TYPE_CHECKING:
    from igni_core.extensions.origin import ExtensionOrigins, HttpReply

logger = logging.getLogger(__name__)


class PageFiles:
    """Answers ``GET /.igni/pages/<id>/…`` on igni's port from each extension's origin.

    Built before the extension runtime exists — the transport listens
    first — so it starts unbound and answers ``404`` until :meth:`bind`.
    """

    #: Where page files are asked for. Nothing else on igni's port is.
    PREFIX: ClassVar[str] = "/.igni/pages/"
    #: The largest file served, held in memory for the length of a reply.
    MAX_BYTES: ClassVar[int] = 32 * 1024 * 1024

    def __init__(self) -> None:
        self._origins: ExtensionOrigins | None = None

    def bind(self, origins: ExtensionOrigins) -> None:
        """Answer from ``origins`` from now on: the extension runtime's."""
        self._origins = origins

    @classmethod
    def asks_for_pages(cls, request: Request) -> bool:
        """``request`` is for a page file, not a WebSocket upgrade."""
        if "Upgrade" in request.headers:
            return False
        return urlsplit(request.path).path.startswith(cls.PREFIX)

    def respond(self, request: Request, *, port: int, marked: bool) -> Response:
        """The answer to a page-file request on igni's port ``port``.

        ``marked`` is whether the request carries the node agent's
        ``pages`` mark, read by the transport off the request itself.
        """
        if not marked:
            return self._plain(HTTPStatus.NOT_FOUND)
        if request.headers.get("Host", "") != f"127.0.0.1:{port}":
            logger.debug("page files: refused Host %r", request.headers.get("Host", ""))
            return self._plain(HTTPStatus.MISDIRECTED_REQUEST)
        rest = request.path[len(self.PREFIX) :]
        extension_id, slash, target = rest.partition("/")
        extension_id = urlsplit(extension_id).path
        return self.answer(extension_id, slash + target if slash else "/")

    def answer(self, extension_id: str, target: str) -> Response:
        """``target`` (origin-form, from ``/``) of ``extension_id``'s page, as its origin answers."""
        if not EXTENSION_ID.match(extension_id):
            return self._plain(HTTPStatus.NOT_FOUND)
        origin = self._origins.get(extension_id) if self._origins is not None else None
        if origin is None:
            return self._plain(HTTPStatus.NOT_FOUND)
        reply = origin.answer("GET", target)
        if reply.file is not None and reply.length > self.MAX_BYTES:
            logger.warning(
                "page files: %s%s is %d bytes, over the %d a page file may be; not served",
                extension_id,
                target,
                reply.length,
                self.MAX_BYTES,
            )
            return self._plain(HTTPStatus.NOT_FOUND)
        return self._response(reply)

    @staticmethod
    def _response(reply: HttpReply) -> Response:
        """``reply`` as ``websockets`` sends it: its headers exactly, its body whole."""
        body = reply.body
        if reply.file is not None:
            try:
                body = reply.file.read_bytes()
            except OSError as exc:
                logger.debug("page files: could not read %s: %s", reply.file, exc)
                return PageFiles._plain(HTTPStatus.NOT_FOUND)
        headers = reply.headers()
        # The file may have changed since its size was taken.
        headers["Content-Length"] = str(len(body))
        return Response(reply.status, reply.reason, Headers(headers), body)

    @staticmethod
    def _plain(status: HTTPStatus) -> Response:
        body = f"{status.value} {status.phrase}\n".encode()
        headers = Headers(
            {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body)),
                "Connection": "close",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            }
        )
        return Response(status.value, status.phrase, headers, body)


__all__ = ["PageFiles"]
