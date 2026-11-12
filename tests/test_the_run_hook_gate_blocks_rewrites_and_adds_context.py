"""``RunHookGate``: how the UserPromptSubmit and Stop hooks decide a run.

Extensions are asked first and the configured hooks see what the extensions
left. The configured hook's verdict is what blocks, with its own message or a
fixed one, or lets the run go with an optional context message.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from igni_core.backend.run_hook_gate import RunHookGate
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import ExtensionHookVerdict, extension_hook_gate
from igni_core.core.hooks.schemas import HookResult


class _Executor:
    """Answers each event with the result it was given and keeps what it was sent."""

    def __init__(self, result: HookResult | None = None, *, raises: bool = False) -> None:
        self.result = result or HookResult()
        self.raises = raises
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, *, event: str, payload: Any) -> HookResult:
        self.calls.append((event, payload))
        if self.raises:
            raise RuntimeError("hook crashed")
        return self.result


class _Extensions:
    """The extensions' side: lets everything through, or rewrites or rejects."""

    def __init__(self, *, rewrite: str | None = None, reject: str | None = None) -> None:
        self.rewrite = rewrite
        self.reject = reject
        extension_hook_gate.install(self)

    def hooks(self, event: HookEvent) -> bool:
        return True

    async def ask(self, value: Any) -> ExtensionHookVerdict[Any]:
        if self.reject is not None:
            return ExtensionHookVerdict(allowed=False, value=value, reason=self.reject)
        if self.rewrite is not None:
            value = value.model_copy(update={"message": self.rewrite})
        return ExtensionHookVerdict(allowed=True, value=value)

    def tell(self, event: HookEvent, payload: Any) -> None:
        return None

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_extensions() -> Iterator[None]:
    extension_hook_gate.install(None)
    yield
    extension_hook_gate.install(None)


def _gate(executor: _Executor) -> RunHookGate:
    return RunHookGate(executor, "s-1")  # type: ignore[arg-type]


class TestUserPromptSubmit:
    async def test_no_opinion_lets_the_text_through_with_no_context(self):
        result = await _gate(_Executor()).fire_user_prompt_submit("hello")

        assert result.should_continue
        assert result.text == "hello"
        assert result.context_message is None
        assert result.block_message is None

    async def test_the_hook_is_sent_the_event_the_text_and_the_session(self):
        executor = _Executor()

        await _gate(executor).fire_user_prompt_submit("hello")

        [(event, payload)] = executor.calls
        assert event == HookEvent.USER_PROMPT_SUBMIT.value
        assert (payload.message, payload.session_id) == ("hello", "s-1")

    async def test_a_hook_message_becomes_the_context(self):
        executor = _Executor(HookResult(message="remember the style guide"))

        result = await _gate(executor).fire_user_prompt_submit("hello")

        assert result.should_continue
        assert result.context_message == "remember the style guide"

    async def test_a_hook_that_stops_the_run_blocks_with_its_message(self):
        executor = _Executor(HookResult(should_continue=False, message="no secrets"))

        result = await _gate(executor).fire_user_prompt_submit("pw=1")

        assert not result.should_continue
        assert result.block_message == "no secrets"

    async def test_a_hook_that_stops_the_run_without_a_message_blocks_with_the_default(self):
        executor = _Executor(HookResult(should_continue=False))

        result = await _gate(executor).fire_user_prompt_submit("pw=1")

        assert not result.should_continue
        assert result.block_message == "Message blocked by hook."

    async def test_the_configured_hook_sees_the_text_the_extensions_left(self):
        _Extensions(rewrite="login pw=…")
        executor = _Executor()

        result = await _gate(executor).fire_user_prompt_submit("login pw=1")

        assert result.text == "login pw=…"
        assert executor.calls[0][1].message == "login pw=…"

    async def test_an_extension_rejection_blocks_before_the_configured_hook_is_asked(self):
        _Extensions(reject="No secrets.")
        executor = _Executor()

        result = await _gate(executor).fire_user_prompt_submit("pw=1")

        assert not result.should_continue
        assert result.block_message == "No secrets."
        assert executor.calls == []

    async def test_an_extension_rejection_without_a_reason_is_still_a_block(self):
        _Extensions(reject="")

        result = await _gate(_Executor()).fire_user_prompt_submit("pw=1")

        assert not result.should_continue
        assert not result.block_message

    async def test_empty_text_is_passed_on_like_any_other(self):
        result = await _gate(_Executor()).fire_user_prompt_submit("")

        assert result.should_continue
        assert result.text == ""

    async def test_a_hook_that_crashes_is_not_swallowed_here(self):
        with pytest.raises(RuntimeError, match="hook crashed"):
            await _gate(_Executor(raises=True)).fire_user_prompt_submit("hello")


class TestStop:
    async def test_no_opinion_continues_silently(self):
        result = await _gate(_Executor()).fire_stop()

        assert result.should_continue
        assert result.block_message is None

    async def test_the_hook_is_sent_the_stop_event_and_the_session(self):
        executor = _Executor()

        await _gate(executor).fire_stop()

        [(event, payload)] = executor.calls
        assert event == HookEvent.STOP.value
        assert payload.session_id == "s-1"

    async def test_a_message_with_should_continue_false_blocks_with_it(self):
        executor = _Executor(HookResult(should_continue=False, message="run the tests"))

        result = await _gate(executor).fire_stop()

        assert not result.should_continue
        assert result.block_message == "run the tests"

    async def test_should_continue_false_without_a_message_does_not_block(self):
        result = await _gate(_Executor(HookResult(should_continue=False))).fire_stop()

        assert result.should_continue
        assert result.block_message is None

    async def test_a_message_with_should_continue_true_is_dropped(self):
        result = await _gate(_Executor(HookResult(message="fyi"))).fire_stop()

        assert result.should_continue
        assert result.block_message is None

    async def test_an_extension_rejection_blocks_with_its_reason_and_skips_the_hook(self):
        _Extensions(reject="Run the tests.")
        executor = _Executor()

        result = await _gate(executor).fire_stop()

        assert not result.should_continue
        assert result.block_message == "Run the tests."
        assert executor.calls == []
