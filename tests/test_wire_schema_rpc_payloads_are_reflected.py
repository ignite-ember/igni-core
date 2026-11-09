"""The non-envelope RPC payloads in the wire schema come from the models.

The RPC payload list used to be three hand-written field lists,
justified by a comment saying the producers were "plain dicts built by
hand in the backend, so there is no model to reflect on". All three
producers return Pydantic models:

* ``BackendServer.loop_status`` -> ``LoopStatusSnapshot``
* ``BackendServer.get_pending_messages`` -> ``list[PendingMessage]``
* ``BackendServer.get_mcp_server_details`` -> ``list[MCPServerSnapshot]``

and the hand-kept copy had already dropped a field: ``mcp_server`` was
missing ``tools_disabled``. The snapshot exists so a backend rename
breaks a test instead of blanking the UI; a field the snapshot never
knew about is a field that protection never covered.

The list is now
:data:`~igni_core.contracts.wire_schema.RPC_PAYLOAD_MODELS` — every
non-envelope RPC payload and the model it is reflected from — and
these tests assert the schema is derived from those models, so the next
field added to any of them cannot go missing.
"""

from __future__ import annotations

from igni_core.contracts.wire_schema import RPC_PAYLOAD_MODELS, WireSchemaPublisher


def test_each_rpc_payload_matches_its_model_exactly():
    """Not "is a subset of" — exactly. A stale entry is as wrong as a
    missing one; the frontend contract test compares both directions."""
    rpc = WireSchemaPublisher().build().rpc

    assert rpc, "the wire schema has no RPC payloads at all"

    for key, model in RPC_PAYLOAD_MODELS.items():
        assert rpc[key] == sorted(model.model_fields), (
            f"wire schema entry {key!r} has drifted from "
            f"{model.__name__}: schema={rpc[key]}, "
            f"model={sorted(model.model_fields)}"
        )


def test_mcp_server_carries_tools_disabled():
    """The field the hand-written list had actually lost.

    ``tools_disabled`` is what the MCP panel reads to grey out a
    per-tool toggle; it shipped on the model and never reached the
    snapshot, so the contract test could not have caught a rename of
    it. Pinned by name because "the list is reflected now" is the
    general claim and this is the specific regression.
    """
    assert "tools_disabled" in WireSchemaPublisher().build().rpc["mcp_server"]
