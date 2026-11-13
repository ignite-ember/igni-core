"""The LSP wire models: how an inbound JSON body is classified, how the
handshake params dump onto the wire, and how a raw ``.lsp.json`` entry becomes
a typed server config. Also that the package root re-exports what it lists.
"""

from __future__ import annotations

import pytest

import igni_core.core.lsp as lsp_package
from igni_core.core.lsp.schemas import (
    ClientCapabilities,
    InitializeParams,
    JsonRpcNotification,
    JsonRpcResponse,
    JsonRpcServerRequest,
    LspConfigFile,
    LspServerConfig,
    LspServerInfo,
    parse_inbound_message,
)


def test_every_name_the_package_lists_resolves_from_its_root():
    assert lsp_package.__all__ == sorted(lsp_package.__all__)
    for name in lsp_package.__all__:
        assert getattr(lsp_package, name) is not None, name


class TestClassifyingInboundMessages:
    def test_an_id_with_a_method_is_a_server_request(self):
        msg = parse_inbound_message({"jsonrpc": "2.0", "id": 4, "method": "workspace/x"})
        assert isinstance(msg, JsonRpcServerRequest)
        assert (msg.id, msg.method) == (4, "workspace/x")

    def test_an_id_alone_is_a_response_and_zero_is_an_id(self):
        msg = parse_inbound_message({"jsonrpc": "2.0", "id": 0, "result": {"a": 1}})
        assert isinstance(msg, JsonRpcResponse)
        assert (msg.id, msg.result) == (0, {"a": 1})

    def test_an_error_response_carries_code_and_message(self):
        msg = parse_inbound_message({"id": 1, "error": {"code": -1, "message": "bad"}})
        assert isinstance(msg, JsonRpcResponse)
        assert (msg.error.code, msg.error.message) == (-1, "bad")

    def test_a_method_alone_or_with_a_null_id_is_a_notification(self):
        for raw in ({"method": "m"}, {"id": None, "method": "m", "params": []}):
            msg = parse_inbound_message(raw)
            assert isinstance(msg, JsonRpcNotification), raw
            assert msg.method == "m"

    @pytest.mark.parametrize(
        "raw",
        [
            [],
            "text",
            5,
            None,
            {},
            {"jsonrpc": "2.0"},
            {"id": None},
            {"id": "abc", "result": 1},
            {"id": 1, "error": {"message": "no code"}},
            {"method": "m", "params": "scalar"},
            {"jsonrpc": "1.0", "id": 1},
        ],
    )
    def test_anything_else_is_dropped_as_none_without_raising(self, raw):
        assert parse_inbound_message(raw) is None


class TestWireDumps:
    def test_initialize_params_dump_to_the_lsp_camel_case_shape(self):
        params = InitializeParams(processId=7, rootUri="file:///r", initializationOptions={"k": 1})
        assert params.model_dump(by_alias=True, exclude_none=True) == {
            "processId": 7,
            "rootUri": "file:///r",
            "capabilities": {"workspace": {}, "textDocument": {}},
            "initializationOptions": {"k": 1},
        }

    def test_unset_initialize_fields_drop_out(self):
        assert InitializeParams().model_dump(by_alias=True, exclude_none=True) == {
            "capabilities": {"workspace": {}, "textDocument": {}},
            "initializationOptions": {},
        }

    def test_capabilities_keep_unknown_keys(self):
        caps = ClientCapabilities(window={"workDoneProgress": True})
        assert caps.model_dump()["window"] == {"workDoneProgress": True}

    def test_server_info_exposes_exactly_the_four_fields_the_tool_prints(self):
        info = LspServerInfo(name="a", languages=["x"], running=True, last_error="")
        assert list(info.model_dump()) == ["name", "languages", "running", "last_error"]

    def test_a_config_file_without_servers_has_none_and_a_wrong_shape_is_rejected(self):
        assert LspConfigFile.model_validate({}).lspServers == {}
        assert LspConfigFile.model_validate({"other": 1}).lspServers == {}
        with pytest.raises(ValueError):
            LspConfigFile.model_validate({"lspServers": []})


class TestReadingAServerEntry:
    def test_env_is_stringified_and_a_non_mapping_env_is_empty(self):
        cfg = LspServerConfig.from_raw("a", {"command": "c", "env": {1: 2, "K": True}})
        assert cfg.env == {"1": "2", "K": "True"}
        for bad in ("x", ["a"], 5, None):
            assert LspServerConfig.from_raw("a", {"command": "c", "env": bad}).env == {}

    def test_options_that_are_not_a_mapping_are_empty(self):
        for bad in ("x", [1], 5, None):
            cfg = LspServerConfig.from_raw("a", {"command": "c", "initializationOptions": bad})
            assert cfg.initialization_options == {}

    def test_a_root_uri_that_is_not_text_is_unset(self):
        assert LspServerConfig.from_raw("a", {"command": "c", "rootUri": 5}).root_uri is None
        assert LspServerConfig.from_raw("a", {"command": "c", "rootUri": "file:///r"}).root_uri == (
            "file:///r"
        )

    def test_null_args_and_languages_are_empty_lists(self):
        cfg = LspServerConfig.from_raw("a", {"command": "c", "args": None, "languages": None})
        assert cfg.args == [] and cfg.languages == []

    @pytest.mark.parametrize(
        "extra",
        [{"args": 5}, {"args": "--stdio"}, {"languages": 5}, {"languages": {"a": 1}}],
    )
    def test_args_or_languages_that_are_not_lists_make_the_entry_unusable(self, extra):
        assert LspServerConfig.from_raw("a", {"command": "c", **extra}) is None

    def test_a_command_must_be_non_blank_text(self):
        for bad in (None, "", "  ", 5, ["x"]):
            assert LspServerConfig.from_raw("a", {"command": bad}) is None
        assert LspServerConfig.from_raw("a", "x") is None

    def test_the_namespace_prefixes_the_name_with_a_colon(self):
        assert LspServerConfig.from_raw("py", {"command": "c"}, namespace="ns").name == "ns:py"
        assert LspServerConfig.from_raw("py", {"command": "c"}, namespace="").name == "py"

    def test_unicode_names_and_commands_survive(self):
        cfg = LspServerConfig.from_raw("sérveur", {"command": "bïn"})
        assert (cfg.name, cfg.command) == ("sérveur", "bïn")
