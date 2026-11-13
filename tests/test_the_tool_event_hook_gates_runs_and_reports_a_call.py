"""ToolEventHook as Agno sees it: what a call returns, what the extension gate is told.

The facade asks extensions' PreToolUse hooks first, runs the permission pipeline, runs
the tool, tells the gate whether an MCP server answered, then lets extensions' PostToolUse
hooks have the last word on the text the model reads. The gate here is a stand-in for the
host's answerer; everything else is the real hook, pipeline and invoker.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

import pytest
from agno.tools.function import ToolResult

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.extension_gate import ExtensionHookVerdict, extension_hook_gate
from igni_core.core.hooks.extension_values import PostToolUseValue, PreToolUseValue
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.hooks.untrusted import wrap_untrusted
from igni_core.core.observability.runtime import OtelRuntime


class _Gate:
    """The extensions' answerer: hooks the events it is given, answers by ``answer``."""

    def __init__(
        self,
        answer: Callable[[Any], ExtensionHookVerdict[Any]] | None = None,
        hooked: tuple[HookEvent, ...] = (HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE),
    ) -> None:
        self.answer = answer or (lambda v: ExtensionHookVerdict(allowed=True, value=v))
        self.hooked = hooked
        self.asked: list[Any] = []
        self.called: list[tuple[str, bool, str]] = []
        extension_hook_gate.install(self)

    def hooks(self, event: HookEvent) -> bool:
        return event in self.hooked

    async def ask(self, value: Any) -> ExtensionHookVerdict[Any]:
        self.asked.append(value)
        return self.answer(value)

    def tell(self, event: HookEvent, payload: Any) -> None:
        return None

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        self.called.append((tool_name, answered, detail))


def _change(**update: Any) -> Callable[[Any], ExtensionHookVerdict[Any]]:
    return lambda v: ExtensionHookVerdict(allowed=True, value=v.model_copy(update=update))


def _refuse(reason: str, only: type) -> Callable[[Any], ExtensionHookVerdict[Any]]:
    return lambda v: ExtensionHookVerdict(allowed=not isinstance(v, only), value=v, reason=reason)


def _hook(**kw: Any) -> ToolEventHook:
    return ToolEventHook(HookExecutor({}), session_id="s-1", **kw)


# ── What Agno sees ────────────────────────────────────────────────────


def test_the_hook_is_a_coroutine_function_to_agno_and_absorbs_extra_keywords():
    hook = _hook()

    assert inspect.iscoroutinefunction(hook)
    assert "kwargs" in inspect.signature(hook.__call__).parameters


async def test_a_call_with_no_arguments_runs_with_none():
    seen: list[dict[str, Any]] = []

    def tool(**kw: Any) -> str:
        seen.append(kw)
        return "ok"

    assert await _hook()("probe", tool, None, agent=object(), team=None, extra=1) == "ok"
    assert seen == [{}]


async def test_a_call_with_no_function_returns_nothing():
    assert await _hook()("probe", None, {"a": 1}) is None


async def test_the_decision_is_noted_for_the_tool_calls_span(monkeypatch):
    noted: list[str] = []
    monkeypatch.setattr(OtelRuntime, "note_permission", classmethod(lambda c, d: noted.append(d)))

    await _hook()("probe", lambda **kw: "ok", {})
    _Gate(_refuse("no", PreToolUseValue))
    await _hook()("probe", lambda **kw: "ok", {})

    assert noted == ["allow", "deny"]


# ── The extensions' PreToolUse ────────────────────────────────────────


async def test_a_refusal_is_returned_as_blocked_with_its_reason_and_the_tool_does_not_run():
    _Gate(_refuse("Not now.", PreToolUseValue))
    ran: list[int] = []

    result = await _hook()("probe", lambda **kw: ran.append(1), {})

    assert result == "Blocked: Not now." and ran == []


async def test_an_empty_refusal_reason_is_still_a_refusal():
    _Gate(_refuse("", PreToolUseValue))

    assert await _hook()("probe", lambda **kw: "ran", {}) == "Blocked: "


async def test_the_pipeline_judges_the_arguments_the_extension_left():
    """An extension rewrites a harmless path to a protected one: the safety list sees the rewrite."""
    _Gate(_change(tool_args={"file_path": "prod/.env"}))
    ran: list[int] = []

    def tool(**kw: Any) -> str:
        ran.append(1)
        return "written"

    result = await _hook(protected_paths=[".env"])("save_file", tool, {"file_path": "notes.txt"})

    assert ran == [] and "Blocked" in result


async def test_the_asked_value_is_the_call_as_made():
    gate = _Gate()

    await _hook()("probe", lambda **kw: "ok", {"x": 1})

    assert gate.asked[0] == PreToolUseValue(session_id="s-1", tool_name="probe", tool_args={"x": 1})


# ── Whether an MCP server answered ────────────────────────────────────


async def test_an_ordinary_result_is_an_answer_with_no_detail():
    gate = _Gate()

    await _hook()("mcp_ext:acme.guard_scan", lambda **kw: ToolResult(content="rows"), {})

    assert gate.called == [("mcp_ext:acme.guard_scan", True, "")]


async def test_an_agno_error_result_is_no_answer_and_its_detail_is_what_follows_the_prefix():
    """Agno turns a timeout into ``Error: …``; by now the result sits inside the untrusted block."""
    gate = _Gate()
    timed_out = ToolResult(content="Error: Timed out while waiting for response")

    await _hook()("mcp_ext:acme.guard_scan", lambda **kw: timed_out, {})

    assert gate.called == [
        ("mcp_ext:acme.guard_scan", False, "Timed out while waiting for response")
    ]


async def test_an_error_the_tool_reported_itself_is_an_answer():
    gate = _Gate()

    await _hook()(
        "mcp_ext:acme.guard_scan",
        lambda **kw: ToolResult(content="Error from MCP tool 'scan': no such file"),
        {},
    )

    assert gate.called[0][1] is True


async def test_an_unwrapped_error_result_is_read_the_same_way():
    gate = _Gate()

    await _hook()("probe", lambda **kw: ToolResult(content="Error: closed"), {})

    assert gate.called == [("probe", False, "closed")]


async def test_a_plain_text_result_is_never_a_missing_answer():
    gate = _Gate()

    await _hook()("probe", lambda **kw: "Error: plain text", {})

    assert gate.called[0][1] is True


async def test_a_raised_error_is_reported_with_whether_the_tool_answered_and_propagates():
    gate = _Gate()

    class Failed(Exception):
        answered = True

    def answered(**kw: Any) -> None:
        raise Failed("no such file")

    def silent(**kw: Any) -> None:
        raise RuntimeError("closed")

    with pytest.raises(Failed):
        await _hook()("a", answered, {})
    with pytest.raises(RuntimeError):
        await _hook()("b", silent, {})

    assert gate.called == [("a", True, "no such file"), ("b", False, "closed")]


async def test_a_call_that_was_blocked_is_not_reported_as_called():
    gate = _Gate(_refuse("no", PreToolUseValue))

    await _hook()("probe", lambda **kw: "ok", {})

    assert gate.called == []


# ── The extensions' PostToolUse ───────────────────────────────────────


async def test_with_no_post_hook_subscribed_the_result_is_not_offered():
    gate = _Gate(hooked=(HookEvent.PRE_TOOL_USE,))

    result = await _hook()("probe", lambda **kw: {"rows": 1}, {})

    assert result == {"rows": 1}
    assert [type(v) for v in gate.asked] == [PreToolUseValue]


async def test_a_reject_withholds_the_result_and_says_why():
    _Gate(_refuse("It holds a secret.", PostToolUseValue))

    assert await _hook()("probe", lambda **kw: "token=1", {}) == "Withheld: It holds a secret."


async def test_a_text_result_is_offered_as_text_and_an_unchanged_one_is_the_same_object():
    gate = _Gate()
    text = "".join(["to", "ken"])

    result = await _hook()("probe", lambda **kw: text, {"a": 1})

    assert result is text
    posted = [v for v in gate.asked if isinstance(v, PostToolUseValue)]
    assert posted == [
        PostToolUseValue(session_id="s-1", tool_name="probe", tool_args={"a": 1}, result="token")
    ]


async def test_a_changed_mcp_text_is_put_in_a_copy_that_keeps_the_rest_of_the_result():
    _Gate(_change(result="clean"))
    original = ToolResult(content="dirty", images=[])

    result = await _hook()("probe", lambda **kw: original, {})

    assert isinstance(result, ToolResult) and result is not original
    assert result.content == "clean" and result.images == original.images
    assert original.content == "dirty"


async def test_an_mcp_result_is_offered_as_the_text_the_model_would_read():
    gate = _Gate()

    await _hook()("mcp_x_read", lambda **kw: ToolResult(content="body"), {})

    [posted] = [v for v in gate.asked if isinstance(v, PostToolUseValue)]
    assert posted.result == wrap_untrusted("body", nonce=_nonce_of(posted.result))


def _nonce_of(text: str) -> str:
    return text.split("nonce=", 1)[1].split(">", 1)[0]


async def test_a_change_to_a_result_that_is_not_text_is_refused_with_a_warning(caplog):
    _Gate(_change(result="changed"))
    payload = {"rows": 3}

    with caplog.at_level(logging.WARNING):
        result = await _hook()("probe", lambda **kw: payload, {})

    assert result is payload
    assert any("probe" in r.getMessage() and "not text" in r.getMessage() for r in caplog.records)


async def test_a_result_that_is_not_text_and_unchanged_passes_without_a_warning(caplog):
    _Gate()

    with caplog.at_level(logging.WARNING):
        result = await _hook()("probe", lambda **kw: [1, 2], {})

    assert result == [1, 2] and not caplog.records


async def test_the_offered_text_of_a_non_text_result_is_its_string_form():
    gate = _Gate()

    await _hook()("probe", lambda **kw: {"rows": 3}, {})

    [posted] = [v for v in gate.asked if isinstance(v, PostToolUseValue)]
    assert posted.result == "{'rows': 3}"


async def test_an_unchanged_mcp_result_is_handed_on_as_it_is_not_copied():
    _Gate()
    original = ToolResult(content="body")

    assert await _hook()("probe", lambda **kw: original, {}) is original
