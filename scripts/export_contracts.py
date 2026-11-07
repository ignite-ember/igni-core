"""Write the contracts igni-core publishes for igni-sdk and igni-app.

    python scripts/export_contracts.py

* ``docs/host-schema.json`` — the host object model; igni-sdk generates
  both SDKs' ``host`` objects from its copy;
* ``docs/wire-schema.json`` — every message and RPC payload the web
  client reads; igni-app tests its reads against its copy.

``make contracts`` runs this. A test fails while either is stale.
"""

from __future__ import annotations

from igni_core.extensions.host import published
from igni_core.protocol import wire_schema


def main() -> None:
    print(f"wrote {published.write()}")
    path, schema = wire_schema.write()
    print(f"wrote {path} ({len(schema['messages'])} messages, {len(schema['rpc'])} rpc payloads)")


if __name__ == "__main__":
    main()
