"""``BypassGuard`` and ``Verdict``: the exact decision for each kind of call.

The existing Bypass tests drive the flows; these pin the boundaries the
guard decides on — the threshold, which tools are read-only, the secret
read, a judge that fails, the exemption for an extension's local read —
and the words the person and the agent read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from igni_core.core.config.schemas.safety import BypassGuardConfig
from igni_core.core.hooks.bypass_guard import NOT_RUN, READ_ONLY_TOOLS, BypassGuard, Verdict
from igni_core.core.hooks.bypass_judge import HarmJudge, HarmScore
from igni_core.core.hooks.bypass_tool_kind import ToolKind
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry


class Judge:
    def __init__(self, probability=0.0, question="destructive", error=None):
        self.probability, self.question, self.error = probability, question, error
        self.texts: list[str] = []

    async def score(self, text):
        self.texts.append(text)
        if self.error:
            raise self.error
        return HarmScore(probability=self.probability, question=self.question)


class Asker:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def __call__(self, tool_name, args, reason, agent_path, *, follow_up=None):
        self.asked.append({"tool": tool_name, "reason": reason, "path": agent_path})
        self.follow_up = follow_up
        return self.answer


class Explainer:
    def __init__(self, sentence):
        self.sentence, self.calls = sentence, []

    async def explain(self, tool_name, args, flagged_for):
        self.calls.append((tool_name, args, flagged_for))
        return self.sentence


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _guard(project, *, ask=None, judge=None, hints=None, explainer=None, bypass=True, **config):
    return BypassGuard(
        config=BypassGuardConfig(**config),
        project_dir=project,
        in_bypass=lambda: bypass,
        ask=ask,
        tool_hints=hints or ToolHintsRegistry(),
        judge=judge,
        explainer=explainer,
    )


def _check(guard, tool, args=None, path=None):
    return asyncio.run(guard.check(tool, args or {}, path))


# ── the threshold ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("probability", "asked"), [(0.74, False), (0.75, True), (0.76, True), (1.0, True), (0.0, False)]
)
def test_a_score_at_the_threshold_asks_and_one_below_does_not(project, probability, asked) -> None:
    ask = Asker(True)
    guard = _guard(project, ask=ask, judge=Judge(probability))
    assert _check(guard, "mystery_tool", {"x": 1}) is None
    assert bool(ask.asked) is asked


def test_a_lowered_threshold_is_honoured(project) -> None:
    ask = Asker(True)
    _check(_guard(project, ask=ask, judge=Judge(0.5), threshold=0.5), "mystery_tool")
    assert len(ask.asked) == 1


def test_a_judged_call_tells_the_person_the_score_and_the_model_reason(project) -> None:
    ask = Asker(False)
    guard = _guard(project, ask=ask, judge=Judge(0.8, "exfiltration"))
    _check(guard, "mystery_tool")
    assert ask.asked[0]["reason"] == (
        "This call might send secrets or private data elsewhere, in the safety model's judgment "
        "(risk score 0.80 of 1). Bypass mode skips approvals, but the safety check stops here "
        "so you decide."
    )


def test_the_judge_is_shown_the_call_as_laya_was_measured_on_it(project) -> None:
    judge = Judge()
    _check(_guard(project, judge=judge), "run_shell_command", {"command": "make all"})
    _check(_guard(project, judge=judge), "mystery", {"a": 1})
    assert judge.texts == ["Bash: make all", 'mystery: {"a": 1}']


# ── rules, no model, a failing model ────────────────────────────────


def test_a_rule_decides_without_asking_the_judge(project) -> None:
    judge = Judge()
    message = _check(
        _guard(project, ask=Asker(None), judge=judge), "run_shell_command", {"command": "rm -rf ~"}
    )
    assert message.startswith(NOT_RUN)
    assert judge.texts == []


def test_with_the_model_switched_off_only_rules_apply(project) -> None:
    guard = _guard(project, ask=Asker(None), use_model=False)
    assert guard._judge is None
    assert _check(guard, "mystery_tool", {"x": 1}) is None
    assert _check(guard, "run_shell_command", {"command": "git push -f"}).startswith(NOT_RUN)


def test_with_the_model_on_and_no_judge_given_the_guard_builds_the_real_one(project) -> None:
    assert isinstance(_guard(project)._judge, HarmJudge)


def test_a_judge_that_fails_asks_and_names_the_failure_to_the_person(project) -> None:
    ask = Asker(True)
    guard = _guard(project, ask=ask, judge=Judge(error=RuntimeError("model not loaded")))
    assert _check(guard, "mystery_tool") is None
    assert ask.asked[0]["reason"] == (
        "This call could not be checked for safety. Bypass mode skips approvals, but the "
        "safety check stops here so you decide."
    )


def test_a_judge_that_fails_with_nobody_to_ask_refuses_the_call(project) -> None:
    guard = _guard(project, ask=None, judge=Judge(error=ValueError("boom")))
    assert _check(guard, "mystery_tool") == (
        f"{NOT_RUN} this call needed the user's approval because it could not be checked "
        "for safety, and nobody was available to give it."
    )


# ── when it does nothing ────────────────────────────────────────────


def test_a_disabled_guard_lets_a_dangerous_call_run(project) -> None:
    guard = _guard(project, ask=Asker(None), judge=Judge(1.0), enabled=False)
    assert _check(guard, "run_shell_command", {"command": "rm -rf ~"}) is None


def test_outside_bypass_even_a_secret_read_is_left_to_the_other_modes(project) -> None:
    guard = _guard(project, ask=Asker(None), bypass=False)
    assert _check(guard, "read_file", {"file_path": "/home/me/.ssh/id_rsa"}) is None


@pytest.mark.parametrize("tool", sorted(READ_ONLY_TOOLS))
def test_a_read_only_tool_runs_without_a_check(project, tool) -> None:
    judge = Judge(1.0)
    assert _check(_guard(project, ask=Asker(None), judge=judge), tool, {"x": 1}) is None
    assert judge.texts == []


def test_the_read_only_tools_are_exactly_these() -> None:
    assert set(READ_ONLY_TOOLS) == set(
        {
            "read_file",
            "list_files",
            "list_directory",
            "grep",
            "glob",
            "search_files",
            "read_process_output",
            "await_process",
            "watch_process",
            "list_processes",
            "loop_status",
            "loop_progress_get",
            "loop_progress_list",
            "monitor_status",
            "monitor_output",
            "list_scheduled_tasks",
            "lsp_query",
            "lsp_list_servers",
            "web_search",
            "todo_write",
            "visualize",
            "spawn_agent",
            "spawn_team",
        }
    )


@pytest.mark.parametrize("tool", ["run_shell_command", "write_file", "fetch_url", "browser_click"])
def test_a_tool_that_is_not_read_only_is_checked(project, tool) -> None:
    judge = Judge(1.0)
    ask = Asker(False)
    message = _check(_guard(project, ask=ask, judge=judge), tool, {"command": "x"})
    assert message == f"{NOT_RUN} the user rejected this call when asked to approve it."
    assert len(judge.texts) == 1


# ── a secret read ───────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["read_file", "search_files"])
def test_a_read_only_tool_reading_a_secret_is_put_to_the_person(project, tool) -> None:
    ask, judge = Asker(True), Judge(0.0)
    assert (
        _check(_guard(project, ask=ask, judge=judge), tool, {"file_path": "/h/.ssh/id_rsa"}) is None
    )
    assert ask.asked[0]["reason"].startswith("This file read reads a credential or secret file.")
    assert judge.texts == []


def test_a_secret_read_with_nobody_to_ask_is_refused(project) -> None:
    message = _check(_guard(project), "read_file", {"path": "/home/me/.aws/credentials"})
    assert message == (
        f"{NOT_RUN} this call needed the user's approval because it reads a credential or "
        "secret file, and nobody was available to give it."
    )


def test_an_ordinary_read_runs_and_a_non_read_tool_naming_a_secret_path_uses_its_own_checks(
    project,
) -> None:
    assert _check(_guard(project, ask=Asker(None)), "read_file", {"file_path": "a.py"}) is None
    # ``grep`` is read-only but not a path-taking file tool, so no secret read applies.
    assert _check(_guard(project, ask=Asker(None)), "grep", {"path": "/h/.ssh/id_rsa"}) is None


# ── extension tools ─────────────────────────────────────────────────


def _hints(**overrides) -> ToolHints:
    fields = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    fields.update(overrides)
    return ToolHints(**fields)


def test_an_extension_tool_that_only_reads_its_own_data_runs_unchecked(project) -> None:
    registry = ToolHintsRegistry()
    registry.declare("srv", {"srv__lookup": _hints()})
    judge = Judge(1.0)
    guard = _guard(project, ask=Asker(None), judge=judge, hints=registry)
    assert _check(guard, "srv__lookup", {"q": "x"}) is None
    assert judge.texts == []


@pytest.mark.parametrize(
    "hints",
    [
        _hints(openWorldHint=True),
        _hints(readOnlyHint=False),
        _hints(readOnlyHint=False, destructiveHint=True),
    ],
)
def test_an_extension_tool_that_reaches_out_or_writes_is_checked(project, hints) -> None:
    registry = ToolHintsRegistry()
    registry.declare("srv", {"srv__act": hints})
    judge = Judge(1.0)
    message = _check(_guard(project, judge=judge, hints=registry), "srv__act")
    assert message.startswith(NOT_RUN)
    assert len(judge.texts) == 1


def test_an_extension_tool_with_no_hints_is_checked(project) -> None:
    judge = Judge(1.0)
    assert _check(_guard(project, judge=judge), "srv__unknown").startswith(NOT_RUN)


# ── the answer ──────────────────────────────────────────────────────


def test_the_agent_path_is_passed_on_as_a_copy(project) -> None:
    ask = Asker(True)
    caller_path = ["main", "reviewer"]
    _check(_guard(project, ask=ask, judge=Judge(1.0)), "mystery_tool", path=caller_path)
    assert ask.asked[0]["path"] == ["main", "reviewer"]
    assert ask.asked[0]["path"] is not caller_path


def test_no_agent_path_is_an_empty_list(project) -> None:
    ask = Asker(True)
    _check(_guard(project, ask=ask, judge=Judge(1.0)), "mystery_tool")
    assert ask.asked[0]["path"] == []


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (True, None),
        (False, f"{NOT_RUN} the user rejected this call when asked to approve it."),
        (
            None,
            f"{NOT_RUN} this call needed the user's approval because it force-pushes, rewriting "
            "shared history, and nobody was available to give it.",
        ),
    ],
)
def test_each_answer_has_its_outcome(project, answer, outcome) -> None:
    guard = _guard(project, ask=Asker(answer))
    assert _check(guard, "run_shell_command", {"command": "git push --force"}) == outcome


def test_the_refusal_prefix_is_the_one_the_protocol_recognises() -> None:
    assert NOT_RUN == "Not run:"


# ── the follow-up explanation ───────────────────────────────────────


def test_without_an_explainer_the_question_has_no_follow_up(project) -> None:
    ask = Asker(True)
    _check(_guard(project, ask=ask), "run_shell_command", {"command": "git push -f"})
    assert ask.follow_up is None


def test_the_follow_up_explains_the_call_and_what_stopped_it(project) -> None:
    ask, explainer = Asker(True), Explainer("Overwrites main on origin")
    _check(
        _guard(project, ask=ask, explainer=explainer),
        "run_shell_command",
        {"command": "git push -f"},
    )
    text = asyncio.run(ask.follow_up())
    assert text == (
        "Overwrites main on origin. Bypass paused here because a safety rule matched "
        "(force-pushes, rewriting shared history)."
    )
    assert explainer.calls == [
        ("run_shell_command", {"command": "git push -f"}, "force-pushes, rewriting shared history")
    ]


def test_the_follow_up_keeps_the_first_text_when_the_explainer_has_nothing(project) -> None:
    ask = Asker(True)
    _check(
        _guard(project, ask=ask, explainer=Explainer(None)),
        "run_shell_command",
        {"command": "git push -f"},
    )
    assert asyncio.run(ask.follow_up()) is None


# ── Verdict ─────────────────────────────────────────────────────────


def test_a_verdict_is_scored_only_when_judged_and_it_has_a_score() -> None:
    assert Verdict(needs_approval=True, reason="r", score=0.9, judged=True).scored
    assert not Verdict(needs_approval=True, reason="r", judged=True).scored
    assert not Verdict(needs_approval=True, reason="r", score=0.9).scored
    assert not Verdict(needs_approval=False, score=0.2).scored


@pytest.mark.parametrize(
    ("kind", "subject"),
    [
        ("run_shell_command", "This command"),
        ("write_file", "This file write"),
        ("fetch_url", "This request"),
        ("read_file", "This file read"),
        ("other", "This call"),
    ],
)
def test_the_prompt_names_the_call_by_its_kind(kind, subject) -> None:
    prompt = Verdict(needs_approval=True, reason="does a thing").prompt(ToolKind.of(kind))
    assert prompt == (
        f"{subject} does a thing. Bypass mode skips approvals, but the safety check stops "
        "here so you decide."
    )


def test_explained_says_what_the_basis_was() -> None:
    rule = Verdict(needs_approval=True, reason="deletes files as root")
    assert rule.explained("Removes a file") == (
        "Removes a file. Bypass paused here because a safety rule matched (deletes files as root)."
    )
    model = Verdict(
        needs_approval=True, reason="might delete or overwrite data", score=0.8, judged=True
    )
    assert model.explained("Wipes it") == (
        "Wipes it. Bypass paused here because the safety model rated it 0.80 of 1 "
        "(might delete or overwrite data)."
    )
    unchecked = Verdict(needs_approval=True, reason="could not be checked for safety")
    assert unchecked.explained("Runs it") == (
        "Runs it. Bypass paused here because the call could not be checked for safety."
    )


@pytest.mark.parametrize("sentence", ["Done.", "Done!", "Done?"])
def test_a_sentence_that_already_ends_in_punctuation_is_not_given_another(sentence) -> None:
    text = Verdict(needs_approval=True, reason="r").explained(sentence)
    assert text.startswith(f"{sentence} Bypass paused")
