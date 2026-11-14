"""``command_segments`` splits a shell line into the commands a rule must cover.

Each command comes back with the operator that joined it on (``None`` for the
first), or the whole result is ``None`` when nothing can vouch for the line.
Plan mode and ``--read-only`` run only the commands on the read-only
allow-list, so each member and each refused argument is pinned here.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.permission_eval.read_only_shell import (
    READ_ONLY_COMMANDS,
    command_parts,
    command_segments,
    is_read_only,
)


def test_each_command_comes_back_with_the_operator_that_joined_it():
    assert command_segments("ls | head && rm x || echo no; pwd") == [
        (None, "ls"),
        ("|", "head"),
        ("&&", "rm x"),
        ("||", "echo no"),
        (";", "pwd"),
    ]


def test_a_line_without_operators_is_returned_as_typed():
    assert command_segments("grep  -n   'a  b' x") == [(None, "grep  -n   'a  b' x")]


def test_a_quoted_operator_is_part_of_its_argument():
    assert command_segments("echo 'a && b' | wc") == [(None, "echo 'a && b'"), ("|", "wc")]


def test_a_harmless_redirect_is_not_an_operator():
    assert command_segments("ls 2>&1 | head") == [(None, "ls"), ("|", "head")]
    assert command_segments("ls >/dev/null") == [(None, "ls >/dev/null")]
    assert command_segments("ls >/dev/null | wc") == [(None, "ls"), ("|", "wc")]


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "ls &",
        "(ls)",
        "ls > out",
        "ls 2>file",
        "ls &>file",
        "ls < in",
        "ls && ",
        "&& ls",
        "ls | | wc",
        "ls ;; pwd",
        "&&",
        "|",
        "ls 'unterminated",
        "ls $(x)",
        "ls `x`",
        "ls 'a`b'",
        "ls\r",
        "ls\nrm x",
        "ls <(x)",
        "ls >(x)",
        "echo '$(x)'",
    ],
)
def test_a_line_nothing_can_vouch_for_has_no_segments(command: str):
    assert command_segments(command) is None
    assert command_parts(command) is None


def test_the_parts_are_the_segments_without_their_operators():
    assert command_parts("cat a | grep b && ls") == ["cat a", "grep b", "ls"]


@pytest.mark.parametrize(
    "command",
    [
        "stat x",
        "du -sh .",
        "df -h",
        "realpath x",
        "readlink x",
        "nl x",
        "cut -d , -f 1 x",
        "tr a b",
        "diff a b",
        "cmp a b",
        "wc -l x",
        "uname -a",
        "basename x",
        "dirname x",
        "which ls",
        "whoami",
        "jq . x",
        "printf hi",
        "egrep a x",
        "fgrep a x",
        "sort -r x",
        "sort -- -o",
        "git for-each-ref --format='%(refname)'",
        "git blame x",
        "git ls-files",
        "git rev-parse HEAD",
        "git branch --list 'f*'",
        "ls 2>/dev/null",
        "ls &>/dev/null | wc",
        "ls 2>&1",
        "ls >&2",
    ],
)
def test_these_read_commands_are_read_only(command: str):
    assert is_read_only(command)


def test_every_allow_listed_command_has_an_accepting_form():
    for name in READ_ONLY_COMMANDS:
        assert is_read_only(f"{name} status" if name == "git" else name)


@pytest.mark.parametrize(
    "command",
    [
        "rg --pre=cmd x",
        "rg --pre cmd x",
        "find . -fprint0 x",
        "find . -fls x",
        "find . -okdir rm {} ;",
        "find . -fprintf x y",
        "sort -o x y",
        "sort -ro x",
        "sort --outp=x y",
        "sort --compress-program=sh x",
        "git diff --output=x",
        "git log --ext-diff",
        "git grep -O x",
        "git grep --open-files-in-pager=sh x",
        "git branch newname",
        "git branch -d x",
        "git -c core.pager=sh log",
        "git -C / log",
        "git for-each-ref --format=%(refname)",
        "git",
        "git push",
        "ls | xargs rm",
        "sudo ls",
        "/bin/ls",
        "A=1 ls",
        "env ls",
        "echo hi >out",
        "echo hi 2>out",
        "ls; pwd",
        "ls || pwd",
        "ls && pwd",
        "ls &",
    ],
)
def test_these_are_not_read_only(command: str):
    assert not is_read_only(command)
