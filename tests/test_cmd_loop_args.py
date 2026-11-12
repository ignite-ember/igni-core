"""``/loop`` — how its argument is read and what each verb answers.

``test_loop.py`` drives the loop machinery; this file pins the command's
own grammar: the leading ``<N>`` / ``<N>x`` cap, the boundaries of the
cap, what is text and what is a number, and the stop/resume verbs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from igni_core.backend.cmd_loop import LoopCapArgs, LoopCommand, cmd_loop, loop_status, loop_stop
from igni_core.core.loop.limits import LOOP_DEFAULT_MAX_ITERATIONS, LOOP_HARD_CAP
from igni_core.core.session.loop_ops import LoopController
from igni_core.protocol.messages import CommandAction, CommandResultKind


def _session() -> SimpleNamespace:
    store = AsyncMock()
    store.load = AsyncMock(return_value=None)
    return SimpleNamespace(loop=LoopController(loop_store=store))


def _handler(session) -> SimpleNamespace:
    return SimpleNamespace(session=session)


class TestReadingTheCap:
    def test_text_with_no_number_is_the_prompt_with_the_default_cap(self):
        parsed = LoopCapArgs.parse("fix the typo")
        assert parsed.ok
        assert (parsed.max_iter, parsed.cap_explicit, parsed.prompt) == (
            LOOP_DEFAULT_MAX_ITERATIONS,
            False,
            "fix the typo",
        )

    @pytest.mark.parametrize("text", ["5 fix it", "5x fix it"])
    def test_a_leading_number_is_an_explicit_cap(self, text: str):
        parsed = LoopCapArgs.parse(text)
        assert (parsed.max_iter, parsed.cap_explicit, parsed.prompt) == (5, True, "fix it")

    def test_the_prompt_is_trimmed(self):
        assert LoopCapArgs.parse("5   spaced out  ").prompt == "spaced out"

    @pytest.mark.parametrize("text", ["0 go", "0x go"])
    def test_zero_is_refused(self, text: str):
        parsed = LoopCapArgs.parse(text)
        assert not parsed.ok
        assert "must be positive" in parsed.error

    def test_a_minus_sign_makes_it_text_not_a_number(self):
        parsed = LoopCapArgs.parse("-3 files")
        assert parsed.ok and not parsed.cap_explicit
        assert parsed.prompt == "-3 files"

    def test_the_hard_cap_is_accepted_and_one_past_it_is_refused(self):
        assert LoopCapArgs.parse(f"{LOOP_HARD_CAP} go").max_iter == LOOP_HARD_CAP
        refused = LoopCapArgs.parse(f"{LOOP_HARD_CAP + 1} go")
        assert not refused.ok
        assert f"{LOOP_HARD_CAP + 1} exceeds the hard cap of {LOOP_HARD_CAP}" in refused.error

    def test_a_huge_number_is_refused_not_overflowed(self):
        assert not LoopCapArgs.parse("9" * 40 + " go").ok

    @pytest.mark.parametrize("text", ["²  fix it", "٣ fix it", "５ fix it"])
    def test_digits_int_cannot_read_are_text_not_a_crash(self, text: str):
        """``'²'.isdigit()`` is true and ``int('²')`` raises: the command
        crashed on a prompt that merely began with such a character."""
        parsed = LoopCapArgs.parse(text)
        assert parsed.ok
        assert parsed.cap_explicit is False
        assert parsed.prompt == text

    def test_a_lone_x_is_text(self):
        parsed = LoopCapArgs.parse("x marks the spot")
        assert not parsed.cap_explicit and parsed.prompt == "x marks the spot"


class TestTheVerbs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["", "   "])
    async def test_no_arguments_open_the_panel(self, args: str):
        result = await cmd_loop(_handler(_session()), args)
        assert result.action is CommandAction.LOOP

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token", ["stop", "cancel", "off", "end", "STOP", " Cancel "])
    async def test_stop_tokens_cancel_the_loop(self, token: str):
        session = _session()
        await cmd_loop(_handler(session), "3 work")
        result = await cmd_loop(_handler(session), token)
        assert session.loop.pending_loop_prompt is None
        assert result.kind is not CommandResultKind.ERROR

    @pytest.mark.asyncio
    async def test_stopping_with_nothing_running_says_so(self):
        result = await cmd_loop(_handler(_session()), "stop")
        assert result.content == "No loop is running."

    @pytest.mark.asyncio
    async def test_a_cap_with_no_prompt_is_an_error(self):
        result = await cmd_loop(_handler(_session()), "5")
        assert result.is_error()
        assert "Loop needs a prompt" in result.content

    @pytest.mark.asyncio
    async def test_a_bad_cap_is_an_error_and_starts_nothing(self):
        session = _session()
        result = await cmd_loop(_handler(session), f"{LOOP_HARD_CAP + 1} go")
        assert result.is_error()
        assert session.loop.pending_loop_prompt is None

    @pytest.mark.asyncio
    async def test_a_prompt_starting_with_a_unicode_digit_starts_a_loop(self):
        session = _session()
        result = await cmd_loop(_handler(session), "² files to fix")
        assert result.action is CommandAction.RUN_PROMPT
        assert session.loop.pending_loop_prompt == "² files to fix"

    @pytest.mark.asyncio
    async def test_the_total_is_shown_only_when_the_cap_was_asked_for(self):
        explicit = await cmd_loop(_handler(_session()), "4 work")
        implicit = await cmd_loop(_handler(_session()), "work")
        assert 'total="4"' in explicit.content
        assert "total=" not in implicit.content

    @pytest.mark.asyncio
    async def test_starting_over_a_running_loop_is_refused(self):
        session = _session()
        await cmd_loop(_handler(session), "3 first")
        result = await cmd_loop(_handler(session), "9 second")
        assert result.action is not CommandAction.RUN_PROMPT
        assert session.loop.pending_loop_prompt == "first"


class TestResume:
    @pytest.mark.asyncio
    async def test_with_no_loop_it_is_an_error(self):
        result = await cmd_loop(_handler(_session()), "resume")
        assert result.is_error()
        assert result.content == "No loop to resume."

    @pytest.mark.asyncio
    async def test_a_running_loop_needs_no_resume(self):
        session = _session()
        await cmd_loop(_handler(session), "3 work")
        result = await cmd_loop(_handler(session), "RESUME")
        assert result.content == "Loop is already running — no resume needed."

    @pytest.mark.asyncio
    async def test_a_paused_loop_re_fires_its_iteration(self):
        session = _session()
        await cmd_loop(_handler(session), "3 work")
        await session.loop.pause_loop()
        assert session.loop.paused

        result = await cmd_loop(_handler(session), "resume")

        assert result.action is CommandAction.RUN_PROMPT
        assert "work" in result.content
        assert not session.loop.paused

    @pytest.mark.asyncio
    async def test_losing_the_race_to_resume_reads_as_already_running(self, monkeypatch):
        session = _session()
        await cmd_loop(_handler(session), "3 work")
        await session.loop.pause_loop()
        monkeypatch.setattr(session.loop, "resume_loop", AsyncMock(return_value=None))

        result = await cmd_loop(_handler(session), "resume")

        assert result.content == "Loop is already running."


class TestStatusAndTheShims:
    def test_status_with_no_loop_is_the_usage_view(self):
        result = loop_status(_handler(_session()))
        assert result.kind is not CommandResultKind.ERROR
        assert "/loop" in result.content

    @pytest.mark.asyncio
    async def test_status_names_the_prompt_and_progress(self):
        session = _session()
        await cmd_loop(_handler(session), "5 polish the docs")
        content = loop_status(_handler(session)).content
        assert content == ("Loop active: 1 done, 4 remaining.\nPrompt: 'polish the docs'")

    @pytest.mark.asyncio
    async def test_stopping_reports_how_far_it_got(self):
        session = _session()
        await cmd_loop(_handler(session), "5 work")
        result = await cmd_loop(_handler(session), "stop")
        assert result.content == "Loop stopped after 1 iteration."

    @pytest.mark.asyncio
    async def test_the_stop_shim_cancels(self):
        session = _session()
        await cmd_loop(_handler(session), "5 work")
        await loop_stop(_handler(session))
        assert session.loop.pending_loop_prompt is None
        assert (await LoopCommand(session).stop()).content == "No loop is running."
