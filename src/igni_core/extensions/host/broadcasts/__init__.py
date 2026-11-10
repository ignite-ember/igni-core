"""What igni does, as host-model signals.

Two sources, which together are every change igni makes:

* **every message igni sends its windows** — run events, mirroring, the
  push channels. :meth:`BroadcastSignals.observe` sees each one on its
  way out (the backend's transport is wrapped once, at the root) and
  translates it into the model's signal, in the model's shape;
* **every write an RPC makes**, whoever made it — a window or an
  extension. :meth:`BroadcastSignals.after_rpc` is told after each
  successful one (:class:`~igni_core.extensions.host.rpc_write.RpcWrite`),
  so "every write is a signal" holds for writes that broadcast nothing of
  their own.

The translation is the point of this package. igni's broadcasts are its
private wire and change with its UI; the :class:`~.signal.Signal` models
here are the promise — each one the host schema's type for its wire,
field for field. A field renamed on the wire is fixed in one model, once,
and no extension notices.

It only translates: what the host remembers lives in
:mod:`igni_core.extensions.host.state`, and :mod:`.translators` is where
each translator is built. :class:`BroadcastSignals` (:mod:`.router`) is
the one entry point.
"""

from igni_core.extensions.host.broadcasts.router import BroadcastSignals

__all__ = ["BroadcastSignals"]
