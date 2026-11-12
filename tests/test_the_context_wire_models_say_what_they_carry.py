"""The wire models of the context surface, and the cards `/output-style`
draws, say exactly what the client reads.

``/ctx`` itself is pinned in ``test_ctx_actually_renders.py``; this file
covers the rest of ``schemas_context``: the rows projected onto the wire,
the outcomes with their empty-on-failure defaults, and the output-style
cards.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.backend import schemas_context
from igni_core.backend.schemas_context import (
    PENDING_STALENESS_SECONDS,
    CompactionOutcome,
    DiscardInterruptedRunResult,
    InterruptedRun,
    OutputStylesListView,
    OutputStyleStatusView,
    PendingMessage,
    TruncateHistoryResult,
)
from igni_core.core.output_styles import OutputStyle
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.pending_messages import InterruptedMessage as InterruptedRow
from igni_core.core.session.pending_messages import PendingMessage as PendingRow
from igni_core.protocol.messages import CommandResultKind


def style(name: str, description: str = "") -> OutputStyle:
    return OutputStyle(name=name, path=Path(f"/x/{name}.md"), description=description, body="b")


def test_a_pending_row_is_not_shown_as_interrupted_for_the_first_minute():
    assert PENDING_STALENESS_SECONDS == 60


class TestPendingMessage:
    def test_a_stored_row_becomes_a_user_message_with_its_text_as_content(self):
        row = PendingRow(message_id="m1", session_id="s1", text="héllo 👋", received_at=1700)

        wire = PendingMessage.from_pending_row(row)

        assert wire.model_dump() == {
            "role": "user",
            "content": "héllo 👋",
            "received_at": 1700,
            "message_id": "m1",
        }

    def test_the_session_id_stays_off_the_wire(self):
        row = PendingRow(message_id="m1", session_id="secret-session", text="", received_at=1)

        assert "secret-session" not in PendingMessage.from_pending_row(row).model_dump_json()

    def test_an_empty_text_is_still_a_message(self):
        row = PendingRow(message_id="m", session_id="s", text="", received_at=0)

        assert PendingMessage.from_pending_row(row).content == ""

    def test_the_role_cannot_be_anything_but_user(self):
        with pytest.raises(ValidationError):
            PendingMessage(role="assistant", content="x", received_at=1, message_id="m")  # type: ignore[arg-type]


class TestInterruptedRun:
    @pytest.mark.parametrize("reason", ["cancelled", "errored", "abandoned"])
    def test_each_reason_survives_the_projection(self, reason: str):
        row = InterruptedRow(
            message_id="m",
            session_id="s",
            text="t",
            received_at=1,
            interrupted_at=2,
            interrupted_reason=reason,  # type: ignore[arg-type]
            last_error="boom" if reason == "errored" else None,
        )

        wire = InterruptedRun.from_interrupted_row(row).model_dump()

        assert wire == {
            "message_id": "m",
            "content": "t",
            "received_at": 1,
            "interrupted_at": 2,
            "reason": reason,
            "last_error": "boom" if reason == "errored" else None,
        }

    def test_an_unknown_reason_is_refused(self):
        with pytest.raises(ValidationError):
            InterruptedRun(
                message_id="m",
                content="t",
                received_at=1,
                interrupted_at=2,
                reason="vanished",  # type: ignore[arg-type]
            )


class TestOutcomes:
    def test_a_truncation_that_failed_removed_nothing_and_says_why(self):
        failed = TruncateHistoryResult(removed=0, error="no such run")

        assert failed.model_dump() == {"removed": 0, "error": "no such run"}

    def test_a_truncation_that_worked_has_no_error(self):
        assert TruncateHistoryResult(removed=3).model_dump() == {"removed": 3, "error": ""}

    def test_a_compaction_that_did_not_happen_has_no_session_and_no_summary(self):
        assert CompactionOutcome(compacted=False).model_dump() == {
            "compacted": False,
            "new_session_id": "",
            "summary": "",
        }

    def test_a_compaction_that_happened_carries_its_session_and_summary(self):
        done = CompactionOutcome(compacted=True, new_session_id="s1", summary="was about x")

        assert done.model_dump() == {
            "compacted": True,
            "new_session_id": "s1",
            "summary": "was about x",
        }

    def test_a_compaction_must_say_whether_it_happened(self):
        with pytest.raises(ValidationError):
            CompactionOutcome()  # type: ignore[call-arg]

    def test_a_discard_that_worked_has_no_error(self):
        assert DiscardInterruptedRunResult(ok=True).model_dump() == {"ok": True, "error": ""}

    def test_a_discard_that_failed_says_why(self):
        failed = DiscardInterruptedRunResult(ok=False, error="message_id is empty")

        assert failed.model_dump() == {"ok": False, "error": "message_id is empty"}

    def test_the_outcome_models_are_exported(self):
        for name in ("CompactionOutcome", "DiscardInterruptedRunResult", "TruncateHistoryResult"):
            assert name in schemas_context.__all__


class TestTheStyleList:
    def test_no_styles_gives_an_info_card_naming_the_directory(self):
        result = OutputStylesListView().to_command_result()

        assert result.kind == CommandResultKind.INFO
        assert result.content == (
            "No output styles configured. Drop a markdown file at "
            f"`{CONFIG_DIR}/output-styles/<name>.md` (frontmatter: `name`, "
            "`description`; body is the system-prompt extension)."
        )

    def test_styles_are_listed_sorted_with_the_active_one_marked(self):
        view = OutputStylesListView(
            styles={
                "terse": style("terse", "Short answers"),
                "alpha": style("alpha"),
                "mid": style("mid", "Middle"),
            },
            active="terse",
        )

        result = view.to_command_result()

        assert result.kind == CommandResultKind.MARKDOWN
        assert result.content == (
            "**Output styles**\n"
            "\n"
            "- `alpha` — _(no description)_\n"
            "- `mid` — Middle\n"
            "- `terse` (active) — Short answers\n"
            "\n"
            "Switch with `/output-style <name>`."
        )

    def test_an_active_name_that_is_not_a_style_marks_nothing(self):
        view = OutputStylesListView(styles={"a": style("a", "A")}, active="ghost")

        assert "(active)" not in view.to_command_result().content

    def test_a_name_with_markdown_characters_is_rendered_verbatim_in_backticks(self):
        view = OutputStylesListView(styles={"a*b_c": style("a*b_c", "d")})

        assert "- `a*b_c` — d" in view.to_command_result().content


class TestTheStyleStatus:
    @pytest.mark.parametrize("active", [None, ""])
    def test_no_style_reads_none(self, active):
        result = OutputStyleStatusView(active=active).to_command_result()

        assert result.kind == CommandResultKind.INFO
        assert result.content == "Active output style: **(none)**"

    def test_a_style_is_named(self):
        result = OutputStyleStatusView(active="terse").to_command_result()

        assert result.content == "Active output style: **terse**"
