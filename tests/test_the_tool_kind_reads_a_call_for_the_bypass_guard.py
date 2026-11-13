"""``ToolKind``: how Bypass's last check classifies a call, names it and shows it to Laya.

``describe`` is the text Laya's threshold was measured on, so its exact
shape and cut-offs are pinned here, as are the argument aliases each
kind accepts and the rule each kind runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.hooks.bypass_tool_kind import ToolKind


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("run_shell_command", ToolKind.SHELL),
        ("bash", ToolKind.SHELL),
        ("shell", ToolKind.SHELL),
        ("edit_file", ToolKind.WRITE),
        ("edit_file_replace_all", ToolKind.WRITE),
        ("create_file", ToolKind.WRITE),
        ("write_file", ToolKind.WRITE),
        ("fetch_url", ToolKind.FETCH),
        ("fetch_json", ToolKind.FETCH),
        ("read_file", ToolKind.READ_FILE),
        ("search_files", ToolKind.READ_FILE),
        ("grep", ToolKind.OTHER),
        ("mcp__srv__tool", ToolKind.OTHER),
        ("", ToolKind.OTHER),
        ("Bash", ToolKind.OTHER),
    ],
)
def test_a_tool_name_is_classified_by_its_exact_name(name, kind) -> None:
    assert ToolKind.of(name) is kind


@pytest.mark.parametrize(
    ("kind", "subject"),
    [
        (ToolKind.SHELL, "This command"),
        (ToolKind.WRITE, "This file write"),
        (ToolKind.FETCH, "This request"),
        (ToolKind.READ_FILE, "This file read"),
        (ToolKind.OTHER, "This call"),
    ],
)
def test_each_kind_names_itself_in_the_dialog(kind, subject) -> None:
    assert kind.subject == subject


@pytest.mark.parametrize(
    ("kind", "args", "target"),
    [
        (ToolKind.SHELL, {"command": "ls"}, "ls"),
        (ToolKind.SHELL, {"cmd": "ls"}, "ls"),
        (ToolKind.SHELL, {"command": "a", "cmd": "b"}, "a"),
        (ToolKind.SHELL, {"command": "", "cmd": "b"}, "b"),
        (ToolKind.WRITE, {"path": "p", "file_path": "f"}, "p"),
        (ToolKind.WRITE, {"file_path": "f"}, "f"),
        (ToolKind.FETCH, {"url": "https://x"}, "https://x"),
        (ToolKind.READ_FILE, {"path": "p", "file_path": "f"}, "f"),
        (ToolKind.READ_FILE, {"path": "p"}, "p"),
        (ToolKind.OTHER, {"command": "ls", "path": "p"}, ""),
        (ToolKind.SHELL, {}, ""),
        (ToolKind.SHELL, {"command": None}, ""),
        (ToolKind.FETCH, {"path": "p"}, ""),
        (ToolKind.SHELL, {"command": 42}, "42"),
    ],
)
def test_the_target_is_the_one_argument_the_kind_is_about(kind, args, target) -> None:
    assert kind.target(args) == target


def test_only_a_file_read_of_a_secret_path_is_a_secret_read() -> None:
    secret = {"file_path": "/home/me/.ssh/id_rsa"}
    assert ToolKind.READ_FILE.secret_read(secret) == "reads a credential or secret file"
    assert ToolKind.READ_FILE.secret_read({"path": "/home/me/.aws/credentials"})
    assert ToolKind.READ_FILE.secret_read({"file_path": "src/main.py"}) is None
    assert ToolKind.READ_FILE.secret_read({}) is None
    for kind in (ToolKind.SHELL, ToolKind.WRITE, ToolKind.FETCH, ToolKind.OTHER):
        assert kind.secret_read({"command": "cat ~/.ssh/id_rsa", "path": "/x/.ssh/id_rsa"}) is None


def test_each_kind_runs_its_own_rule_and_a_read_or_other_call_has_none(tmp_path: Path) -> None:
    assert ToolKind.SHELL.rule({"command": "git push --force"}, tmp_path) == (
        "force-pushes, rewriting shared history"
    )
    assert ToolKind.SHELL.rule({"cmd": "rm -rf /"}, tmp_path) == "deletes /, outside this project"
    assert ToolKind.WRITE.rule({"file_path": "/etc/hosts"}, tmp_path) == (
        "writes /etc/hosts, outside this project"
    )
    assert ToolKind.WRITE.rule({"path": "src/a.py"}, tmp_path) is None
    assert ToolKind.FETCH.rule({"url": "https://x.example/?token=abc"}, tmp_path) == (
        "sends what looks like a secret or token in a URL"
    )
    assert ToolKind.FETCH.rule({"url": "https://x.example/docs"}, tmp_path) is None
    # Neither of these has a shape rule, even for an obviously dangerous target.
    assert ToolKind.READ_FILE.rule({"file_path": "/home/me/.ssh/id_rsa"}, tmp_path) is None
    assert (
        ToolKind.OTHER.rule({"command": "rm -rf /", "url": "https://x/?token=1"}, tmp_path) is None
    )


def test_a_shell_write_and_fetch_call_are_shown_to_laya_as_tool_colon_argument() -> None:
    assert ToolKind.SHELL.describe("bash", {"command": "ls -la"}) == "Bash: ls -la"
    assert ToolKind.SHELL.describe("shell", {"cmd": "ls"}) == "Bash: ls"
    assert ToolKind.FETCH.describe("fetch_url", {"url": "https://x.example"}) == (
        "WebFetch: https://x.example"
    )
    assert ToolKind.READ_FILE.describe("read_file", {"file_path": "a.py"}) == "Read: a.py"


def test_a_write_is_shown_with_its_content_or_new_string_repr_cut_at_400() -> None:
    assert ToolKind.WRITE.describe("create_file", {"path": "a.py", "content": "x = 1\n"}) == (
        "Write: a.py  content: 'x = 1\\n'"
    )
    assert ToolKind.WRITE.describe("edit_file", {"path": "a.py", "new_string": "new"}) == (
        "Write: a.py  content: 'new'"
    )
    assert ToolKind.WRITE.describe(
        "edit_file", {"path": "a.py", "content": "c", "new_string": "n"}
    ) == ("Write: a.py  content: 'c'")
    assert ToolKind.WRITE.describe("create_file", {"path": "a.py"}) == "Write: a.py  content: ''"
    long = ToolKind.WRITE.describe("create_file", {"path": "a.py", "content": "é" * 500})
    assert long == f"Write: a.py  content: {'é' * 400!r}"


def test_any_other_call_is_shown_as_its_name_and_json_args_cut_at_1500() -> None:
    assert ToolKind.OTHER.describe("deploy", {"env": "prod", "n": 2}) == (
        'deploy: {"env": "prod", "n": 2}'
    )
    assert ToolKind.OTHER.describe("t", {"p": Path("/x")}) == 't: {"p": "/x"}'
    shown = ToolKind.OTHER.describe("t", {"blob": "a" * 3000})
    assert shown == "t: " + json.dumps({"blob": "a" * 3000})[:1500]
    assert len(shown) == len("t: ") + 1500
