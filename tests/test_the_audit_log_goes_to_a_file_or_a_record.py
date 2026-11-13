"""Where an audit entry goes: a JSONL file on the desktop, one structured
record on a layer that keeps no log files; and what the entry's wire
shape holds at its edges."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from igni_core.core.layer import Layer, layers
from igni_core.core.log_fields import LogFields
from igni_core.core.utils.audit import AuditEntry, AuditLogger
from igni_core.core.utils.audit_schemas import (
    AuditStatus,
    AuditWriteResult,
    BlockedDetails,
    ErrorDetails,
    ToolCallDetails,
)


def _logger(path: Path) -> AuditLogger:
    settings = MagicMock()
    settings.storage.audit_log = str(path)
    return AuditLogger(settings)


def _entry(**kwargs) -> AuditEntry:
    return AuditEntry.success(
        session_id=kwargs.get("session_id", "s1"), agent_name="main", tool_name="edit"
    )


class _NoLogFiles(Layer):
    name = "nofiles"

    def log_files(self) -> bool:
        return False


class TestOnALayerWithoutLogFiles:
    def test_the_entry_is_one_audit_record_and_no_file_or_directory_is_made(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        layers.install(_NoLogFiles())
        path = tmp_path / "deep" / "audit.log"
        logger = _logger(path)
        with caplog.at_level(logging.INFO, logger="igni_core.audit"):
            result = logger.log(
                AuditEntry.blocked(
                    session_id="s1", agent_name="main", tool_name="run_shell", reason="rm -rf /"
                )
            )
        assert result == AuditWriteResult(ok=True)
        assert not path.parent.exists()
        records = [r for r in caplog.records if r.name == "igni_core.audit"]
        assert len(records) == 1
        assert records[0].getMessage() == "audit"
        fields = LogFields.carried_by(records[0])
        assert fields is not None
        section = fields.sections["audit"]
        assert isinstance(section, dict)
        assert section["session_id"] == "s1"
        assert section["tool"] == "run_shell"
        assert section["status"] == "blocked"
        assert section["details"] == {"reason": "rm -rf /"}

    def test_the_desktop_layer_writes_the_file_and_no_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = tmp_path / "audit.log"
        with caplog.at_level(logging.INFO, logger="igni_core.audit"):
            _logger(path).log(_entry())
        assert path.read_text().count("\n") == 1
        assert [r for r in caplog.records if r.name == "igni_core.audit"] == []


class TestOnTheDesktop:
    def test_the_log_path_expands_the_home_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        logger = _logger(Path("~/logs/audit.log"))
        assert logger.log_path == tmp_path / "logs" / "audit.log"
        logger.log(_entry())
        assert (tmp_path / "logs" / "audit.log").is_file()

    def test_non_ascii_content_is_written_and_read_back_whole(self, tmp_path: Path):
        path = tmp_path / "audit.log"
        _logger(path).log(
            AuditEntry.tool_call(
                session_id="сесія", agent_name="a", tool_name="t", args='привіт "світ" \\ 🙂'
            )
        )
        entry = json.loads(path.read_text(encoding="utf-8"))
        assert entry["session_id"] == "сесія"
        assert entry["details"] == {"args": 'привіт "світ" \\ 🙂'}

    def test_a_failing_write_warns_once_per_logger_and_keeps_failing_quietly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # A directory where the file should be: opening it for append is an OSError.
        path = tmp_path / "audit.log"
        logger = _logger(path)
        path.mkdir()
        with caplog.at_level(logging.WARNING, logger="igni_core.core.utils.audit"):
            first = logger.log(_entry())
            second = logger.log(_entry())
        assert first.ok is False and second.ok is False
        assert first.reason and "audit.log" in first.reason
        assert second.reason == first.reason
        warnings = [r for r in caplog.records if "Audit log write failed" in r.getMessage()]
        assert len(warnings) == 1
        assert first.reason in warnings[0].getMessage()

    def test_another_logger_warns_again_for_itself(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = tmp_path / "audit.log"
        one, two = _logger(path), _logger(path)
        path.mkdir()
        with caplog.at_level(logging.WARNING, logger="igni_core.core.utils.audit"):
            one.log(_entry())
            two.log(_entry())
        assert len([r for r in caplog.records if "Audit log write failed" in r.getMessage()]) == 2

    def test_two_loggers_on_one_file_add_whole_lines(self, tmp_path: Path):
        path = tmp_path / "audit.log"
        one, two = _logger(path), _logger(path)
        for i in range(10):
            (one if i % 2 else two).log(_entry(session_id=f"s{i}"))
        sessions = [json.loads(line)["session_id"] for line in path.read_text().splitlines()]
        assert sessions == [f"s{i}" for i in range(10)]


class TestTheWireShape:
    def test_the_statuses_are_lowercase_words(self):
        assert [s.value for s in AuditStatus] == ["success", "error", "blocked"]

    def test_the_default_timestamp_is_utc_aware(self):
        entry = _entry()
        assert entry.timestamp.utcoffset() == timedelta(0)
        assert json.loads(entry.to_jsonl_line())["timestamp"].endswith("+00:00")

    def test_a_line_is_one_newline_terminated_object_with_the_keys_in_order(self):
        line = _entry().to_jsonl_line()
        assert line.endswith("\n") and line.count("\n") == 1
        assert list(json.loads(line)) == ["timestamp", "session_id", "agent", "tool", "status"]

    def test_an_error_without_a_type_omits_the_type(self):
        entry = AuditEntry.error(session_id="s", agent_name="a", tool_name="t", error="boom")
        data = json.loads(entry.to_jsonl_line())
        assert data["status"] == "error"
        assert data["details"] == {"error": "boom"}

    def test_an_error_with_a_type_carries_it(self):
        entry = AuditEntry.error(
            session_id="s", agent_name="a", tool_name="t", error="boom", error_type="OSError"
        )
        assert json.loads(entry.to_jsonl_line())["details"] == {
            "error": "boom",
            "error_type": "OSError",
        }

    def test_a_tool_call_is_a_success_that_keeps_its_args_verbatim(self):
        entry = AuditEntry.tool_call(
            session_id="s", agent_name="a", tool_name="t", args="  foo --bar 3 "
        )
        data = json.loads(entry.to_jsonl_line())
        assert data["status"] == "success"
        assert data["details"] == {"args": "  foo --bar 3 "}

    def test_a_tool_call_with_no_args_still_carries_an_empty_args_string(self):
        entry = AuditEntry.tool_call(session_id="s", agent_name="a", tool_name="t", args="")
        assert json.loads(entry.to_jsonl_line())["details"] == {"args": ""}

    def test_a_blocked_entry_without_a_reason_is_refused(self):
        with pytest.raises(ValidationError):
            BlockedDetails()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            AuditEntry.model_validate(
                {
                    "session_id": "s",
                    "agent": "a",
                    "tool": "t",
                    "status": "blocked",
                    "details": {"kind": "blocked"},
                }
            )

    def test_an_unknown_details_kind_is_refused(self):
        with pytest.raises(ValidationError):
            AuditEntry.model_validate(
                {
                    "session_id": "s",
                    "agent": "a",
                    "tool": "t",
                    "status": "success",
                    "details": {"kind": "mystery"},
                }
            )

    def test_the_details_variants_default_their_optional_fields(self):
        assert ErrorDetails(error="x").error_type is None
        assert ToolCallDetails().args == ""
        assert AuditWriteResult(ok=False).reason is None
