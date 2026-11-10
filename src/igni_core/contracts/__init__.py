"""The contracts igni-core publishes for the repositories beside it.

* :mod:`igni_core.contracts.wire_schema` — ``docs/wire-schema.json``:
  every message the wire can carry, and every RPC payload the web
  client reads, as field lists. igni-app keeps a copy beside the web
  client and tests its reads against that copy.

The host object model (``docs/host-schema.json``) is published from
:mod:`igni_core.extensions.host.published`; it belongs to the
extension host, which owns the model, not to this package.
"""
