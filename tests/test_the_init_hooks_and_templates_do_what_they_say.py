"""The hook scripts and starter files ``igni init`` writes.

``core/init_templates.py``. The two hooks are bash that runs on a
person's machine whenever the agent pushes or commits, so they are run
here as bash, on a real repository, with the payload the hook runner
sends on stdin.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from igni_core.core.init_templates import (
    _HOME_CONFIG_BOOTSTRAP,
    CONFIG_YAML_HEADER,
    IGNI_MD_TEMPLATE,
    POST_COMMIT_TODO_HOOK,
    PRE_PR_REVIEW_HOOK,
    PROJECT_CONFIG_TEMPLATE,
)

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test"]


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _run(script: str, cwd: Path, command: str | None) -> dict:
    payload = {"tool_name": "Bash", "tool_input": {} if command is None else {"command": command}}
    # The hooks read a flat ``"command": "..."`` anywhere in the payload.
    done = subprocess.run(
        ["bash", "-c", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        check=True,
    )
    return json.loads(done.stdout)


def _stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)


class TestThePrePushReview:
    @pytest.mark.parametrize("command", [None, "ls -la", "git status", "git commit -m x"])
    def test_anything_that_is_not_a_push_or_a_pr_passes_through(self, tmp_path: Path, command):
        repo = _repo(tmp_path)
        _stage(repo, "a.py", "# TODO later\nprint('x')\n")
        assert _run(PRE_PR_REVIEW_HOOK, repo, command) == {"continue": True}

    @pytest.mark.parametrize(
        "command", ["git push origin main", "gh pr create --fill", "gh pr view"]
    )
    def test_a_clean_push_continues_without_a_message(self, tmp_path: Path, command: str):
        repo = _repo(tmp_path)
        _stage(repo, "a.py", "value = 1\n")
        assert _run(PRE_PR_REVIEW_HOOK, repo, command) == {"continue": True}

    def test_an_empty_diff_continues_without_a_message(self, tmp_path: Path):
        assert _run(PRE_PR_REVIEW_HOOK, _repo(tmp_path), "git push") == {"continue": True}

    def test_a_push_with_todos_and_debug_statements_continues_with_a_warning(self, tmp_path: Path):
        repo = _repo(tmp_path)
        _stage(repo, "a.py", "# TODO one\n# FIXME two\nbreakpoint()\n")
        out = _run(PRE_PR_REVIEW_HOOK, repo, "git push origin main")
        assert out["continue"] is True
        assert "2 TODO/FIXME comment(s)" in out["systemMessage"]
        assert "1 debug statement(s)" in out["systemMessage"]
        assert out["systemMessage"].startswith("Before pushing: found ")

    @pytest.mark.parametrize("command", ["git push", "gh pr create --fill", "gh pr view"])
    def test_every_push_or_pr_command_is_reviewed(self, tmp_path: Path, command: str):
        repo = _repo(tmp_path)
        _stage(repo, "a.py", "# HACK\n")
        out = _run(PRE_PR_REVIEW_HOOK, repo, command)
        assert "1 TODO/FIXME comment(s)" in out["systemMessage"]

    def test_only_added_lines_count(self, tmp_path: Path):
        repo = _repo(tmp_path)
        _stage(repo, "a.py", "# TODO old\nvalue = 1\n")
        subprocess.run([*_GIT, "-C", str(repo), "commit", "-q", "-m", "first"], check=True)
        (repo / "a.py").write_text("value = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
        assert _run(PRE_PR_REVIEW_HOOK, repo, "git push") == {"continue": True}


class TestThePostCommitTodoHook:
    def _commit(self, repo: Path, message: str) -> None:
        _stage(repo, "a.txt", "one\n")
        subprocess.run([*_GIT, "-C", str(repo), "commit", "-q", "-m", message], check=True)

    @pytest.mark.parametrize("command", [None, "ls", "git push"])
    def test_anything_but_a_commit_passes_through(self, tmp_path: Path, command):
        repo = _repo(tmp_path)
        (repo / ".igni").mkdir()
        (repo / ".igni" / "TODO.md").write_text("- [ ] x\n")
        assert _run(POST_COMMIT_TODO_HOOK, repo, command) == {"continue": True}

    def test_a_commit_without_a_todo_file_passes_through(self, tmp_path: Path):
        repo = _repo(tmp_path)
        self._commit(repo, "first")
        assert _run(POST_COMMIT_TODO_HOOK, repo, "git commit -m first") == {"continue": True}

    def test_a_commit_with_a_todo_file_sends_the_commit_as_one_json_string(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / ".igni").mkdir()
        (repo / ".igni" / "TODO.md").write_text("- [ ] x\n")
        self._commit(repo, 'say "hi" \\ and more')
        out = _run(POST_COMMIT_TODO_HOOK, repo, "git commit -m x")
        assert out["continue"] is True
        message = out["systemMessage"]
        assert isinstance(message, str)
        assert "update .igni/TODO.md" in message
        assert 'Commit: say "hi" \\ and more' in message


class TestTheScriptsAreValidBash:
    @pytest.mark.parametrize("script", [PRE_PR_REVIEW_HOOK, POST_COMMIT_TODO_HOOK])
    def test_bash_parses_each_one(self, script: str):
        done = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr

    @pytest.mark.parametrize("script", [PRE_PR_REVIEW_HOOK, POST_COMMIT_TODO_HOOK])
    def test_each_starts_with_a_shebang(self, script: str):
        assert script.startswith("#!/bin/bash\n")


class TestTheStarterFiles:
    def test_the_project_config_parses_and_turns_guardrails_and_learning_on(self):
        config = yaml.safe_load(PROJECT_CONFIG_TEMPLATE)
        assert config == {"guardrails": {"pii_detection": True}, "learning": {"enabled": True}}

    def test_the_two_config_texts_that_carry_no_settings_parse_to_nothing(self):
        assert yaml.safe_load(CONFIG_YAML_HEADER) is None
        assert yaml.safe_load(_HOME_CONFIG_BOOTSTRAP) is None

    def test_the_context_file_has_its_sections_in_order(self):
        headings = re.findall(r"^#+ .*$", IGNI_MD_TEMPLATE, flags=re.M)
        assert headings == [
            "# Project Context",
            "## Overview",
            "## Tech Stack",
            "## Architecture",
            "## Conventions",
            "## Domain Terminology",
        ]

    def test_the_headers_say_where_the_file_lives(self):
        assert "~/.igni/config.yaml" in CONFIG_YAML_HEADER
        assert ".igni/config.yaml" in PROJECT_CONFIG_TEMPLATE
