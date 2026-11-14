"""How the developer's remembered answers about group hooks are read and written.

``~/.igni/group-hook-decisions.json`` is the developer's file. Whatever state
it is in, a read never crashes the hook dispatch: an unreadable file is asked
about again. A write goes through a temporary file, ends up readable by the
developer alone, and a failed write is logged and simply not remembered.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from igni_core.core.hooks.group_hook_decisions import (
    ForgetfulGroupHookDecisions,
    GroupHookDecisionRecord,
    GroupHookDecisions,
)
from igni_core.core.hooks.schemas import HookDefinition


def _hook(command: str = "echo hi") -> HookDefinition:
    return HookDefinition(type="command", command=command).mark_from_group("/g/hooks.json")


class TestReadingTheFile:
    def test_a_missing_file_has_no_answers_and_is_not_created(self, tmp_path: Path):
        decisions = GroupHookDecisions(tmp_path / "none.json")

        assert decisions.get("abc") is None
        assert not (tmp_path / "none.json").exists()

    def test_a_file_that_is_not_utf8_asks_again(self, tmp_path: Path, caplog):
        path = tmp_path / "decisions.json"
        path.write_bytes(b"\xff\xfe\x00 not text")

        with caplog.at_level(logging.WARNING):
            assert GroupHookDecisions(path).get("abc") is None

        assert "asking again" in caplog.text

    @pytest.mark.parametrize("content", ["not json", "[]", '{"hooks": []}', '{"version": "x"}'])
    def test_a_file_of_the_wrong_shape_asks_again(self, tmp_path: Path, content: str, caplog):
        path = tmp_path / "decisions.json"
        path.write_text(content, encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            assert GroupHookDecisions(path).get("abc") is None

        assert "asking again" in caplog.text

    def test_a_path_that_cannot_be_read_asks_again(self, tmp_path: Path, caplog):
        directory = tmp_path / "decisions.json"
        directory.mkdir()

        with caplog.at_level(logging.WARNING):
            assert GroupHookDecisions(directory).get("abc") is None

        assert "Could not read" in caplog.text

    def test_a_bad_entry_is_named_in_the_log(self, tmp_path: Path, caplog):
        path = tmp_path / "decisions.json"
        path.write_text('{"hooks": {"abc": {"decision": "maybe"}}}', encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            assert GroupHookDecisions(path).get("abc") is None

        assert "unreadable answer for abc" in caplog.text


class TestWritingTheFile:
    def test_the_record_holds_the_decision_the_hook_the_source_and_a_whole_second(
        self, tmp_path: Path
    ):
        path = tmp_path / "decisions.json"
        hook = _hook("echo one")

        before = datetime.now(UTC).replace(microsecond=0)
        GroupHookDecisions(path).set(hook.digest(), "run", hook)

        record = json.loads(path.read_text(encoding="utf-8"))["hooks"][hook.digest()]
        assert record["decision"] == "run"
        assert record["what"] == "echo one"
        assert record["source"] == "/g/hooks.json"
        decided = datetime.fromisoformat(record["decided_at"])
        assert decided.tzinfo is not None
        assert decided.microsecond == 0
        assert before <= decided <= datetime.now(UTC)

    def test_the_file_is_the_developers_alone(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        hook = _hook()

        GroupHookDecisions(path).set(hook.digest(), "run", hook)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_missing_parent_directories_are_created(self, tmp_path: Path):
        path = tmp_path / "a" / "b" / "decisions.json"
        hook = _hook()

        GroupHookDecisions(path).set(hook.digest(), "refuse", hook)

        assert GroupHookDecisions(path).get(hook.digest()) == "refuse"

    def test_no_temporary_file_is_left_beside_it(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        hook = _hook()

        GroupHookDecisions(path).set(hook.digest(), "run", hook)

        assert [p.name for p in tmp_path.iterdir()] == ["decisions.json"]

    def test_a_later_answer_for_the_same_hook_replaces_the_earlier(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        hook = _hook()

        GroupHookDecisions(path).set(hook.digest(), "refuse", hook)
        GroupHookDecisions(path).set(hook.digest(), "run", hook)

        assert GroupHookDecisions(path).get(hook.digest()) == "run"
        assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]) == 1

    def test_a_write_that_fails_is_logged_and_not_remembered(self, tmp_path: Path, caplog):
        # The decisions "file" is a directory, so os.replace cannot put one there.
        path = tmp_path / "decisions.json"
        path.mkdir()
        hook = _hook()

        with caplog.at_level(logging.WARNING):
            GroupHookDecisions(path).set(hook.digest(), "run", hook)

        assert "Could not record the answer" in caplog.text
        assert GroupHookDecisions(path).get(hook.digest()) is None

    def test_a_write_that_fails_leaves_no_temporary_file(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        path.mkdir()
        hook = _hook()

        GroupHookDecisions(path).set(hook.digest(), "run", hook)

        assert [p.name for p in tmp_path.iterdir()] == ["decisions.json"]

    def test_an_unwritable_directory_is_logged_not_raised(self, tmp_path: Path, caplog):
        if os.geteuid() == 0:
            pytest.skip("root writes anywhere")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        hook = _hook()
        try:
            with caplog.at_level(logging.WARNING):
                GroupHookDecisions(locked / "decisions.json").set(hook.digest(), "run", hook)
        finally:
            locked.chmod(0o700)

        assert "Could not record the answer" in caplog.text


class TestWhereTheFileLives:
    def test_the_developers_file_is_under_their_igni_directory(self):
        decisions = GroupHookDecisions.in_home()

        assert decisions.path == Path.home() / ".igni" / "group-hook-decisions.json"
        assert GroupHookDecisions.FILENAME == "group-hook-decisions.json"


class TestForgetting:
    def test_a_forgetful_store_remembers_nothing(self):
        hook = _hook()
        store = ForgetfulGroupHookDecisions()

        store.set(hook.digest(), "run", hook)

        assert store.get(hook.digest()) is None

    def test_a_record_made_now_reads_back_with_the_hooks_own_description(self):
        hook = _hook("echo two")

        record = GroupHookDecisionRecord.now("refuse", hook)

        assert (record.decision, record.what, record.source) == (
            "refuse",
            "echo two",
            "/g/hooks.json",
        )
