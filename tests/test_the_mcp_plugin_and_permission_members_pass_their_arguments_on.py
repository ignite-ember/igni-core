"""``host.mcp``, ``host.plugins`` and ``host.permissions``.

A member takes an extension's parameters and asks igni's own RPC with
them: the right names, the optional ones left empty when absent, a
boolean read as a boolean. What igni answers is read back through its
model, and a refusal igni words as an ``info`` of type ``error`` reaches
the extension as a refusal. Driven through the real host model; the RPCs
whose arguments are looked at come from :func:`tests.scripted_backend.script`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig
from tests.scripted_backend import script

EVERYTHING = [p.name for p in HOST_SCHEMA.permissions if p.name != "workspace"]


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


INFO = {"type": "info", "text": "done"}
REFUSAL = {"type": "error", "text": "no such thing"}


class TestTheMcpServers:
    async def test_the_servers_their_status_and_their_details_are_listed(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        [status] = ok(await rig.ask(ext, "get", "mcp.status"))
        assert (status["name"], status["connected"]) == ("github", False)
        [details] = ok(await rig.ask(ext, "get", "mcp.details"))
        assert (details["name"], details["transport"]) == ("github", "stdio")

    @pytest.mark.parametrize("member", ["mcp.servers", "mcp.status", "mcp.details"])
    async def test_a_row_that_is_not_a_server_is_refused(self, rig: HostRig, member: str) -> None:
        ext = rig.install(host=EVERYTHING)
        method = {
            "mcp.servers": RpcMethod.GET_MCP_SERVERS,
            "mcp.status": RpcMethod.GET_MCP_STATUS,
            "mcp.details": RpcMethod.GET_MCP_SERVER_DETAILS,
        }[member]
        script(rig, {method: [{"connected": True}]})
        message = refused(await rig.ask(ext, "get", member), "failed")
        assert member in message

    async def test_no_servers_is_an_empty_list(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        rig.session.get_mcp_servers = lambda: []  # type: ignore[method-assign]
        assert ok(await rig.ask(ext, "get", "mcp.servers")) == []

    async def test_connect_and_disconnect_name_the_server_igni_is_to_act_on(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        backend = script(rig, {RpcMethod.MCP_CONNECT: INFO, RpcMethod.MCP_DISCONNECT: INFO})
        assert ok(await rig.ask(ext, "call", "mcp.connect", name="github")) is None
        assert ok(await rig.ask(ext, "call", "mcp.disconnect", name="slack")) is None
        assert backend.asked(RpcMethod.MCP_CONNECT) == [{"server_name": "github"}]
        assert backend.asked(RpcMethod.MCP_DISCONNECT) == [{"server_name": "slack"}]

    async def test_a_call_without_a_server_name_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        refused(await rig.ask(ext, "call", "mcp.connect"), "invalid")
        refused(await rig.ask(ext, "call", "mcp.disconnect"), "invalid")

    async def test_ensure_answers_what_igni_connected(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(
            rig,
            {
                RpcMethod.ENSURE_MCP: {
                    "connected": ["github"],
                    "failed": {"slack": "timed out"},
                    "rebuilt": True,
                    "skipped_reason": None,
                }
            },
        )
        result = ok(await rig.ask(ext, "call", "mcp.ensure"))
        assert result["connected"] == ["github"]
        assert result["failed"] == {"slack": "timed out"}
        assert result["rebuilt"] is True

    async def test_an_ensure_answer_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.ENSURE_MCP: {"connected": "github"}})
        message = refused(await rig.ask(ext, "call", "mcp.ensure"), "failed")
        assert "mcp.ensure" in message

    @pytest.mark.parametrize("on", [True, False])
    async def test_a_tool_is_switched_to_the_state_asked_for(self, rig: HostRig, on: bool) -> None:
        ext = rig.install(host=EVERYTHING)
        result = ok(
            await rig.ask(
                ext, "call", "mcp.set_tool_enabled", server="github", tool="search", on=on
            )
        )
        assert result == {"server": "github", "tool": "search", "enabled": on}

    async def test_the_toggle_is_asked_of_igni_by_the_names_it_reads(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        backend = script(
            rig,
            {RpcMethod.SET_MCP_TOOL_ENABLED: {"server": "s", "tool": "t", "enabled": False}},
        )
        ok(await rig.ask(ext, "call", "mcp.set_tool_enabled", server="s", tool="t", on=False))
        assert backend.asked(RpcMethod.SET_MCP_TOOL_ENABLED) == [
            {"server": "s", "tool": "t", "enabled": False}
        ]

    async def test_a_toggle_answer_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.SET_MCP_TOOL_ENABLED: {"server": "s"}})
        message = refused(
            await rig.ask(ext, "call", "mcp.set_tool_enabled", server="s", tool="t", on=True),
            "failed",
        )
        assert "mcp.set_tool_enabled" in message

    async def test_a_toggle_without_its_tool_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        refused(await rig.ask(ext, "call", "mcp.set_tool_enabled", server="s", on=True), "invalid")


class TestThePlugins:
    async def test_the_installed_plugins_and_the_marketplaces_are_listed(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        script(
            rig,
            {
                RpcMethod.GET_PLUGIN_DETAILS: [
                    {"name": "lint", "version": "1.2", "enabled": False}
                ],
                RpcMethod.GET_MARKETPLACES: [{"name": "acme", "url": "https://m.test/acme.git"}],
            },
        )
        [plugin] = ok(await rig.ask(ext, "get", "plugins.installed"))
        assert (plugin["name"], plugin["version"], plugin["enabled"]) == ("lint", "1.2", False)
        [market] = ok(await rig.ask(ext, "get", "plugins.marketplaces"))
        assert (market["name"], market["url"]) == ("acme", "https://m.test/acme.git")
        assert market["plugins"] == []

    async def test_nothing_installed_is_an_empty_list(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "get", "plugins.installed")) == []
        assert ok(await rig.ask(ext, "get", "plugins.marketplaces")) == []

    @pytest.mark.parametrize(
        ("member", "method"),
        [
            ("plugins.installed", RpcMethod.GET_PLUGIN_DETAILS),
            ("plugins.marketplaces", RpcMethod.GET_MARKETPLACES),
        ],
    )
    async def test_a_row_that_is_not_a_plugin_is_refused(
        self, rig: HostRig, member: str, method: RpcMethod
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {method: [{"version": "1"}]})
        message = refused(await rig.ask(ext, "get", member), "failed")
        assert member in message

    async def test_contents_are_asked_of_the_plugin_named(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        contents = ok(await rig.ask(ext, "call", "plugins.contents", name="lint"))
        assert contents["name"] == "lint"

    async def test_a_preview_carries_the_source_and_the_optional_branch_and_subdir(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        backend = script(rig, {RpcMethod.PREVIEW_PLUGIN: {"name": "p", "root_path": "/r"}})
        ok(await rig.ask(ext, "call", "plugins.preview", source="https://g.test/p.git"))
        ok(
            await rig.ask(
                ext,
                "call",
                "plugins.preview",
                source="https://g.test/p.git",
                branch="dev",
                subdir="plugins/p",
            )
        )
        assert backend.asked(RpcMethod.PREVIEW_PLUGIN) == [
            {"source": "https://g.test/p.git", "branch": None, "subdir": None},
            {"source": "https://g.test/p.git", "branch": "dev", "subdir": "plugins/p"},
        ]

    async def test_a_preview_igni_answers_oddly_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.PREVIEW_PLUGIN: {"skills": "all"}})
        message = refused(await rig.ask(ext, "call", "plugins.preview", source="s"), "failed")
        assert "plugins.preview" in message

    async def test_a_plugin_is_installed_updated_and_removed_by_its_name(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        ok(await rig.ask(ext, "call", "plugins.install", ref="@acme/lint"))
        ok(await rig.ask(ext, "call", "plugins.update", name="lint"))
        ok(await rig.ask(ext, "call", "plugins.remove", name="lint"))
        assert rig.session.plugins.installed == ["@acme/lint"]
        assert rig.session.plugins.updated == ["lint"]
        assert rig.session.plugins.removed == ["lint"]

    async def test_install_and_update_carry_the_ref_to_check_out_when_given(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        backend = script(rig, {RpcMethod.INSTALL_PLUGIN: INFO, RpcMethod.UPDATE_PLUGIN: INFO})
        ok(await rig.ask(ext, "call", "plugins.install", ref="u"))
        ok(await rig.ask(ext, "call", "plugins.install", ref="u", install_ref="v2"))
        ok(await rig.ask(ext, "call", "plugins.update", name="n"))
        ok(await rig.ask(ext, "call", "plugins.update", name="n", install_ref="v3"))
        assert backend.asked(RpcMethod.INSTALL_PLUGIN) == [
            {"ref": "u", "install_ref": None},
            {"ref": "u", "install_ref": "v2"},
        ]
        assert backend.asked(RpcMethod.UPDATE_PLUGIN) == [
            {"name": "n", "install_ref": None},
            {"name": "n", "install_ref": "v3"},
        ]

    @pytest.mark.parametrize(
        ("member", "method", "params"),
        [
            ("plugins.install", RpcMethod.INSTALL_PLUGIN, {"ref": "u"}),
            ("plugins.update", RpcMethod.UPDATE_PLUGIN, {"name": "n"}),
            ("plugins.remove", RpcMethod.REMOVE_PLUGIN, {"name": "n"}),
            ("plugins.set_enabled", RpcMethod.SET_PLUGIN_ENABLED, {"name": "n", "on": True}),
            ("plugins.add_marketplace", RpcMethod.ADD_MARKETPLACE, {"url": "u"}),
            ("plugins.remove_marketplace", RpcMethod.REMOVE_MARKETPLACE, {"name": "n"}),
            ("plugins.refresh_marketplaces", RpcMethod.REFRESH_MARKETPLACES, {}),
        ],
    )
    async def test_what_igni_refuses_is_refused_in_its_words(
        self, rig: HostRig, member: str, method: RpcMethod, params: dict[str, Any]
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {method: REFUSAL})
        message = refused(await rig.ask(ext, "call", member, **params), "refused")
        assert message == "no such thing"

    @pytest.mark.parametrize("on", [True, False])
    async def test_a_plugin_is_switched_on_or_off_as_asked(self, rig: HostRig, on: bool) -> None:
        ext = rig.install(host=EVERYTHING)
        ok(await rig.ask(ext, "call", "plugins.set_enabled", name="lint", on=on))
        assert rig.session.plugins.enabled == [("lint", on)]

    async def test_marketplaces_are_added_removed_and_refreshed(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        ok(await rig.ask(ext, "call", "plugins.add_marketplace", url="https://m.test/a.git"))
        ok(await rig.ask(ext, "call", "plugins.remove_marketplace", name="a"))
        ok(await rig.ask(ext, "call", "plugins.refresh_marketplaces"))
        ok(await rig.ask(ext, "call", "plugins.refresh_marketplaces", name="b"))
        assert rig.session.marketplaces.added == ["https://m.test/a.git"]
        assert rig.session.marketplaces.removed == ["a"]
        assert rig.session.marketplaces.refreshed == [None, "b"]

    async def test_a_call_without_what_it_acts_on_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        refused(await rig.ask(ext, "call", "plugins.install"), "invalid")
        refused(await rig.ask(ext, "call", "plugins.add_marketplace"), "invalid")
        refused(await rig.ask(ext, "call", "plugins.set_enabled", name="n"), "invalid")
        assert rig.session.plugins.installed == []


class TestThePermissions:
    async def test_the_mode_is_the_one_igni_reports(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "get", "permissions.mode")) == "default"
        script(rig, {RpcMethod.GET_STATUS: {"model": "small", "permission_mode": "plan"}})
        assert ok(await rig.ask(ext, "get", "permissions.mode")) == "plan"

    async def test_a_check_with_no_arguments_asks_with_none(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        backend = script(rig, {RpcMethod.CHECK_PERMISSION: "deny"})
        assert (
            ok(await rig.ask(ext, "call", "permissions.check", tool_name="Bash", func_name="bash"))
            == "deny"
        )
        assert backend.asked(RpcMethod.CHECK_PERMISSION) == [
            {"tool_name": "Bash", "func_name": "bash", "tool_args": {}}
        ]

    async def test_a_check_carries_the_arguments_of_the_call(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        backend = script(rig, {RpcMethod.CHECK_PERMISSION: "ask"})
        ok(
            await rig.ask(
                ext,
                "call",
                "permissions.check",
                tool_name="Bash",
                func_name="bash",
                tool_args={"command": "ls"},
            )
        )
        assert backend.asked(RpcMethod.CHECK_PERMISSION) == [
            {"tool_name": "Bash", "func_name": "bash", "tool_args": {"command": "ls"}}
        ]

    @pytest.mark.parametrize("word", ["ALLOW", "Ask", "", "maybe"])
    async def test_a_level_in_other_words_is_refused_by_name(self, rig: HostRig, word: str) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.CHECK_PERMISSION: word})
        message = refused(
            await rig.ask(ext, "call", "permissions.check", tool_name="Bash", func_name="bash"),
            "invalid",
        )
        assert repr(word) in message
        assert "allow, ask or deny" in message

    @pytest.mark.parametrize("word", ["ALLOW", "Deny", "perhaps"])
    async def test_a_rule_under_a_level_in_other_words_is_not_saved(
        self, rig: HostRig, word: str
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        message = refused(
            await rig.ask(ext, "call", "permissions.save_rule", rule="Bash(*)", level=word),
            "invalid",
        )
        assert repr(word) in message
        assert rig.session.permissions.rules == []

    @pytest.mark.parametrize("level", ["allow", "ask", "deny"])
    async def test_each_of_the_three_levels_is_saved(self, rig: HostRig, level: str) -> None:
        ext = rig.install(host=EVERYTHING)
        ok(await rig.ask(ext, "call", "permissions.save_rule", rule="Read(*)", level=level))
        assert rig.session.permissions.rules == [("Read(*)", level)]
