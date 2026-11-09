"""Protocol message types for BE↔FE communication.

**This module is now a re-export shim.** The wire schemas live in
:mod:`igni_core.protocol.schemas`, split by concern:

* :mod:`.schemas.envelope` — base :class:`Message` +
  :class:`RunHeader` + :class:`RunScopedMessage` mixin.
* :mod:`.schemas.enums` — every wire-contract StrEnum.
* :mod:`.schemas.be_events` — BE → FE run / tool / task events.
* :mod:`.schemas.mirroring` — multi-client broadcasts.
* :mod:`.schemas.fe_actions` — FE → BE actions.
* :mod:`.schemas.rpc` — process-split RPC.
* :mod:`.schemas.push` — push notifications + typed channel enum.
* :mod:`.schemas.internal` — intermediate value objects (not wire).

Existing callsites (``from igni_core.protocol.messages import
UserMessage`` etc.) keep working unchanged — every public symbol
is re-exported below. New code should import directly from the
sub-modules under :mod:`igni_core.protocol.schemas` when the
concern is clear (e.g. a slash-command handler importing from
:mod:`.schemas.be_events`).

The wire contract is *not* discovered from this namespace. It is
stated explicitly in :mod:`igni_core.protocol.schemas.wire`, which
names every message class that can arrive on the wire and builds the
``type``-discriminated union the deserializer validates against.
``schemas.__all__`` remains the source of truth for what this shim
re-exports.
"""

from __future__ import annotations

from igni_core.protocol.schemas import *  # noqa: F401,F403
from igni_core.protocol.schemas import __all__ as _schemas_all

__all__ = list(_schemas_all)
