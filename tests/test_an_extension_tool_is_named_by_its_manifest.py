"""Whose tool a registered name is, and what its manifest calls it."""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.extensions.host.tools import ExtensionTools
from igni_core.extensions.registry import ExtensionRegistry
from tests.host_model_helpers import manifest


def _tools(*ids: str) -> tuple[ExtensionTools, ExtensionRegistry]:
    registry = ExtensionRegistry()
    for extension_id in ids:
        # ``model_copy`` skips the id grammar, which has no underscore: the
        # registry is read by whatever ids it holds, and the rule below is
        # about ids one of which plus ``_`` begins another.
        registry.install(manifest().model_copy(update={"id": extension_id}))
    return ExtensionTools(registry), registry


def test_a_registered_tool_belongs_to_the_extension_in_its_name():
    tools, _ = _tools("acme.tickets", "acme.other")

    assert tools.owner("mcp_ext:acme.tickets_lookup_ticket") == "acme.tickets"
    assert tools.owner("mcp_ext:acme.other_run") == "acme.other"


def test_the_longest_matching_id_owns_a_tool_when_ids_prefix_each_other():
    tools, _ = _tools("acme.a", "acme.a_b")

    assert tools.owner("mcp_ext:acme.a_b_x") == "acme.a_b"
    assert tools.owner("mcp_ext:acme.a_x") == "acme.a"
    assert tools.manifest_name("mcp_ext:acme.a_b_x") == "acme.a_b.x"
    assert tools.manifest_name("mcp_ext:acme.a_x") == "acme.a.x"


def test_a_tool_no_installed_extension_owns_has_no_owner_and_no_manifest_name():
    tools, _ = _tools("acme.tickets")

    assert tools.owner("mcp_ext:acme.missing_run") is None
    assert tools.owner("mcp_other_run") is None
    assert tools.owner("shell") is None
    assert tools.owner("") is None
    assert tools.manifest_name("shell") == ""
    assert tools.manifest_name("mcp_ext:acme.missing_run") == ""


def test_with_nothing_installed_no_tool_is_owned():
    tools, _ = _tools()

    assert tools.owner("mcp_ext:acme.tickets_lookup_ticket") is None
    assert tools.manifest_name("mcp_ext:acme.tickets_lookup_ticket") == ""


def test_a_name_that_is_only_the_prefix_is_not_a_tool():
    tools, _ = _tools("acme.tickets")

    assert tools.owner("mcp_ext:acme.tickets") is None
    assert tools.manifest_name("mcp_ext:acme.tickets") == ""
    assert tools.manifest_name("mcp_ext:acme.tickets_") == "acme.tickets."


def test_the_registry_is_read_live_as_extensions_come_and_go():
    tools, registry = _tools()
    name = "mcp_ext:acme.tickets_lookup_ticket"

    registry.install(manifest("acme.tickets"))
    assert tools.manifest_name(name) == "acme.tickets.lookup_ticket"

    registry.remove("acme.tickets")
    assert tools.owner(name) is None
    assert tools.manifest_name(name) == ""


def test_the_tool_a_value_carries_is_named_as_its_manifest_names_it():
    tools, _ = _tools("acme.tickets")

    carrying = SimpleNamespace(tool_name="mcp_ext:acme.tickets_lookup_ticket")

    assert tools.of(carrying) == "acme.tickets.lookup_ticket"  # type: ignore[arg-type]
    assert tools.of(SimpleNamespace(tool_name="shell")) == ""  # type: ignore[arg-type]


def test_a_value_with_no_tool_name_or_a_non_text_one_names_no_tool():
    tools, _ = _tools("acme.tickets")

    assert tools.of(SimpleNamespace()) is None  # type: ignore[arg-type]
    assert tools.of(SimpleNamespace(tool_name=None)) is None  # type: ignore[arg-type]
    assert tools.of(SimpleNamespace(tool_name=7)) is None  # type: ignore[arg-type]
