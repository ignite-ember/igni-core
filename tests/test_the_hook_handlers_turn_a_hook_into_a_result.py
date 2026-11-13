"""What each hook handler does with one hook, called directly.

The executor reaches the handlers through the registry; these tests call the
handlers (and the registry) themselves, with a real ``bash`` for the command
handler and a real ``httpx`` client over an in-process transport for the http
handler, so what is pinned is the wire behaviour and the result a hook yields:

* ``command`` — payload JSON on stdin, exit 0 envelope, exit 2 block, every
  other exit a logged fail-open, timeout and spawn failure fail open;
* ``http`` — the POST body and headers, 200 envelope, anything else fails open;
* ``mcp_tool`` — a resolver that raises or returns junk, the async timeout,
  arguments that collide with the event's own;
* ``prompt`` — the text, whatever the event;
* the registry — replace, supports, known types, unknown type, group gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from igni_core.core.hooks.group_hook_decisions import ForgetfulGroupHookDecisions
from igni_core.core.hooks.group_hook_gate import GroupHookGate
from igni_core.core.hooks.handlers.base import HookHandler
from igni_core.core.hooks.handlers.command import CommandHookHandler
from igni_core.core.hooks.handlers.http import HttpHookHandler
from igni_core.core.hooks.handlers.mcp_tool import McpInvocationArgs, McpToolHookHandler
from igni_core.core.hooks.handlers.prompt import PromptHookHandler
from igni_core.core.hooks.handlers.registry import HookHandlerRegistry
from igni_core.core.hooks.schemas import HookDefinition, HookPayload, HookResult, PermissionDecision
from igni_core.core.hooks.tool_events import PreToolUsePayload
from igni_core.core.network_policy import NetworkPolicy


def _payload(tool: str = "shell") -> HookPayload:
    return HookPayload(body=PreToolUsePayload(tool_name=tool))


def _command(script: str, **kw: Any) -> HookDefinition:
    return HookDefinition(type="command", command=script, timeout=kw.pop("timeout", 5000), **kw)


# ── the base class ───────────────────────────────────────────────────


class TestHookHandlerBase:
    def test_a_handler_without_run_cannot_be_built(self) -> None:
        class NoRun(HookHandler):
            handles = "prompt"

        with pytest.raises(TypeError):
            NoRun()  # type: ignore[abstract]

    def test_the_base_class_itself_cannot_be_built(self) -> None:
        with pytest.raises(TypeError):
            HookHandler()  # type: ignore[abstract]

    def test_non_blocking_continues_with_the_message_given_or_none(self) -> None:
        assert HookHandler._non_blocking() == HookResult(should_continue=True, message="")
        said = HookHandler._non_blocking("heads up")
        assert said.should_continue is True
        assert said.message == "heads up"


# ── command ──────────────────────────────────────────────────────────


class TestCommandHandler:
    async def test_the_payload_arrives_on_stdin_as_json(self, tmp_path: Path) -> None:
        out = tmp_path / "stdin.json"
        hook = _command(f"cat > {out}")
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload("naïve-tool"))
        assert result.should_continue is True
        assert json.loads(out.read_text(encoding="utf-8")) == _payload("naïve-tool").to_wire_dict()

    async def test_exit_zero_with_an_envelope_blocks_or_advises_as_it_says(self) -> None:
        hook = _command("""echo '{"continue": false, "systemMessage": "nope"}'""")
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "nope")

    async def test_exit_zero_with_a_permission_decision_carries_it(self) -> None:
        hook = _command("""echo '{"hookSpecificOutput": {"permissionDecision": "deny"}}'""")
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert result.permission_decision == PermissionDecision.DENY

    @pytest.mark.parametrize(
        "script",
        ["true", "echo not-json", "echo '[1, 2]'", "echo 'null'", "printf '\\xff\\xfe'"],
        ids=["empty", "text", "list", "null", "not-utf8"],
    )
    async def test_exit_zero_with_stdout_that_is_no_envelope_continues(self, script: str) -> None:
        result = await CommandHookHandler().run(_command(script), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    async def test_exit_two_blocks_with_the_envelope_message(self) -> None:
        hook = _command("""echo '{"systemMessage": "from json"}'; echo ignored >&2; exit 2""")
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "from json")

    async def test_exit_two_with_an_envelope_lacking_a_message_says_blocked(self) -> None:
        hook = _command("""echo '{"continue": false}'; echo stderr-text >&2; exit 2""")
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "Blocked by hook")

    async def test_exit_two_without_json_blocks_with_stderr(self) -> None:
        hook = _command("echo '  reason here  ' >&2; exit 2")
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "reason here")

    async def test_exit_two_with_nothing_said_blocks_as_blocked_by_hook(self) -> None:
        result = await CommandHookHandler().run(_command("exit 2"), "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "Blocked by hook")

    async def test_a_raising_rewake_callback_is_swallowed(self) -> None:
        def boom(_msg: str) -> None:
            raise RuntimeError("callback broke")

        hook = _command("echo wake >&2; exit 2", async_rewake=True)
        result = await CommandHookHandler(rewake_callback=boom).run(hook, "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    async def test_async_rewake_without_a_callback_blocks_as_usual(self) -> None:
        hook = _command("echo wake >&2; exit 2", async_rewake=True)
        result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "wake")

    async def test_a_soft_error_logs_the_code_and_stderr_but_never_returns_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        hook = _command("echo secret-diagnostic >&2; exit 7")
        with caplog.at_level(logging.WARNING):
            result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")
        (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
        text = record.getMessage()
        assert "exited 7" in text
        assert "secret-diagnostic" in text
        assert "exit 7" in text  # the command is named

    async def test_a_soft_error_with_no_stderr_says_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            await CommandHookHandler().run(_command("exit 3"), "PreToolUse", _payload())
        assert "stderr: <empty>" in caplog.text

    async def test_a_soft_error_logs_at_most_500_characters_of_stderr(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        hook = _command("head -c 800 /dev/zero | tr '\\0' 'x' >&2; exit 5")
        with caplog.at_level(logging.WARNING):
            await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert "x" * 500 in caplog.text
        assert "x" * 501 not in caplog.text

    async def test_a_command_slower_than_its_timeout_fails_open_and_says_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        hook = _command("sleep 5", timeout=100)
        with caplog.at_level(logging.WARNING):
            result = await CommandHookHandler().run(hook, "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="Hook timed out")
        assert "timed out after 0.1s" in caplog.text
        assert "sleep 5" in caplog.text

    async def test_a_hook_that_cannot_be_spawned_fails_open_and_says_so(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_spawn(*_a: Any, **_k: Any) -> None:
            raise FileNotFoundError("bash is gone")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
        with caplog.at_level(logging.WARNING):
            result = await CommandHookHandler().run(_command("true"), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")
        assert "could not be run" in caplog.text
        assert "bash is gone" in caplog.text


# ── http ─────────────────────────────────────────────────────────────


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Any, seen: dict[str, Any] | None = None
) -> None:
    """Route the handler's ``httpx.AsyncClient`` through an in-process transport."""
    real = httpx.AsyncClient

    def factory(**kw: Any) -> httpx.AsyncClient:
        if seen is not None:
            seen["timeout"] = kw.get("timeout")
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr("igni_core.core.hooks.handlers.http.httpx.AsyncClient", factory)


def _http(url: str = "https://hooks.example/h", **kw: Any) -> HookDefinition:
    return HookDefinition(type="http", url=url, **kw)


class TestHttpHandler:
    async def test_it_posts_the_payload_as_json_with_expanded_headers_and_the_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IGNI_TEST_HOOK_TOKEN", "s3cret")
        monkeypatch.delenv("IGNI_TEST_HOOK_UNSET", raising=False)
        seen: dict[str, Any] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            seen["unset"] = request.headers["x-unset"]
            return httpx.Response(200, json={})

        _serve(monkeypatch, respond, seen)
        hook = _http(
            headers={
                "Authorization": "Bearer $IGNI_TEST_HOOK_TOKEN",
                "X-Unset": "$IGNI_TEST_HOOK_UNSET",
            },
            timeout=2500,
        )
        await HttpHookHandler().run(hook, "PreToolUse", _payload("tool-x"))
        assert seen["method"] == "POST"
        assert seen["url"] == "https://hooks.example/h"
        assert seen["body"] == _payload("tool-x").to_wire_dict()
        assert seen["auth"] == "Bearer s3cret"
        # An unset variable is left as typed, not blanked.
        assert seen["unset"] == "$IGNI_TEST_HOOK_UNSET"
        assert seen["timeout"] == 2.5

    async def test_a_200_envelope_blocks_with_its_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(
            monkeypatch,
            lambda _r: httpx.Response(200, json={"continue": False, "systemMessage": "denied"}),
        )
        result = await HttpHookHandler().run(_http(), "PreToolUse", _payload())
        assert (result.should_continue, result.message) == (False, "denied")

    @pytest.mark.parametrize("status", [201, 204, 302, 403, 500])
    async def test_any_status_but_200_continues(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        _serve(
            monkeypatch,
            lambda _r: httpx.Response(status, json={"continue": False, "systemMessage": "x"}),
        )
        result = await HttpHookHandler().run(_http(), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    @pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b"null", b""])
    async def test_a_200_body_that_is_no_envelope_continues(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        _serve(monkeypatch, lambda _r: httpx.Response(200, content=body))
        result = await HttpHookHandler().run(_http(), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    async def test_a_network_failure_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _serve(monkeypatch, down)
        result = await HttpHookHandler().run(_http(), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    async def test_a_timeout_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        _serve(monkeypatch, slow)
        result = await HttpHookHandler().run(_http(), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    async def test_an_empty_url_continues(self) -> None:
        result = await HttpHookHandler().run(_http(url=""), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    async def test_a_refused_url_is_logged_by_name(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = HttpHookHandler(network_policy=NetworkPolicy(air_gapped=True))
        with caplog.at_level(logging.WARNING):
            result = await handler.run(
                _http("https://hooks.vendor.example/x"), "PreToolUse", _payload()
            )
        assert result == HookResult(should_continue=True, message="")
        assert "HTTP hook not sent" in caplog.text
        assert "hooks.vendor.example" in caplog.text


# ── mcp_tool ─────────────────────────────────────────────────────────


def _mcp(**kw: Any) -> HookDefinition:
    return HookDefinition(type="mcp_tool", mcp_server="srv", mcp_tool="tool", **kw)


class TestMcpToolHandler:
    async def test_a_resolver_that_raises_continues(self) -> None:
        def resolver(_s: str, _t: str) -> Any:
            raise RuntimeError("manager down")

        result = await McpToolHookHandler(resolver).run(_mcp(), "PreToolUse", _payload())
        assert result == HookResult(should_continue=True, message="")

    @pytest.mark.parametrize("returned", ["a string", 42, object()])
    async def test_a_resolver_that_returns_a_non_callable_continues(self, returned: Any) -> None:
        result = await McpToolHookHandler(lambda _s, _t: returned).run(
            _mcp(), "PreToolUse", _payload()
        )
        assert result == HookResult(should_continue=True, message="")

    async def test_the_resolver_is_asked_for_the_hooks_server_and_tool(self) -> None:
        asked: list[tuple[str, str]] = []

        def resolver(server: str, tool: str) -> Any:
            asked.append((server, tool))
            return None

        await McpToolHookHandler(resolver).run(_mcp(), "PreToolUse", _payload())
        assert asked == [("srv", "tool")]

    async def test_an_async_tool_slower_than_the_timeout_says_it_timed_out(self) -> None:
        async def slow(**_kw: Any) -> str:
            await asyncio.sleep(5)
            return "late"

        result = await McpToolHookHandler(lambda _s, _t: slow).run(
            _mcp(timeout=50), "PreToolUse", _payload()
        )
        assert result == HookResult(should_continue=True, message="MCP tool hook timed out")

    async def test_static_args_that_collide_with_the_event_fields_continue(self) -> None:
        called: list[dict[str, Any]] = []

        def tool(**kw: Any) -> str:
            called.append(kw)
            return "ran"

        result = await McpToolHookHandler(lambda _s, _t: tool).run(
            _mcp(mcp_args={"event": "mine"}), "PreToolUse", _payload()
        )
        assert result == HookResult(should_continue=True, message="")
        assert called == []

    @pytest.mark.parametrize(
        ("returned", "message"),
        [(["a", 1], "['a', 1]"), (7, "7"), ({"unrelated": 1}, "")],
    )
    async def test_a_return_that_is_not_an_envelope_is_shown_as_text(
        self, returned: Any, message: str
    ) -> None:
        result = await McpToolHookHandler(lambda _s, _t: lambda **_kw: returned).run(
            _mcp(), "PreToolUse", _payload()
        )
        assert result == HookResult(should_continue=True, message=message)

    def test_the_invocation_args_are_flat_and_default_to_an_empty_payload(self) -> None:
        assert McpInvocationArgs(event="Stop").to_kwargs() == {"event": "Stop", "payload": {}}
        args = McpInvocationArgs(event="Stop", payload={"a": 1}, extra="x")
        assert args.to_kwargs() == {"event": "Stop", "payload": {"a": 1}, "extra": "x"}


# ── prompt ───────────────────────────────────────────────────────────


class TestPromptHandler:
    @pytest.mark.parametrize("text", ["", "line one\nline two", "naïve ✓"])
    async def test_it_returns_its_text_whatever_the_event(self, text: str) -> None:
        hook = HookDefinition(type="prompt", text=text)
        for event in ("PreToolUse", "Stop"):
            result = await PromptHookHandler().run(hook, event, _payload())
            assert result == HookResult(should_continue=True, message=text)


# ── registry ─────────────────────────────────────────────────────────


class _Recording(HookHandler):
    handles = "prompt"

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, hook: HookDefinition, event: str, payload: HookPayload) -> HookResult:
        self.ran.append(event)
        return HookResult(should_continue=False, message="replaced")


class TestHandlerRegistry:
    def test_it_serves_the_four_types_and_nothing_else(self) -> None:
        registry = HookHandlerRegistry()
        assert sorted(registry.known_types()) == ["command", "http", "mcp_tool", "prompt"]
        assert registry.supports("command") is True
        assert registry.supports("agent") is False
        assert registry.supports("") is False

    async def test_register_replaces_the_handler_for_its_type(self) -> None:
        registry = HookHandlerRegistry()
        fake = _Recording()
        registry.register(fake)
        assert sorted(registry.known_types()) == ["command", "http", "mcp_tool", "prompt"]
        result = await registry.run(HookDefinition(type="prompt", text="orig"), "Stop", _payload())
        assert result == HookResult(should_continue=False, message="replaced")
        assert fake.ran == ["Stop"]

    async def test_an_unknown_type_is_skipped(self) -> None:
        result = await HookHandlerRegistry().run(HookDefinition(type="agent"), "Stop", _payload())
        assert result is None

    async def test_the_constructor_arguments_reach_their_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        woken: list[str] = []
        asked: list[tuple[str, str]] = []

        def resolver(server: str, tool: str) -> Any:
            asked.append((server, tool))
            return lambda **_kw: "from tool"

        registry = HookHandlerRegistry(
            mcp_resolver=resolver,
            rewake_callback=woken.append,
            network_policy=NetworkPolicy(air_gapped=True),
        )
        rewake = await registry.run(
            _command("echo wake >&2; exit 2", async_rewake=True), "Stop", _payload()
        )
        assert rewake == HookResult(should_continue=True, message="")
        assert woken == ["wake"]

        mcp = await registry.run(_mcp(), "Stop", _payload())
        assert mcp is not None
        assert mcp.message == "from tool"
        assert asked == [("srv", "tool")]

        _serve(monkeypatch, lambda _r: pytest.fail("a gapped install sent the hook"))
        http = await registry.run(_http("https://hooks.vendor.example/x"), "Stop", _payload())
        assert http == HookResult(should_continue=True, message="")

    async def test_a_registry_without_a_gate_runs_no_group_hook_but_runs_the_rest(self) -> None:
        registry = HookHandlerRegistry()
        own = HookDefinition(type="prompt", text="mine")
        group = HookDefinition(type="prompt", text="theirs").mark_from_group("/g/hooks.json")
        assert await registry.run(group, "Stop", _payload()) is None
        assert await registry.run(own, "Stop", _payload()) == HookResult(
            should_continue=True, message="mine"
        )

    async def test_a_gate_that_permits_lets_the_group_hook_through(self) -> None:
        registry = HookHandlerRegistry(
            group_gate=GroupHookGate(mode="run", decisions=ForgetfulGroupHookDecisions())
        )
        group = HookDefinition(type="prompt", text="theirs").mark_from_group("/g/hooks.json")
        result = await registry.run(group, "Stop", _payload())
        assert result == HookResult(should_continue=True, message="theirs")
