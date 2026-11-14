"""Each permission stage, and the pipeline that runs them, decided one call at a time.

The stages are driven directly with a recording hook executor, so what is
pinned is what each one returns, what it tells the model, and which hook
events it fires: the order the pipeline runs them in, the stages a hook's
``allow`` may skip, and the ones it may not.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from igni_core.core.approvals.declined import declined_note
from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.config.schemas.safety import BypassGuardConfig
from igni_core.core.hooks.bypass_guard import BypassGuard
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.blocked_command_stage import BlockedCommandStage
from igni_core.core.hooks.permission.bypass_guard_stage import BypassGuardStage
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.denial import Denial
from igni_core.core.hooks.permission.evaluator_stage import PermissionEvaluatorStage
from igni_core.core.hooks.permission.outcome import AllowSkip, Block, Continue
from igni_core.core.hooks.permission.pipeline import PermissionPipeline
from igni_core.core.hooks.permission.pre_tool_use_stage import PreToolUseHookStage
from igni_core.core.hooks.permission.protected_path_stage import ProtectedPathStage
from igni_core.core.hooks.permission.stage import PermissionStage
from igni_core.core.hooks.schemas import HookResult
from igni_core.core.hooks.schemas import PermissionDecision as HookDecision
from igni_core.core.mcp.hints import ToolHintsRegistry


class _Recorder(HookExecutor):
    """Answers each event with a scripted result and remembers what it was asked."""

    def __init__(self, **by_event: HookResult) -> None:
        super().__init__({})
        self.by_event = by_event
        self.fired: list[tuple[str, Any]] = []

    async def execute(self, event: str, payload: Any, target: str = "") -> HookResult:
        self.fired.append((event, payload))
        return self.by_event.get(event, HookResult())

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.fired]


def _firer(**by_event: HookResult) -> tuple[HookFirer, _Recorder]:
    recorder = _Recorder(**by_event)
    return HookFirer(recorder, "sess-1"), recorder


def _call(name: str, **args: Any) -> ToolCallContext:
    return ToolCallContext(name=name, func=None, args=args)


def _pre(decision: HookDecision, message: str = "", **kw: Any) -> HookResult:
    return HookResult(permission_decision=decision, message=message, **kw)


# ── outcome and stage ───────────────────────────────────────────────


def test_outcomes_are_frozen_and_block_carries_its_message() -> None:
    for outcome in (Continue(), Block("no"), AllowSkip()):
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.message = "x"  # type: ignore[attr-defined]
    assert Block("no").message == "no"
    assert Block("") == Block("")
    assert Continue() != AllowSkip()


def test_a_stage_without_evaluate_cannot_be_built() -> None:
    class Forgetful(PermissionStage):
        pass

    with pytest.raises(TypeError):
        Forgetful()  # type: ignore[abstract]


def test_only_the_evaluator_and_the_bypass_guard_yield_to_a_hook_allow() -> None:
    assert PermissionStage.skipped_by_hook_allow is False
    assert PreToolUseHookStage.skipped_by_hook_allow is False
    assert ProtectedPathStage.skipped_by_hook_allow is False
    assert BlockedCommandStage.skipped_by_hook_allow is False
    assert PermissionEvaluatorStage.skipped_by_hook_allow is True
    assert BypassGuardStage.skipped_by_hook_allow is True


# ── context ─────────────────────────────────────────────────────────


def test_a_context_is_frozen_and_for_check_has_no_callable_or_caller() -> None:
    ctx = ToolCallContext.for_check("save_file", {"file_name": "a"})
    assert (ctx.name, ctx.func, ctx.args, ctx.agent, ctx.team) == (
        "save_file",
        None,
        {"file_name": "a"},
        None,
        None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.name = "other"  # type: ignore[misc]


def test_with_args_returns_itself_for_the_same_dict_and_a_copy_for_another() -> None:
    ctx = _call("edit_file", file_path="a")
    assert ctx.with_args(ctx.args) is ctx
    new = {"file_path": "b"}
    changed = ctx.with_args(new)
    assert changed is not ctx and changed.args == new and changed.args is not new
    assert ctx.args == {"file_path": "a"}
    assert changed.name == "edit_file"
    equal = ctx.with_args({"file_path": "a"})
    assert equal is not ctx and equal.args == ctx.args


class _Owner:
    def __init__(self, name: str | None, tools: Any) -> None:
        self.name = name
        self.tools = tools


def test_agent_path_is_the_agent_name_or_empty() -> None:
    def ctx(agent: Any) -> ToolCallContext:
        return ToolCallContext(name="t", func=None, args={}, agent=agent)

    assert ctx(_Owner("main", [])).agent_path == ["main"]
    assert ctx(_Owner("", [])).agent_path == []
    assert ctx(_Owner(None, [])).agent_path == []
    assert ctx(None).agent_path == []


def _noop() -> str:
    return ""


async def _anoop() -> str:
    return ""


def test_built_with_pause_reads_functions_toolkits_async_and_the_team() -> None:
    paused = Function(name="paused_fn", entrypoint=_noop, requires_confirmation=True)
    plain = Function(name="plain_fn", entrypoint=_noop)
    unpaused_kit = Toolkit(name="kit", tools=[_noop])
    listed_kit = Toolkit(name="kit", tools=[_noop], requires_confirmation_tools=["_noop"])
    async_kit = Toolkit(name="akit", tools=[_anoop])

    def pause(name: str, *tools: Any, agent_tools: Any = None, team: Any = None) -> bool | None:
        agent = _Owner("main", list(tools) if agent_tools is None else agent_tools)
        return ToolCallContext(
            name=name, func=None, args={}, agent=agent, team=team
        ).built_with_pause()

    assert pause("paused_fn", paused, plain, "not a tool") is True
    assert pause("plain_fn", paused, plain) is False
    assert pause("_noop", unpaused_kit) is False
    assert pause("_noop", listed_kit) is True
    assert pause("_anoop", async_kit) is False
    assert pause("missing", paused, unpaused_kit) is None
    # a tool only the team holds
    assert pause("paused_fn", team=_Owner("team", [paused])) is True
    # tools that are not a list, or no owner at all
    not_a_list = ToolCallContext(
        name="paused_fn",
        func=None,
        args={},
        agent=_Owner("main", None),
        team=_Owner("t", (paused,)),
    )
    assert not_a_list.built_with_pause() is None
    assert ToolCallContext(name="paused_fn", func=None, args={}).built_with_pause() is None


# ── denial ──────────────────────────────────────────────────────────


def test_a_denial_words_a_policy_refusal_with_its_reason_and_the_shell_advice() -> None:
    shell = Denial("run_shell_command", "plan mode blocks it.")
    assert shell.has_alternatives and not Denial("edit_file").has_alternatives
    off = shell.policy_message(tool_is_off_entirely=True)
    assert off.startswith(
        "Blocked: permission policy denied 'run_shell_command'. plan mode blocks it."
    )
    assert off.endswith(Denial.ALTERNATIVES["run_shell_command"])
    assert shell.policy_message(tool_is_off_entirely=False) == (
        "Blocked: permission policy denied 'run_shell_command'. plan mode blocks it."
    )
    assert Denial("edit_file").policy_message(tool_is_off_entirely=True) == (
        "Blocked: permission policy denied 'edit_file'."
    )
    assert Denial("run_shell_command").policy_message(tool_is_off_entirely=False) == (
        "Blocked: permission policy denied 'run_shell_command'."
    )


def test_a_denial_words_an_unasked_call_with_and_without_a_reason() -> None:
    tail = (
        ", and this session has no way to ask for it here. Ask the person in your "
        "reply instead of retrying. In plan mode, put the step in your plan and call "
        "exit_plan_mode(plan)."
    )
    assert Denial("edit_file").unasked_message() == (
        "Blocked: 'edit_file' needs the person's approval" + tail
    )
    assert Denial("edit_file", "ask rule").unasked_message() == (
        "Blocked: 'edit_file' needs the person's approval (ask rule)" + tail
    )


def test_the_denial_alternatives_cannot_be_edited() -> None:
    with pytest.raises(TypeError):
        Denial.ALTERNATIVES["edit_file"] = "x"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Denial("a").reason = "b"  # type: ignore[misc]


# ── blocked commands ────────────────────────────────────────────────


async def test_a_blocked_command_names_the_first_pattern_that_matches(caplog) -> None:
    stage = BlockedCommandStage(["sudo ", "rm -rf", "rm"])
    out = await stage.evaluate(_call("run_shell_command", command="sudo rm -rf /x"))
    assert out == Block("Blocked: command matches blocked pattern 'sudo '.")
    assert "Blocked command: sudo rm -rf /x" in caplog.text
    mid = await stage.evaluate(_call("run_shell_command", command="echo x; rm -rf y"))
    assert mid == Block("Blocked: command matches blocked pattern 'rm -rf'.")


async def test_a_blocked_command_is_case_sensitive_and_ignores_other_tools() -> None:
    stage = BlockedCommandStage(["rm -rf"])
    assert await stage.evaluate(_call("run_shell_command", command="RM -RF /")) == Continue()
    assert await stage.evaluate(_call("run_shell_command")) == Continue()
    assert await stage.evaluate(_call("edit_file", command="rm -rf /")) == Continue()
    assert (
        await BlockedCommandStage([]).evaluate(_call("run_shell_command", command="rm -rf /"))
        == Continue()
    )
    assert BlockedCommandStage.applies_to("run_shell_command")
    assert not BlockedCommandStage.applies_to("edit_file")


async def test_an_empty_blocked_pattern_blocks_every_shell_call() -> None:
    out = await BlockedCommandStage([""]).evaluate(_call("run_shell_command", command="ls"))
    assert out == Block("Blocked: command matches blocked pattern ''.")


# ── protected paths ─────────────────────────────────────────────────


def test_the_target_path_is_the_first_non_empty_string_among_the_keys() -> None:
    target = ProtectedPathStage.target_path
    assert target({"file_path": "a", "file_name": "b", "path": "c"}) == "a"
    assert target({"file_path": "", "file_name": "b", "path": "c"}) == "b"
    assert target({"file_path": 3, "file_name": None, "path": "c"}) == "c"
    assert target({"path": ""}) == ""
    assert target({}) == ""


async def test_a_protected_path_blocks_by_basename_and_glob_even_through_traversal(
    caplog,
) -> None:
    stage = ProtectedPathStage([".env", ".env.*", "**/credentials.json"])
    msg = "Blocked: '{}' is a protected path and cannot be written to."
    for path in ("../.env", "/a/b/.env.prod", "/x/y/credentials.json", ".env"):
        out = await stage.evaluate(_call("edit_file", file_path=path))
        assert out == Block(msg.format(path))
    assert "Protected path blocked: ../.env via edit_file" in caplog.text
    assert await stage.evaluate(_call("edit_file", file_path="notes.txt")) == Continue()
    assert await stage.evaluate(_call("edit_file")) == Continue()
    assert await stage.evaluate(_call("edit_file", file_path="")) == Continue()
    assert await stage.evaluate(_call("read_file", file_path=".env")) == Continue()
    assert await ProtectedPathStage([]).evaluate(_call("edit_file", file_path=".env")) == Continue()


@pytest.mark.parametrize(
    "name",
    [
        "save_file",
        "edit_file",
        "edit_file_replace_all",
        "create_file",
        "notebook_edit_cell",
        "notebook_add_cell",
        "notebook_remove_cell",
    ],
)
async def test_every_write_function_is_gated_on_a_protected_path(name: str) -> None:
    out = await ProtectedPathStage([".env"]).evaluate(_call(name, path=".env"))
    assert isinstance(out, Block)
    assert ProtectedPathStage.applies_to(name)


# ── PreToolUse hook ─────────────────────────────────────────────────


async def test_a_pre_tool_use_deny_blocks_with_its_message_or_the_default() -> None:
    firer, rec = _firer(PreToolUse=_pre(HookDecision.DENY, "no way"))
    out = await PreToolUseHookStage(firer).evaluate(_call("edit_file", file_path="a"))
    assert out == Block("no way")
    assert rec.events == ["PreToolUse", "PermissionDenied"]
    pre, denied = rec.fired[0][1], rec.fired[1][1]
    assert (pre.tool_name, pre.tool_args, pre.session_id) == (
        "edit_file",
        {"file_path": "a"},
        "sess-1",
    )
    assert (denied.reason, denied.tool_name) == ("pre_tool_use_hook", "edit_file")

    firer, rec = _firer(PreToolUse=_pre(HookDecision.DENY))
    out = await PreToolUseHookStage(firer).evaluate(_call("edit_file"))
    assert out == Block("Blocked: 'edit_file' denied by PreToolUse hook")


async def test_a_pre_tool_use_ask_is_refused_after_the_request_and_denial_fire() -> None:
    firer, rec = _firer(PreToolUse=_pre(HookDecision.ASK))
    out = await PreToolUseHookStage(firer).evaluate(_call("edit_file", file_path="a"))
    assert out == Block(
        "Blocked: a PreToolUse hook asked for confirmation of 'edit_file', "
        "and hooks have no way to prompt the user. Ask the user directly, or "
        "have the hook return allow/deny."
    )
    assert rec.events == ["PreToolUse", "PermissionRequest", "PermissionDenied"]
    assert rec.fired[1][1].tool_args == {"file_path": "a"}
    assert rec.fired[2][1].reason == "pre_tool_use_hook"

    firer, _ = _firer(PreToolUse=_pre(HookDecision.ASK, "ask me"))
    assert await PreToolUseHookStage(firer).evaluate(_call("x")) == Block("ask me")


async def test_a_pre_tool_use_allow_skips_and_other_answers_continue_or_stop() -> None:
    def stage(result: HookResult) -> PreToolUseHookStage:
        return PreToolUseHookStage(_firer(PreToolUse=result)[0])

    assert await stage(_pre(HookDecision.ALLOW)).evaluate(_call("x")) == AllowSkip()
    assert await stage(HookResult()).evaluate(_call("x")) == Continue()
    assert await stage(_pre(HookDecision.DEFER)).evaluate(_call("x")) == Continue()
    stop = HookResult(should_continue=False)
    assert await stage(stop).evaluate(_call("x")) == Block("Blocked by PreToolUse hook")
    stop = HookResult(should_continue=False, message="halt")
    assert await stage(stop).evaluate(_call("x")) == Block("halt")
    both = _pre(HookDecision.DENY, "denied", should_continue=False)
    assert await stage(both).evaluate(_call("x")) == Block("denied")


# ── evaluator ───────────────────────────────────────────────────────


def _evaluator_stage(
    evaluator: PermissionEvaluator | None, ask_person: Any = None
) -> tuple[PermissionEvaluatorStage, _Recorder]:
    firer, rec = _firer()
    return PermissionEvaluatorStage(evaluator, firer, ask_person), rec


async def test_without_an_evaluator_the_stage_passes_and_fires_nothing() -> None:
    stage, rec = _evaluator_stage(None)
    assert await stage.evaluate(_call("run_shell_command", command="rm x")) == Continue()
    assert rec.fired == []


async def test_a_deny_fires_the_denial_and_attaches_advice_only_when_off_entirely() -> None:
    stage, rec = _evaluator_stage(PermissionEvaluator.from_strings(deny=["Bash"]))
    out = await stage.evaluate(_call("run_shell_command", command="ls"))
    assert out == Block(
        Denial("run_shell_command", "deny rule 'Bash' matched").policy_message(
            tool_is_off_entirely=True
        )
    )
    assert rec.events == ["PermissionDenied"]
    assert rec.fired[0][1].reason == "permission_evaluator"

    scoped, rec = _evaluator_stage(PermissionEvaluator.from_strings(deny=["Bash(rm *)"]))
    out = await scoped.evaluate(_call("run_shell_command", command="rm x"))
    assert out == Block(
        "Blocked: permission policy denied 'run_shell_command'. deny rule 'Bash(rm *)' matched"
    )

    other, _ = _evaluator_stage(PermissionEvaluator.from_strings(deny=["edit_file"]))
    out = await other.evaluate(_call("edit_file", file_path="a"))
    assert out == Block(
        "Blocked: permission policy denied 'edit_file'. deny rule 'edit_file' matched"
    )


async def test_an_ask_for_a_paused_or_unknown_tool_continues_after_the_request_fires() -> None:
    asked: list[str] = []

    async def ask(*a: Any) -> bool | None:
        asked.append(a[0])
        return True

    stage, rec = _evaluator_stage(PermissionEvaluator.from_strings(ask=["Bash"]), ask)
    shell = Function(name="run_shell_command", entrypoint=_noop, requires_confirmation=True)
    paused = ToolCallContext(
        name="run_shell_command", func=None, args={"command": "ls"}, agent=_Owner("m", [shell])
    )
    assert await stage.evaluate(paused) == Continue()
    assert await stage.evaluate(_call("run_shell_command", command="ls")) == Continue()
    assert asked == []
    assert rec.events == ["PermissionRequest", "PermissionRequest"]
    assert rec.fired[0][1].tool_args == {"command": "ls"}


def _unpaused(**args: Any) -> ToolCallContext:
    shell = Function(name="run_shell_command", entrypoint=_noop)
    return ToolCallContext(
        name="run_shell_command", func=None, args=args, agent=_Owner("lead", [shell])
    )


async def test_an_unpaused_ask_is_put_to_the_person_with_the_reason_and_agent_path() -> None:
    seen: list[tuple] = []

    async def ask(name, args, reason, path):
        seen.append((name, args, reason, path))
        return True

    stage, rec = _evaluator_stage(PermissionEvaluator.from_strings(ask=["Bash"]), ask)
    assert await stage.evaluate(_unpaused(command="ls")) == Continue()
    assert seen == [("run_shell_command", {"command": "ls"}, "ask rule 'Bash' matched", ["lead"])]
    assert rec.events == ["PermissionRequest"]


async def test_an_unpaused_ask_the_person_declines_blocks_with_the_declined_note() -> None:
    async def ask(*a: Any) -> bool | None:
        return False

    stage, rec = _evaluator_stage(PermissionEvaluator.from_strings(ask=["Bash"]), ask)
    out = await stage.evaluate(_unpaused(command="ls"))
    assert out == Block(declined_note("run_shell_command"))
    assert rec.events == ["PermissionRequest", "PermissionDenied"]
    assert rec.fired[1][1].reason == "permission_evaluator"


@pytest.mark.parametrize("channel", ["none", "silent", "raises"])
async def test_an_unpaused_ask_nobody_can_answer_is_refused_with_guidance(channel: str) -> None:
    async def silent(*a: Any) -> bool | None:
        return None

    async def raises(*a: Any) -> bool | None:
        raise RuntimeError("socket closed")

    ask = {"none": None, "silent": silent, "raises": raises}[channel]
    stage, rec = _evaluator_stage(PermissionEvaluator.from_strings(ask=["Bash"]), ask)
    out = await stage.evaluate(_unpaused(command="ls"))
    assert out == Block(Denial("run_shell_command", "ask rule 'Bash' matched").unasked_message())
    assert rec.events == ["PermissionRequest", "PermissionDenied"]


async def test_allow_and_defer_pass_without_an_event() -> None:
    stage, rec = _evaluator_stage(PermissionEvaluator.from_strings(allow=["Bash"]))
    assert await stage.evaluate(_call("run_shell_command", command="ls")) == Continue()
    stage, rec2 = _evaluator_stage(PermissionEvaluator.from_strings())
    assert await stage.evaluate(_call("run_shell_command", command="ls")) == Continue()
    assert rec.fired == [] and rec2.fired == []


# ── bypass guard ────────────────────────────────────────────────────


def _guard(in_bypass: bool = True, ask: Any = None) -> BypassGuard:
    return BypassGuard(
        config=BypassGuardConfig(use_model=False),
        project_dir=Path("/proj"),
        in_bypass=lambda: in_bypass,
        ask=ask,
        tool_hints=ToolHintsRegistry(),
    )


async def test_the_bypass_guard_stage_blocks_with_the_guards_message_or_continues() -> None:
    stage = BypassGuardStage(_guard())
    out = await stage.evaluate(_call("run_shell_command", command="rm -rf ~/"))
    assert isinstance(out, Block) and "nobody was available" in out.message
    paths: list[list[str]] = []

    async def ask(name, args, prompt, path, *, follow_up=None):
        paths.append(path)
        return True

    agent = _Owner("builder", [])
    ctx = ToolCallContext(
        name="run_shell_command", func=None, args={"command": "rm -rf ~/"}, agent=agent
    )
    assert await BypassGuardStage(_guard(ask=ask)).evaluate(ctx) == Continue()
    assert paths == [["builder"]]
    assert await stage.evaluate(_call("run_shell_command", command="ls")) == Continue()
    assert (
        await BypassGuardStage(_guard(in_bypass=False)).evaluate(
            _call("run_shell_command", command="rm -rf ~/")
        )
        == Continue()
    )


# ── the pipeline ────────────────────────────────────────────────────


class _Fixed(PermissionStage):
    def __init__(self, outcome: Any, log: list[str], label: str, skippable: bool = False) -> None:
        self.outcome, self.log, self.label = outcome, log, label
        self.skipped_by_hook_allow = skippable  # type: ignore[misc]

    async def evaluate(self, ctx: ToolCallContext) -> Any:
        self.log.append(self.label)
        return self.outcome


async def test_an_empty_pipeline_lets_the_call_through() -> None:
    assert await PermissionPipeline([]).evaluate(_call("x")) is None


async def test_the_first_block_decides_and_later_stages_never_run() -> None:
    log: list[str] = []
    pipeline = PermissionPipeline(
        [
            _Fixed(Continue(), log, "a"),
            _Fixed(Block("first"), log, "b"),
            _Fixed(Block("second"), log, "c"),
        ]
    )
    assert await pipeline.evaluate(_call("x")) == "first"
    assert log == ["a", "b"]


async def test_a_hook_allow_skips_only_the_skippable_stages_that_follow_it() -> None:
    log: list[str] = []
    pipeline = PermissionPipeline(
        [
            _Fixed(Continue(), log, "before", skippable=True),
            _Fixed(AllowSkip(), log, "hook"),
            _Fixed(Block("skipped"), log, "skippable", skippable=True),
            _Fixed(Continue(), log, "safety"),
            _Fixed(Block("safety says no"), log, "safety2"),
        ]
    )
    assert await pipeline.evaluate(_call("x")) == "safety says no"
    assert log == ["before", "hook", "safety", "safety2"]


def _stages(pipeline: PermissionPipeline) -> list[type]:
    return [type(s) for s in pipeline._stages]


def test_build_orders_hook_safety_lists_evaluator_and_adds_the_guard_last() -> None:
    firer, _ = _firer()
    base = [
        PreToolUseHookStage,
        ProtectedPathStage,
        BlockedCommandStage,
        PermissionEvaluatorStage,
    ]
    assert _stages(PermissionPipeline.build(firer, [], [], None)) == base
    with_guard = PermissionPipeline.build(firer, [], [], None, bypass_guard=_guard())
    assert _stages(with_guard) == [*base, BypassGuardStage]


async def test_a_built_pipeline_threads_ask_person_into_the_evaluator() -> None:
    firer, _ = _firer()
    seen: list[str] = []

    async def ask(name, args, reason, path):
        seen.append(name)
        return False

    pipeline = PermissionPipeline.build(
        firer, [], [], PermissionEvaluator.from_strings(ask=["Bash"]), ask_person=ask
    )
    out = await pipeline.evaluate(_unpaused(command="ls"))
    assert out == declined_note("run_shell_command") and seen == ["run_shell_command"]


async def test_a_built_pipeline_runs_the_hook_first_and_the_safety_lists_after_an_allow() -> None:
    firer, rec = _firer(PreToolUse=_pre(HookDecision.ALLOW))
    pipeline = PermissionPipeline.build(
        firer,
        [".env"],
        ["rm -rf"],
        PermissionEvaluator.from_strings(deny=["Bash", "edit_file"]),
        bypass_guard=_guard(),
    )
    # allowed: the evaluator's deny and the guard are skipped
    assert await pipeline.evaluate(_call("run_shell_command", command="rm x")) is None
    # but the safety lists still hold
    assert await pipeline.evaluate(_call("edit_file", file_path=".env")) == (
        "Blocked: '.env' is a protected path and cannot be written to."
    )
    assert await pipeline.evaluate(_call("run_shell_command", command="rm -rf /")) == (
        "Blocked: command matches blocked pattern 'rm -rf'."
    )
    assert rec.events.count("PermissionDenied") == 0
    # the guard would have refused this one, but a hook's allow skips it
    push = _call("run_shell_command", command="git push --force origin main")
    assert await pipeline.evaluate(push) is None
    bare = PermissionPipeline.build(_firer()[0], [], [], None, bypass_guard=_guard())
    assert "nobody was available" in (await bare.evaluate(push) or "")

    firer, _ = _firer(PreToolUse=_pre(HookDecision.DENY, "hook said no"))
    pipeline = PermissionPipeline.build(firer, [".env"], [], None)
    assert await pipeline.evaluate(_call("edit_file", file_path=".env")) == "hook said no"


async def test_a_built_pipeline_with_a_guard_refuses_a_harmful_call_nobody_can_approve() -> None:
    firer, _ = _firer()
    pipeline = PermissionPipeline.build(firer, [], [], None, bypass_guard=_guard())
    out = await pipeline.evaluate(_call("run_shell_command", command="rm -rf ~/"))
    assert out is not None and "nobody was available" in out
