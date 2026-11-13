"""The MCP wire models: what they read off the SDK's objects and what they dump.

The SDK's own ``Resource`` and ``Prompt`` are used, so a rename on the
SDK's side (``mimeType`` became ``mime_type`` in mcp 2) shows here.
"""

from __future__ import annotations

from types import SimpleNamespace

from mcp import types

from igni_core.core.mcp.schemas import (
    MCPConnectResult,
    MCPPrompt,
    MCPResource,
    MCPToolInfo,
)


def test_a_real_sdk_resource_keeps_its_uri_name_description_and_mime_type():
    resource = types.Resource(
        uri="file:///notes/today.md",
        name="today",
        description="Today's notes",
        mime_type="text/markdown",
    )
    assert MCPResource.from_sdk(resource) == MCPResource(
        uri="file:///notes/today.md",
        name="today",
        description="Today's notes",
        mime_type="text/markdown",
    )


def test_a_resource_with_only_a_uri_has_empty_strings_for_the_rest():
    resource = MCPResource.from_sdk(types.Resource(uri="file:///a", name="a"))
    assert (resource.description, resource.mime_type) == ("", "")


def test_a_resource_with_none_or_missing_attributes_projects_to_empty_strings():
    none = SimpleNamespace(uri=None, name=None, description=None, mime_type=None)
    assert MCPResource.from_sdk(none) == MCPResource(uri="")
    assert MCPResource.from_sdk(object()) == MCPResource(uri="")


def test_a_uri_that_is_not_text_is_turned_into_text():
    assert MCPResource.from_sdk(SimpleNamespace(uri=3, name="n")).uri == "3"


def test_a_real_sdk_prompt_is_flattened_to_its_argument_names():
    prompt = types.Prompt(
        name="summarise",
        description="Summarise a note",
        arguments=[
            types.PromptArgument(name="note", required=True),
            types.PromptArgument(name="style"),
        ],
    )
    assert MCPPrompt.from_sdk(prompt) == MCPPrompt(
        name="summarise", description="Summarise a note", arguments=["note", "style"]
    )


def test_a_prompt_without_arguments_has_an_empty_list():
    assert MCPPrompt.from_sdk(types.Prompt(name="hello")).arguments == []
    assert MCPPrompt.from_sdk(SimpleNamespace(name="hello", arguments=None)).arguments == []


def test_a_prompt_argument_without_a_name_becomes_an_empty_name():
    prompt = SimpleNamespace(name="p", arguments=[SimpleNamespace(), SimpleNamespace(name="x")])
    assert MCPPrompt.from_sdk(prompt).arguments == ["", "x"]


def test_a_prompt_that_says_nothing_has_an_empty_description():
    assert MCPPrompt.from_sdk(types.Prompt(name="p")).description == ""
    assert MCPPrompt.from_sdk(SimpleNamespace(name="p", description=None)).description == ""


def test_a_tool_row_defaults_to_enabled_with_no_description():
    row = MCPToolInfo(name="search")
    assert (row.description, row.enabled) == ("", True)


def test_a_successful_connect_carries_the_client_and_no_reason():
    client = object()
    result = MCPConnectResult.success(client)  # type: ignore[arg-type]
    assert (result.ok, result.client, result.reason) == (True, client, "")


def test_a_failed_connect_carries_the_reason_and_no_client():
    result = MCPConnectResult.failure("it did not start")
    assert (result.ok, result.client, result.reason) == (False, None, "it did not start")
    assert MCPConnectResult.failure("").reason == ""


def test_the_live_client_is_never_part_of_a_dump():
    result = MCPConnectResult.success(object())  # type: ignore[arg-type]
    assert result.model_dump() == {"ok": True, "reason": ""}
    assert "client" not in result.model_dump_json()


def test_the_models_in_the_rpc_contract_describe_every_field_for_the_generated_docs():
    # MCPResource and MCPPrompt are the two that reach docs/rpc-contract.json.
    for model in (MCPResource, MCPPrompt):
        schema = model.model_json_schema()
        assert model.__doc__ and model.__doc__.strip(), model.__name__
        for field, spec in schema["properties"].items():
            assert spec.get("description"), f"{model.__name__}.{field} has no description"
