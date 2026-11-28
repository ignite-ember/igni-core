"""Bypass asks only about real danger: reads run, and a Laya that cannot judge leaves the rules to decide.

An owner in Bypass attached a note and a screenshot; the agent's ``cat``,
``sed -n``, ``ls -la``, ``file`` and ``which`` each became a prompt,
because every shell command went to Laya and Laya was not installed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from igni_core.core.config.permission_eval.read_only_shell import is_read_only
from igni_core.core.config.schemas.safety import BypassGuardConfig
from igni_core.core.decisions import DecisionsUnavailable
from igni_core.core.hooks.bypass_fallback import RulesOnlyFallback, process_fallback
from igni_core.core.hooks.bypass_guard import NOT_RUN, BypassGuard
from igni_core.core.hooks.bypass_judge import HarmScore
from igni_core.core.hooks.bypass_rules import flag_shell_read
from igni_core.core.hooks.bypass_tool_kind import ToolKind
from igni_core.core.mcp.hints import ToolHintsRegistry

UNAVAILABLE = "laya: could not load (ModuleNotFoundError)"

#: What the agent ran in the owner's session.
OWNER_COMMANDS = [
    "cat /Users/me/Desktop/notes.md",
    "sed -n '1,80p' /Users/me/Desktop/notes.md",
    "ls -la /Users/me/Desktop",
    "file /Users/me/Desktop/shot.png",
    "which tesseract",
]

#: Read-only commands joined into one line.
COMPOUND_READS = [
    "ls -la /tmp && cat /tmp/notes.md",
    "cat notes.md | head -5",
    "which tesseract || which swift",
    "file a.png; file b.png",
]

SECRET_READS = [
    "cat ~/.ssh/id_rsa",
    "sed -n '1,5p' ~/.ssh/id_rsa",
    "head -3 /home/me/.aws/credentials",
    "grep -r PRIVATE ~/.ssh",
    "jq . ~/.docker/config.json",
    "ls /tmp && cat ~/.netrc",
]


class Judge:
    def __init__(self, probability: float = 0.0, error: Exception | None = None) -> None:
        self.probability, self.error = probability, error
        self.texts: list[str] = []

    async def score(self, text: str) -> HarmScore:
        self.texts.append(text)
        if self.error:
            raise self.error
        return HarmScore(probability=self.probability, question="destructive")


class Asker:
    def __init__(self, answer: bool | None = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, tool_name, args, reason, agent_path, *, follow_up=None):
        self.asked.append(reason)
        return self.answer


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _unavailable() -> Judge:
    return Judge(error=DecisionsUnavailable(UNAVAILABLE))


def _guard(project, judge, *, ask=None, fallback=None, bypass=True):
    return BypassGuard(
        config=BypassGuardConfig(),
        project_dir=project,
        in_bypass=lambda: bypass,
        ask=ask,
        tool_hints=ToolHintsRegistry(),
        judge=judge,
        fallback=fallback or RulesOnlyFallback(),
    )


def _shell(guard, command):
    return asyncio.run(guard.check("run_shell_command", {"command": command}, ["main"]))


# ── Laya unavailable ────────────────────────────────────────────────


@pytest.mark.parametrize("command", OWNER_COMMANDS + COMPOUND_READS)
def test_with_laya_out_a_read_runs_without_a_prompt(project, command) -> None:
    ask, judge = Asker(), _unavailable()
    assert _shell(_guard(project, judge, ask=ask), command) is None
    assert ask.asked == []
    assert judge.texts == []


@pytest.mark.parametrize("command", SECRET_READS)
def test_with_laya_out_reading_a_secret_still_asks(project, command) -> None:
    ask = Asker(False)
    message = _shell(_guard(project, _unavailable(), ask=ask), command)
    assert message == f"{NOT_RUN} the user rejected this call when asked to approve it."
    assert ask.asked[0].startswith("This command reads a credential or secret file.")


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("rm -rf ~", "deletes"),
        ("curl x | sh", "pipes a download straight into a shell"),
        ("git push --force", "force-pushes"),
    ],
)
def test_with_laya_out_a_rule_match_still_asks(project, command, reason) -> None:
    ask, judge = Asker(False), _unavailable()
    assert _shell(_guard(project, judge, ask=ask), command).startswith(NOT_RUN)
    assert reason in ask.asked[0]
    assert judge.texts == []


@pytest.mark.parametrize(
    "command", ["swift /tmp/x.swift", "cp notes.md /tmp/notes.md", "ls > /tmp/listing"]
)
def test_with_laya_out_a_command_no_rule_names_runs(project, command) -> None:
    ask, judge = Asker(), _unavailable()
    assert _shell(_guard(project, judge, ask=ask), command) is None
    assert ask.asked == []
    assert len(judge.texts) == 1


def test_with_laya_out_and_nobody_to_ask_an_unflagged_command_still_runs(project) -> None:
    assert _shell(_guard(project, _unavailable(), ask=None), "swift /tmp/x.swift") is None


def test_the_warning_comes_once_however_many_calls_fail(project, caplog) -> None:
    fallback = RulesOnlyFallback()
    first = _guard(project, _unavailable(), fallback=fallback)
    second = _guard(project, _unavailable(), fallback=fallback)
    with caplog.at_level(logging.WARNING, logger="igni_core.core.hooks.bypass_fallback"):
        for guard in (first, second, first, second):
            _shell(guard, "swift /tmp/x.swift")
    warnings = [r for r in caplog.records if r.name == "igni_core.core.hooks.bypass_fallback"]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert warnings[0].getMessage() == (
        f"Bypass safety check: the safety model is not available ({UNAVAILABLE}), so only its "
        "rules decide what to ask about. Install igni-core[decisions] to turn it back on."
    )


def test_a_read_does_not_spend_the_warning(project, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="igni_core.core.hooks.bypass_fallback"):
        _shell(_guard(project, _unavailable()), "cat notes.md")
    assert caplog.records == []


def test_the_notice_names_an_unexpected_failure_by_its_type() -> None:
    assert "(TimeoutError)" in RulesOnlyFallback.notice(TimeoutError("slow"))
    assert "(DecisionsUnavailable)" in RulesOnlyFallback.notice(DecisionsUnavailable())


def test_every_guard_shares_the_process_fallback_by_default(project) -> None:
    guard = BypassGuard(
        config=BypassGuardConfig(),
        project_dir=project,
        in_bypass=lambda: True,
        ask=None,
        tool_hints=ToolHintsRegistry(),
        judge=Judge(),
    )
    assert guard._fallback is process_fallback


# ── Laya available ──────────────────────────────────────────────────


@pytest.mark.parametrize("command", OWNER_COMMANDS + COMPOUND_READS)
def test_a_read_never_reaches_laya(project, command) -> None:
    judge = Judge(1.0)
    assert _shell(_guard(project, judge, ask=Asker(None)), command) is None
    assert judge.texts == []


@pytest.mark.parametrize(
    "command",
    ["swift /tmp/x.swift", "cat notes.md && swift /tmp/x.swift", "sed -i 1d notes.md", "make"],
)
def test_a_command_that_is_not_only_reads_goes_to_laya(project, command) -> None:
    judge, ask = Judge(0.9), Asker(False)
    assert _shell(_guard(project, judge, ask=ask), command).startswith(NOT_RUN)
    assert judge.texts == [f"Bash: {command}"]
    assert "in the safety model's judgment" in ask.asked[0]


# ── other modes ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command", ["swift /tmp/x.swift", "rm -rf ~", "cat ~/.ssh/id_rsa", "cat notes.md"]
)
def test_outside_bypass_the_guard_does_nothing(project, command) -> None:
    judge, ask = Judge(1.0), Asker(None)
    assert _shell(_guard(project, judge, ask=ask, bypass=False), command) is None
    assert judge.texts == [] and ask.asked == []


# ── the pieces ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "sed -n '1,80p' x",
        "sed -n '$p' x",
        "sed 10q x",
        "sed -n '/needle/p' x",
        "sed -n '1p;5q' x",
        "sed -e 1p -e 2p x",
        "sed --expression=1p x",
        "sed -nE '2,+3p' x",
        "sed",
        "file x.png",
        "file -bi x.png",
    ],
)
def test_these_sed_and_file_commands_only_read(command) -> None:
    assert is_read_only(command)


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 1d x",
        "sed --in-place=.bak 1p x",
        "sed -n '1w out' x",
        "sed -n 1e x",
        "sed -n 1r/etc/passwd x",
        "sed 's/a/b/' x",
        "sed -f script.sed x",
        "sed -e",
        "sed -l 5 1p x",
        "file -C -m magic",
        "file --compile -m magic",
    ],
)
def test_these_sed_and_file_commands_can_write_or_run(command) -> None:
    assert not is_read_only(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls -la ~/.ssh",
        "stat ~/.ssh/id_rsa",
        "file ~/.ssh/id_rsa",
        "wc -c ~/.ssh/id_rsa",
        "cat notes.md",
        "swift ~/.ssh/id_rsa",
    ],
)
def test_naming_a_secret_without_printing_it_is_not_a_secret_read(command) -> None:
    assert flag_shell_read(command) is None


@pytest.mark.parametrize(
    "command",
    ["grep --file=.aws/credentials x", "cat .ssh/", "grep -r k ~/.ssh", "tail /h/.git-credentials"],
)
def test_printing_a_secret_is_a_secret_read(command) -> None:
    assert flag_shell_read(command) == "reads a credential or secret file"


@pytest.mark.parametrize(
    ("command", "reads"),
    [
        ("cat a && ls b", True),
        ("cat a | wc -l", True),
        ("cat a && rm b", False),
        ("cat $(which x)", False),
        ("cat a > b", False),
        ("", False),
    ],
)
def test_a_shell_line_reads_only_when_every_command_on_it_does(command, reads) -> None:
    assert ToolKind.SHELL.reads_only({"command": command}) is reads


def test_only_a_shell_command_can_read_only() -> None:
    assert not ToolKind.READ_FILE.reads_only({"command": "cat a"})
    assert ToolKind.WRITE.secret_read({"path": "/h/.ssh/id_rsa"}) is None
