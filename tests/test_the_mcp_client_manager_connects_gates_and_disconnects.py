"""What :class:`MCPClientManager` does to connect a server, refuse it and let it go.

The manager is real; only the transport is replaced: a stdio server arrives
through the :class:`StdioMCPBinding` the manager is given (the seam its own
constructor offers) and an SSE one through the SDK class it imports. The
integration file next door runs the same manager against a real server; this
one reaches the gate orders, failure wording and cleanup paths a real server
cannot be made to take on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.mcp import client as client_mod
from igni_core.core.mcp.approval import MCPApprovalManager
from igni_core.core.mcp.client import MCPClientManager, _no_tools_reason, _with_stderr
from igni_core.core.mcp.config import MCPPolicy, MCPServerConfig, MCPTransport
from igni_core.core.mcp.naming import tool_name_prefix
from igni_core.core.network_policy import NetworkPolicy


class _Entry:
    def __init__(self, name: str) -> None:
        async def call(**kwargs: Any) -> str:
            return name

        self.name = name
        self.entrypoint = call


class _Client:
    """What Agno's ``MCPTools`` is to the manager: ``functions``, ``session``, ``__aexit__``."""

    def __init__(
        self,
        tools: tuple[str, ...] = ("a", "b"),
        *,
        session: Any = None,
        exit_raises: BaseException | None = None,
    ) -> None:
        self.functions = {t: _Entry(t) for t in tools}
        self.session = session
        self.exited = 0
        self._exit_raises = exit_raises

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1
        if self._exit_raises is not None:
            raise self._exit_raises


class _Binding:
    def __init__(self, client: _Client | Exception | None = None, tail: Any = "") -> None:
        self.client = client if client is not None else _Client()
        self.tail = tail
        self.opened: list[dict[str, Any]] = []

    async def open(self, name: str, config: MCPServerConfig, **kwargs: Any) -> Any:
        self.opened.append({"name": name, "config": config, **kwargs})
        if isinstance(self.client, Exception):
            raise self.client
        return self.client

    def stderr_tail(self, name: str) -> Any:
        if isinstance(self.tail, Exception):
            raise self.tail
        return self.tail


def _manager(tmp_path: Path, binding: _Binding | None = None, **kw: Any) -> MCPClientManager:
    kw.setdefault("policy", MCPPolicy())
    return MCPClientManager(
        project_dir=tmp_path,
        stdio_binding=binding or _Binding(),
        **kw,  # type: ignore[arg-type]
    )


def _stdio(name: str = "srv", **kw: Any) -> MCPServerConfig:
    return MCPServerConfig(name=name, command="x", source_path="/p/.mcp.json", **kw)


@pytest.fixture(autouse=True)
def _nobody_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MCPApprovalManager, "_can_prompt", staticmethod(lambda: False))


# -- the words a failure is reported in -----------------------------------


class TestFailureWording:
    def test_stderr_is_appended_to_a_reason_after_its_full_stop(self) -> None:
        assert _with_stderr("it broke.", "boom") == ("it broke. Its process said on stderr: boom")
        assert _with_stderr("it broke", "boom") == "it broke. Its process said on stderr: boom"

    def test_a_reason_is_unchanged_when_the_process_said_nothing(self) -> None:
        assert _with_stderr("it broke.", "") == "it broke."

    def test_an_extension_with_stderr_gets_its_last_lines(self) -> None:
        assert _no_tools_reason("ext:acme.x", stderr_tail="config missing") == (
            "its MCP server returned no tools, or its process exited first. "
            "Its last lines on stderr: config missing"
        )

    def test_an_extension_without_stderr_is_told_to_run_the_command_by_hand(self) -> None:
        assert _no_tools_reason("ext:acme.x", stderr_tail="") == (
            "its MCP server returned no tools, or its process exited first. "
            "It wrote nothing to stderr; run its command by hand to see what it does."
        )

    def test_any_other_server_gets_the_ide_hint_and_the_stderr(self) -> None:
        assert _no_tools_reason("ide", stderr_tail="") == (
            "MCP server connected but returned no tools. Ensure the IDE has MCP support enabled."
        )
        assert _no_tools_reason("ide", stderr_tail="died") == (
            "MCP server connected but returned no tools. Ensure the IDE has MCP support "
            "enabled. Its process said on stderr: died"
        )

    def test_only_the_ext_prefix_makes_a_name_an_extension(self) -> None:
        assert "IDE" in _no_tools_reason("plugin:ext:x", stderr_tail="")
        assert "IDE" in _no_tools_reason("extension", stderr_tail="")


# -- construction ---------------------------------------------------------


class TestConstruction:
    def test_the_group_directory_reaches_the_loader_the_manager_reads(self, tmp_path: Path) -> None:
        group = tmp_path / "group"
        group.mkdir()
        (group / "org.json").write_text(
            json.dumps({"mcpServers": {"org-tool": {"command": "org"}}})
        )

        mcp = _manager(tmp_path / "proj", group_mcps_dir=group)

        assert mcp.list_servers() == ["org-tool"]
        assert mcp.configs["org-tool"].source == "group-policy"

    def test_without_a_group_directory_nothing_of_a_group_is_read(self, tmp_path: Path) -> None:
        assert _manager(tmp_path / "proj").list_servers() == []

    def test_a_registered_server_connects_by_its_name(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path)
        mcp.register_server(_stdio("mine", source="extension"))

        assert "mine" in mcp.list_servers()
        assert asyncio.run(mcp.connect("mine")).ok

    def test_the_managed_policy_is_read_when_none_is_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from igni_core.core.mcp import config as mcp_config

        managed = tmp_path / "managed"
        managed.mkdir()
        (managed / "managed-settings.json").write_text(
            json.dumps({"mcp": {"denied": ["bad*"], "required": ["must"]}})
        )
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: managed)

        mcp = MCPClientManager(project_dir=tmp_path, stdio_binding=_Binding())  # type: ignore[arg-type]

        assert mcp.is_policy_denied("bad-one") is True
        assert mcp.is_policy_denied("good") is False
        assert mcp.list_required() == ["must"]


# -- the gate -------------------------------------------------------------


class TestTheGate:
    async def test_an_unknown_name_has_no_config(self, tmp_path: Path) -> None:
        result = await _manager(tmp_path).connect("nope")

        assert (result.ok, result.reason) == (False, "No config found")

    async def test_a_denied_and_a_not_allowed_server_say_which(self, tmp_path: Path) -> None:
        binding = _Binding()
        mcp = _manager(
            tmp_path, binding, policy=MCPPolicy(denied=["bad*"], allowed=["good", "bad-1"])
        )
        for n in ("bad-1", "other", "good"):
            mcp.register_server(_stdio(n, source="extension"))

        assert (await mcp.connect("bad-1")).reason == "Server 'bad-1' is blocked by admin policy"
        assert (await mcp.connect("other")).reason == "Server 'other' is not in the allowed list"
        assert (await mcp.connect("good")).ok
        assert [o["name"] for o in binding.opened] == ["good"]

    async def test_policy_still_blocks_an_extension_server(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path, policy=MCPPolicy(denied=["ext:*"]))
        mcp.register_server(_stdio("ext:acme.x", source="extension"))

        assert not (await mcp.connect("ext:acme.x")).ok

    async def test_an_unapproved_server_is_refused_without_a_terminal_and_never_opened(
        self, tmp_path: Path
    ) -> None:
        binding = _Binding()
        mcp = _manager(tmp_path, binding)
        mcp.register_server(_stdio("proj"))

        result = await mcp.connect("proj")

        assert (result.ok, result.reason) == (False, "User denied MCP server connection")
        assert binding.opened == []

    async def test_an_extension_server_connects_with_no_terminal_and_no_approval(
        self, tmp_path: Path
    ) -> None:
        binding = _Binding()
        mcp = _manager(tmp_path, binding)
        mcp.register_server(_stdio("ext:acme.x", source="extension"))

        assert (await mcp.connect("ext:acme.x")).ok
        assert not (tmp_path / "approved").exists()

    async def test_the_air_gap_is_checked_before_anyone_is_asked_to_approve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []
        monkeypatch.setattr(
            MCPApprovalManager,
            "check_approval",
            lambda self, name, path: asked.append(name) or True,
        )
        mcp = _manager(tmp_path, network_policy=NetworkPolicy(air_gapped=True))
        mcp.register_server(
            MCPServerConfig(name="r", type=MCPTransport.sse, url="https://out.example/sse")
        )

        result = await mcp.connect("r")

        assert "out.example" in result.reason
        assert "MCP server 'r' was not contacted" in result.reason
        assert asked == []

    async def test_a_stdio_server_is_never_checked_against_the_network(
        self, tmp_path: Path
    ) -> None:
        mcp = _manager(tmp_path, network_policy=NetworkPolicy(air_gapped=True))
        mcp.register_server(_stdio("local", source="extension", url="https://out.example"))

        assert (await mcp.connect("local")).ok

    async def test_an_internal_url_passes_the_air_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sse = _SseRecorder()
        monkeypatch.setattr(client_mod, "_MCPTools", sse)
        mcp = _manager(
            tmp_path,
            network_policy=NetworkPolicy(air_gapped=True, internal_hosts=["mcp.corp.example"]),
        )
        mcp.register_server(
            MCPServerConfig(
                name="r",
                type=MCPTransport.sse,
                url="https://mcp.corp.example/sse",
                source="extension",
            )
        )

        assert (await mcp.connect("r")).ok

    async def test_missing_sdk_is_reported_after_the_other_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_mod, "_MCPTools", None)
        mcp = _manager(tmp_path)
        mcp.register_server(_stdio("s", source="extension"))

        assert (await mcp.connect("s")).reason == (
            "MCP dependencies not installed (pip install agno[mcp])"
        )

    async def test_a_live_connection_is_returned_without_asking_the_gate_again(
        self, tmp_path: Path
    ) -> None:
        binding = _Binding()
        mcp = _manager(tmp_path, binding)
        mcp.register_server(_stdio("s", source="extension"))
        first = await mcp.connect("s")
        mcp._policy = MCPPolicy(denied=["*"])

        second = await mcp.connect("s")

        assert second.ok and second.client is first.client
        assert len(binding.opened) == 1


# -- opening a transport --------------------------------------------------


class _SseRecorder:
    """Stands in for ``agno.tools.mcp.MCPTools`` when an SSE server is opened."""

    instances: list[_SseRecorder]

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.functions = {"x": _Entry("x")}
        self.session = None
        self.entered = 0
        self.instances.append(self)

    async def __aenter__(self) -> _SseRecorder:
        self.entered += 1
        return self

    def __call__(self, **kwargs: Any) -> _SseRecorder:
        return _SseRecorder(**kwargs)


@pytest.fixture(autouse=True)
def _sse_instances() -> None:
    _SseRecorder.instances = []


class TestOpeningATransport:
    async def test_an_sse_server_gets_the_same_tool_prefix_a_stdio_one_does(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_mod, "_MCPTools", _SseRecorder)
        mcp = _manager(tmp_path)
        mcp.register_server(
            MCPServerConfig(
                name="remote",
                type=MCPTransport.sse,
                url="https://x.example/sse",
                source="extension",
            )
        )

        assert (await mcp.connect("remote")).ok

        (opened,) = _SseRecorder.instances
        assert opened.kwargs == {
            "url": "https://x.example/sse",
            "transport": "sse",
            "tool_name_prefix": tool_name_prefix("remote"),
        }
        assert opened.kwargs["tool_name_prefix"] == "mcp_remote"
        assert opened.entered == 1

    async def test_an_sse_type_given_as_plain_text_is_still_sse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_mod, "_MCPTools", _SseRecorder)
        mcp = _manager(tmp_path)
        cfg = MCPServerConfig.model_construct(
            name="r",
            type="sse",
            url="https://x.example/sse",
            source="extension",
            command="",
            args=[],
            env={},
            cwd="",
            source_path="",
        )
        mcp.register_server(cfg)

        assert (await mcp.connect("r")).ok
        assert _SseRecorder.instances[0].kwargs["transport"] == "sse"

    async def test_an_unsupported_type_is_a_failure_naming_it(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path)
        mcp.register_server(
            MCPServerConfig.model_construct(
                name="w",
                type="websocket",
                url="",
                command="",
                args=[],
                env={},
                cwd="",
                source_path="",
                source="extension",
            )
        )

        assert (await mcp.connect("w")).reason == "Unsupported MCP type: websocket"

    async def test_the_extension_request_meta_goes_to_an_extension_only(
        self, tmp_path: Path
    ) -> None:
        binding = _Binding()
        mcp = _manager(tmp_path, binding)
        sentinel = object()
        mcp.extension_request_meta = sentinel  # type: ignore[assignment]
        mcp.register_server(_stdio("ext:acme.x", source="extension"))
        mcp.register_server(_stdio("plain", source="project"))
        mcp._approval.approve("plain", "/p/.mcp.json")

        await mcp.connect("ext:acme.x")
        await mcp.connect("plain")

        by_name = {o["name"]: o for o in binding.opened}
        assert by_name["ext:acme.x"]["request_meta"] is sentinel
        assert by_name["plain"]["request_meta"] is None
        assert by_name["plain"]["notifications"] is mcp.notifications

    async def test_a_server_that_fails_to_open_says_why_with_its_stderr(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        binding = _Binding(RuntimeError("spawn failed"), tail="no such file: x")
        mcp = _manager(tmp_path, binding)
        mcp.register_server(_stdio("s", source="extension"))

        with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.client"):
            result = await mcp.connect("s")

        assert result.reason == "spawn failed. Its process said on stderr: no such file: x"
        assert "MCP connect 's' failed: spawn failed" in caplog.text
        assert mcp.list_connected() == []

    @pytest.mark.parametrize("tail", ["", None, 42, RuntimeError("log unreadable")])
    async def test_a_tail_that_is_empty_missing_or_unreadable_changes_nothing(
        self, tmp_path: Path, tail: Any
    ) -> None:
        mcp = _manager(tmp_path, _Binding(RuntimeError("spawn failed"), tail=tail))
        mcp.register_server(_stdio("s", source="extension"))

        assert (await mcp.connect("s")).reason == "spawn failed"

    async def test_a_binding_without_a_tail_method_changes_nothing(self, tmp_path: Path) -> None:
        class Bare:
            async def open(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("spawn failed")

        mcp = _manager(tmp_path, Bare())  # type: ignore[arg-type]
        mcp.register_server(_stdio("s", source="extension"))

        assert (await mcp.connect("s")).reason == "spawn failed"

    async def test_an_sse_failure_never_borrows_a_stdio_servers_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Down:
            def __init__(self, **kw: Any) -> None: ...

            async def __aenter__(self) -> None:
                raise ConnectionError("refused")

        monkeypatch.setattr(client_mod, "_MCPTools", Down)
        mcp = _manager(tmp_path, _Binding(tail="stdio noise"))
        mcp.register_server(
            MCPServerConfig(
                name="r", type=MCPTransport.sse, url="https://x.example/sse", source="extension"
            )
        )

        assert (await mcp.connect("r")).reason == "refused"

    async def test_a_server_with_no_tools_is_closed_and_the_reason_is_the_stderr(
        self, tmp_path: Path
    ) -> None:
        empty = _Client(())
        mcp = _manager(tmp_path, _Binding(empty, tail="config missing"))
        mcp.register_server(_stdio("ext:acme.x", source="extension"))

        result = await mcp.connect("ext:acme.x")

        assert not result.ok
        assert "Its last lines on stderr: config missing" in result.reason
        assert empty.exited == 1
        assert mcp.list_connected() == []

    async def test_a_close_that_fails_after_no_tools_does_not_hide_the_reason(
        self, tmp_path: Path
    ) -> None:
        empty = _Client((), exit_raises=RuntimeError("close failed"))
        mcp = _manager(tmp_path, _Binding(empty))
        mcp.register_server(_stdio("ide", source="extension"))

        result = await mcp.connect("ide")

        assert result.reason == (
            "MCP server connected but returned no tools. Ensure the IDE has MCP support enabled."
        )

    async def test_a_connection_is_cached_snapshotted_and_filtered(self, tmp_path: Path) -> None:
        live = _Client(("a", "b"))
        mcp = _manager(tmp_path, _Binding(live))
        mcp.register_server(_stdio("s", source="extension"))
        mcp._tool_filter._disabled_tools["s"] = {"b"}

        result = await mcp.connect("s")

        assert result.client is live
        assert mcp.get_client("s") is live
        assert list(live.functions) == ["a"]
        assert mcp.get_tools("s") == ["a", "b"]
        assert mcp.get_disabled_tools("s") == ["b"]


# -- letting go -----------------------------------------------------------


class TestDisconnecting:
    async def _connected(
        self, tmp_path: Path, **clients: _Client
    ) -> tuple[MCPClientManager, dict[str, _Client]]:
        mcp = _manager(tmp_path)
        for name, c in clients.items():
            mcp._clients[name] = c  # type: ignore[assignment]
            mcp.configs[name] = _stdio(name, source="extension")
            mcp._tool_filter.snapshot(name, c)  # type: ignore[arg-type]
        return mcp, clients

    async def test_disconnect_all_closes_stdio_and_abandons_sse(self, tmp_path: Path) -> None:
        stdio, sse = _Client(), _Client()
        mcp, _ = await self._connected(tmp_path, a=stdio, b=sse)
        mcp.configs["b"] = MCPServerConfig(name="b", type=MCPTransport.sse, url="u")

        await mcp.disconnect_all()

        assert (stdio.exited, sse.exited) == (1, 0)
        assert mcp.list_connected() == []

    async def test_a_sse_type_held_as_plain_text_is_abandoned_too(self, tmp_path: Path) -> None:
        sse = _Client()
        mcp, _ = await self._connected(tmp_path, b=sse)
        mcp.configs["b"] = MCPServerConfig.model_construct(name="b", type="sse")

        await mcp.disconnect_all()

        assert sse.exited == 0

    async def test_a_close_that_raises_even_a_cancellation_does_not_stop_the_rest(
        self, tmp_path: Path
    ) -> None:
        first = _Client(exit_raises=asyncio.CancelledError())
        second = _Client(exit_raises=RuntimeError("x"))
        third = _Client()
        mcp, _ = await self._connected(tmp_path, a=first, b=second, c=third)

        await mcp.disconnect_all()

        assert (first.exited, second.exited, third.exited) == (1, 1, 1)
        assert mcp.list_connected() == []

    async def test_disconnect_all_with_nothing_connected_is_a_no_op(self, tmp_path: Path) -> None:
        await _manager(tmp_path).disconnect_all()

    async def test_a_disconnected_server_lists_no_tools(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path, _Binding(_Client(("a", "b"))))
        mcp.register_server(_stdio("s", source="extension"))
        mcp.register_server(_stdio("t", source="extension"))
        await mcp.connect("s")
        await mcp.connect("t")
        assert mcp.get_tools("s") == ["a", "b"]

        await mcp.disconnect_all()

        # The connect-time snapshot went with the connection, not kept to show stale rows.
        assert mcp.get_tools("s") == []
        assert mcp.get_tools("t") == []

    async def test_disconnect_one_of_an_unknown_server_is_false(self, tmp_path: Path) -> None:
        assert await _manager(tmp_path).disconnect_one("nope") is False

    async def test_disconnect_one_closes_a_stdio_server_and_forgets_it(
        self, tmp_path: Path
    ) -> None:
        a, b = _Client(("a", "b")), _Client(("a", "b"))
        mcp, _ = await self._connected(tmp_path, a=a, b=b)

        assert await mcp.disconnect_one("a") is True

        assert (a.exited, b.exited) == (1, 0)
        assert mcp.list_connected() == ["b"]
        assert mcp.get_tools("a") == []
        assert mcp.get_tools("b") == ["a", "b"]

    async def test_disconnect_one_returns_true_even_when_the_close_raises(
        self, tmp_path: Path
    ) -> None:
        a = _Client(exit_raises=asyncio.CancelledError())
        mcp, _ = await self._connected(tmp_path, a=a)

        assert await mcp.disconnect_one("a") is True
        assert mcp.list_connected() == []

    async def test_disconnect_one_abandons_a_sse_connection(self, tmp_path: Path) -> None:
        sse = _Client()
        mcp, _ = await self._connected(tmp_path, b=sse)
        mcp.configs["b"] = MCPServerConfig(name="b", type=MCPTransport.sse, url="u")

        assert await mcp.disconnect_one("b") is True
        assert sse.exited == 0
        assert mcp.list_connected() == []


# -- asking a live server -------------------------------------------------


class _Session:
    def __init__(self, **answers: Any) -> None:
        self.answers = answers

    def _answer(self, key: str) -> Any:
        value = self.answers[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def list_tools(self) -> Any:
        return self._answer("tools")

    async def list_resources(self) -> Any:
        return self._answer("resources")

    async def list_prompts(self) -> Any:
        return self._answer("prompts")


class _Obj:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class TestAskingALiveServer:
    def _with_session(self, tmp_path: Path, session: Any) -> MCPClientManager:
        mcp = _manager(tmp_path)
        mcp._clients["s"] = _Client(session=session)  # type: ignore[assignment]
        return mcp

    async def test_nothing_is_said_by_a_server_that_is_not_connected(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path)

        assert await mcp.tool_annotations("s") == {}
        assert await mcp.get_resources("s") == []
        assert await mcp.get_prompts("s") == []

    async def test_nothing_is_said_by_a_connection_without_a_session(self, tmp_path: Path) -> None:
        mcp = self._with_session(tmp_path, None)

        assert await mcp.tool_annotations("s") == {}
        assert await mcp.get_resources("s") == []
        assert await mcp.get_prompts("s") == []

    async def test_a_server_that_raises_says_nothing(self, tmp_path: Path) -> None:
        boom = RuntimeError("Method not found")
        mcp = self._with_session(tmp_path, _Session(tools=boom, resources=boom, prompts=boom))

        assert await mcp.tool_annotations("s") == {}
        assert await mcp.get_resources("s") == []
        assert await mcp.get_prompts("s") == []

    async def test_annotations_are_keyed_by_the_servers_own_tool_names(
        self, tmp_path: Path
    ) -> None:
        ann = {"readOnlyHint": True}
        listed = _Obj(tools=[_Obj(name="read", annotations=ann), _Obj(name="w", annotations=None)])
        mcp = self._with_session(tmp_path, _Session(tools=listed))

        assert await mcp.tool_annotations("s") == {"read": ann, "w": None}

    async def test_a_bare_list_answer_is_read_like_a_result_object(self, tmp_path: Path) -> None:
        tools = [_Obj(name="read", annotations="a")]
        resources = [_Obj(uri="file:///a", name="a", description="d", mimeType="text/plain")]
        prompts = [_Obj(name="p", description="d", arguments=[])]
        mcp = self._with_session(
            tmp_path, _Session(tools=tools, resources=resources, prompts=prompts)
        )

        assert await mcp.tool_annotations("s") == {"read": "a"}
        assert [r.uri for r in await mcp.get_resources("s")] == ["file:///a"]
        assert [p.name for p in await mcp.get_prompts("s")] == ["p"]

    async def test_a_result_object_with_nothing_in_it_is_an_empty_list(
        self, tmp_path: Path
    ) -> None:
        mcp = self._with_session(
            tmp_path,
            _Session(tools=_Obj(tools=None), resources=_Obj(), prompts=_Obj(prompts=[])),
        )

        assert await mcp.tool_annotations("s") == {}
        assert await mcp.get_resources("s") == []
        assert await mcp.get_prompts("s") == []


# -- enumeration ----------------------------------------------------------


class TestEnumeration:
    async def test_all_clients_is_a_copy(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path)
        live = _Client()
        mcp._clients["s"] = live  # type: ignore[assignment]

        copy = mcp.all_clients()
        copy.clear()

        assert mcp.all_clients() == {"s": live}

    async def test_a_required_server_stays_listed_until_it_is_connected(
        self, tmp_path: Path
    ) -> None:
        mcp = _manager(tmp_path, policy=MCPPolicy(required=["a", "b"]))
        mcp._clients["a"] = _Client()  # type: ignore[assignment]

        assert mcp.list_required() == ["b"]
        assert mcp.list_connected() == ["a"]

    async def test_the_policys_denial_is_what_is_policy_denied_says(self, tmp_path: Path) -> None:
        mcp = _manager(tmp_path, policy=MCPPolicy(denied=["x-*"]))

        assert mcp.is_policy_denied("x-1") is True
        assert mcp.is_policy_denied("y") is False
