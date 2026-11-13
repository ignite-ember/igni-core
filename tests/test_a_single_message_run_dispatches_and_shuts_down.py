"""What ``igni -m`` does with a message, and how it shuts down.

The turn pipeline is :class:`SessionRun`; the dispatch and the shutdown
order are :class:`SingleMessageRun`. These run the real classes against a
real session's command handler, with the model call and the display
replaced, so what is asserted is what the run sends to the model, what it
prints, which hooks it fires and in what order, and that the extensions
are always stopped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from igni_core.core.config.settings import Settings
from igni_core.core.session.single_message_run import SingleMessageRun
from tests.session_factory import build_session


class _Extensions:
    """Records when the run brings extensions up and down, among the hooks."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def start(self) -> None:
        self._log.append("extensions.start")

    async def stop(self) -> None:
        self._log.append("extensions.stop")


@pytest.fixture
def log() -> list[str]:
    return []


@pytest.fixture
def session(tmp_path: Path, log: list[str]):
    session = build_session(tmp_path)

    async def hook(*, event: str, payload) -> None:
        log.append(event)

    session.hook_executor.execute = hook
    session.display = MagicMock()
    session.handle_message = AsyncMock(return_value="the answer")
    return session


async def _run(session, log: list[str], message: str, **kwargs) -> None:
    with (
        patch("igni_core.core.session.session_run.Session", return_value=session),
        patch(
            "igni_core.core.session.single_message_run.HeadlessExtensions",
            lambda _session: _Extensions(log),
        ),
    ):
        await SingleMessageRun(Settings(), **kwargs).run(message)


async def test_a_plain_message_is_the_turn_as_typed(session, log):
    await _run(session, log, "  /not a command, leading space")

    session.handle_message.assert_awaited_once_with(
        "/not a command, leading space".join(["  ", ""])
    )


async def test_extensions_come_up_before_session_start_and_go_down_after_session_end(session, log):
    await _run(session, log, "hello")

    assert log == ["extensions.start", "SessionStart", "SessionEnd", "extensions.stop"]


async def test_a_loop_prompt_is_the_turn_and_prints_nothing_instead(session, log):
    await _run(session, log, "/loop 5m check it")

    (sent,) = session.handle_message.await_args.args
    assert "<loop-iteration" in sent and "check it" in sent
    session.display.print_info.assert_not_called()
    session.display.print_response.assert_called_once_with("the answer")


async def test_a_built_in_command_prints_its_result_and_runs_no_turn(session, log):
    await _run(session, log, "/output-style nope")

    session.handle_message.assert_not_called()
    (printed,) = session.display.print_info.call_args.args
    assert printed.startswith("Error: unknown output style 'nope'")
    session.display.print_response.assert_not_called()
    assert log.count("SessionEnd") == 1
    assert log == ["extensions.start", "SessionStart", "SessionEnd", "extensions.stop"]


@pytest.mark.parametrize("message", ["/", "/nosuchthing"])
async def test_an_unknown_command_is_said_so_and_never_sent_to_the_model(session, log, message):
    await _run(session, log, message)

    session.handle_message.assert_not_called()
    (printed,) = session.display.print_info.call_args.args
    assert printed == f"Unknown command: {message}"
    assert log.count("SessionEnd") == 1


async def test_a_turn_that_raises_still_stops_the_extensions_and_skips_session_end(session, log):
    session.handle_message = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await _run(session, log, "hello")

    assert log == ["extensions.start", "SessionStart", "extensions.stop"]


async def test_a_failed_turn_ends_the_session_and_stops_extensions_before_exiting(session, log):
    with (
        patch.object(
            type(session), "last_turn_failure", new_callable=PropertyMock, return_value="refused"
        ),
        pytest.raises(SystemExit) as exit_,
    ):
        await _run(session, log, "hello")

    assert exit_.value.code == SingleMessageRun.FAILED_EXIT_CODE == 1
    assert log == ["extensions.start", "SessionStart", "SessionEnd", "extensions.stop"]


async def test_a_successful_run_returns_normally(session, log):
    with patch.object(
        type(session), "last_turn_failure", new_callable=PropertyMock, return_value=""
    ):
        assert await _run(session, log, "hello") is None
