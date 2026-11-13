"""The turn pipeline every run shares: what is printed around the model call.

:class:`SessionRun` is the base of the interactive loop and ``igni -m``.
These tests run its real ``_run_turn`` and hooks over a session double that
only answers the model call, and read what the display was told.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from igni_core.core.config.settings import Settings
from igni_core.core.session.session_run import SessionRun
from igni_core.core.utils.display_schemas import RunStats


class _Run(SessionRun):
    async def run(self) -> None:  # pragma: no cover - never entered
        return None


def _session(response: str = "Hello!", *, failure: object = "", project_dir: Path | None = None):
    session = MagicMock()
    session.session_id = "s-1"
    session.settings.models.default = "igni-1.0-4b"
    session.project_dir = project_dir
    session.display = MagicMock()
    session.handle_message = AsyncMock(return_value=response)
    session.hook_executor.execute = AsyncMock()
    type(session).last_turn_failure = PropertyMock(return_value=failure)
    return session


def _run(session) -> _Run:
    with patch("igni_core.core.session.session_run.Session", return_value=session):
        return _Run(Settings())


def _stats(session) -> RunStats:
    (stats,) = session.display.print_run_stats.call_args.args
    return stats


def test_a_subclass_without_run_cannot_be_built():
    class NoRun(SessionRun):
        pass

    with pytest.raises(TypeError, match="abstract"):
        NoRun(Settings())  # type: ignore[abstract]


def test_the_session_is_built_from_the_runs_arguments(tmp_path):
    with patch("igni_core.core.session.session_run.Session") as session_class:
        settings = Settings()
        _Run(
            settings,
            resume_session_id="old",
            project_dir=tmp_path,
            additional_dirs=[tmp_path / "x"],
        )

    session_class.assert_called_once_with(
        settings, project_dir=tmp_path, resume_session_id="old", additional_dirs=[tmp_path / "x"]
    )


async def test_session_start_and_end_carry_the_session_id_and_whether_it_was_resumed():
    session = _session()
    with patch("igni_core.core.session.session_run.Session", return_value=session):
        run = _Run(Settings(), resume_session_id="")

    await run._fire_session_start()
    await run._fire_session_end()

    calls = session.hook_executor.execute.await_args_list
    assert [c.kwargs["event"] for c in calls] == ["SessionStart", "SessionEnd"]
    for call in calls:
        assert call.kwargs["payload"].session_id == "s-1"
        # An empty resume id is no resume.
        assert call.kwargs["payload"].resumed is False


async def test_an_at_mention_is_named_before_the_model_answers(tmp_path):
    session = _session(project_dir=tmp_path)

    assert await _run(session)._run_turn("look at @notes.txt and @other.py") is True

    session.display.print_info.assert_called_once_with("Referenced: notes.txt, other.py")
    (sent,) = session.handle_message.await_args.args
    assert "[Referenced files: notes.txt, other.py" in sent
    assert sent.endswith("look at @notes.txt and @other.py")


async def test_a_message_without_references_prints_no_info_line(tmp_path):
    session = _session(project_dir=tmp_path)

    assert await _run(session)._run_turn("plain words") is True

    session.display.print_info.assert_not_called()
    session.handle_message.assert_awaited_once_with("plain words")


async def test_a_referenced_file_is_resolved_to_its_path_and_named(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    session = _session(project_dir=tmp_path)

    await _run(session)._run_turn("describe shot.png")

    full = str((tmp_path / "shot.png").resolve())
    session.display.print_info.assert_called_once_with(f"Resolved: {full}")
    (sent,) = session.handle_message.await_args.args
    assert sent == f"describe {full}"


async def test_a_failed_turn_prints_only_the_stats_with_the_default_model_and_returns_false():
    session = _session("Error text that is not a reply", failure="provider refused")

    assert await _run(session)._run_turn("hello") is False

    session.display.print_response.assert_not_called()
    session.display.print_warning.assert_not_called()
    stats = _stats(session)
    assert stats.model == "igni-1.0-4b"
    assert stats.elapsed_seconds >= 0


async def test_a_failure_that_is_not_a_string_is_not_a_failed_turn():
    """A loosely held session answers with a truthy non-string; only a
    string is the failure's text."""
    session = _session("fine", failure=object())

    assert await _run(session)._run_turn("hello") is True

    session.display.print_response.assert_called_once_with("fine")


async def test_an_answered_turn_prints_the_reply_then_the_stats():
    session = _session("Hello!")

    assert await _run(session)._run_turn("hi") is True

    assert [c[0] for c in session.display.method_calls] == ["print_response", "print_run_stats"]
    assert _stats(session).model == "igni-1.0-4b"


async def test_an_empty_answer_warns_then_still_prints_the_stats_and_succeeds():
    session = _session("  \n")

    assert await _run(session)._run_turn("hi") is True

    assert [c[0] for c in session.display.method_calls] == ["print_warning", "print_run_stats"]
