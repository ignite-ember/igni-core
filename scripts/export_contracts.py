"""Write the contracts igni-core publishes for its SDKs and its clients.

    python scripts/export_contracts.py

* ``docs/host-schema.json`` — the host object model; the SDKs generate
  their ``host`` objects from their copy;
* ``docs/rpc-contract.json`` — every RPC method and message, typed and
  documented; the SDKs generate their client from their copy;
* ``docs/wire-schema.json`` — every message and RPC payload the web
  client reads; a client tests its reads against its copy.

``make contracts`` runs this. A test fails while any of them is stale.
"""

from __future__ import annotations

from igni_core.contracts.wire_schema import EmptyWireContractError, WireSchemaPublisher
from igni_core.extensions.host import published
from igni_core.protocol import rpc_contract


def main() -> None:
    print(f"wrote {published.write()}")
    path, contract = rpc_contract.write()
    print(
        f"wrote {path} ({len(contract['methods'])} methods, {len(contract['messages'])} messages)"
    )
    publisher = WireSchemaPublisher()
    try:
        path, schema = publisher.write()
    except EmptyWireContractError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"wrote {path} ({len(schema.messages)} messages, {len(schema.rpc)} rpc payloads)")


if __name__ == "__main__":
    main()
