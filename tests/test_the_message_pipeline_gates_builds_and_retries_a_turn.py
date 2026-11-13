"""What the headless message pipeline does around a turn.

The UserPromptSubmit gates (the extensions', then the configured hook), the
message the model is sent (reminders, datetime, guardrail prefix), the Stop
hook's bounded retry, the StopFailure notice and the media handed to the
team. The real ``SessionMessageHandler`` runs; only its collaborators are
faked.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from agno.metrics import RunMetrics
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from pydantic import BaseModel

from igni_core.core.hooks.extension_gate import ExtensionHookVerdict, extension_hook_gate
from igni_core.core.session.compaction.context_budget import ContextBudget
from igni_core.core.session.message_handler import MessageHandlerDeps, SessionMessageHandler
from igni_core.core.session.schemas import MessageMedia


class _Hooks:
    """Hook executor: answers per event from a script, records every call."""

    def __init__(self, **answers: Any) -> None:
        self.calls: list[tuple[str, BaseModel]] = []
        self._answers = {k: list(v) if isinstance(v, list) else [v] for k, v in answers.items()}

    async def execute(self, *, event: str, payload: BaseModel):
        self.calls.append((event, payload))
        queue = self._answers.get(event)
        if not queue:
            return SimpleNamespace(should_continue=True, message="")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def payloads(self, event: str) -> list[BaseModel]:
        return [p for name, p in self.calls if name == event]


def _block(message: str = "") -> SimpleNamespace:
    return SimpleNamespace(should_continue=False, message=message)


class _Audit:
    def __init__(self) -> None:
        self.entries: list = []

    def log(self, entry) -> None:
        self.entries.append(entry)


class _Team:
    """Answers each ``arun`` from a script of run outputs, recording the calls."""

    def __init__(self, *runs: RunOutput | Exception) -> None:
        self._runs = list(runs) or [_done("answer")]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def arun(self, message: str, **kwargs: Any):
        self.calls.append((message, kwargs))
        run = self._runs.pop(0) if len(self._runs) > 1 else self._runs[0]
        if isinstance(run, Exception):
            raise run
        return run


def _done(content: str = "answer", tokens: int = 1) -> RunOutput:
    return RunOutput(
        status=RunStatus.completed, content=content, metrics=RunMetrics(input_tokens=tokens)
    )


class _Guardrails:
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.seen: list[str] = []

    async def warning_prefix(self, text: str) -> str:
        self.seen.append(text)
        return self.prefix


class _Rig:
    def __init__(
        self,
        team: _Team | None = None,
        hooks: _Hooks | None = None,
        *,
        reminders: list[str] | None = None,
        prefix: str = "",
    ) -> None:
        self.team = team or _Team()
        self.hooks = hooks or _Hooks()
        self.audit = _Audit()
        self.errors: list[str] = []
        self.order: list[str] = []
        self.guardrails = _Guardrails(prefix)
        self._reminders = reminders or []
        self.compacted: list[tuple[int, int]] = []
        self.handler = SessionMessageHandler(
            MessageHandlerDeps(
                hooks=SimpleNamespace(executor=self.hooks),
                audit=self.audit,
                display=SimpleNamespace(print_error=self.errors.append),
                guardrail_runner=self.guardrails,
                team_ref=lambda: self.team,
                pending_reminders_drain=self._drain,
                compact_hook=self._compact,
                ensure_mcp=self._mcp,
                session_id_ref=lambda: "s-1",
                budget=ContextBudget(window=100_000),
            )
        )

    def _drain(self) -> list[str]:
        out, self._reminders = self._reminders, []
        return out

    async def _compact(self, tokens: int, window: int) -> bool:
        self.compacted.append((tokens, window))
        return False

    async def _mcp(self) -> None:
        self.order.append("mcp")


class _Extensions:
    def __init__(
        self, *, rewrite: str | None = None, reject: str | None = None, only: str | None = None
    ) -> None:
        self.rewrite = rewrite
        self.reject = reject
        self.only = only
        self.asked: list[Any] = []
        extension_hook_gate.install(self)

    def hooks(self, event) -> bool:
        return True

    async def ask(self, value: Any) -> ExtensionHookVerdict[Any]:
        self.asked.append(value)
        if self.only is not None and value.hook_event.value != self.only:
            return ExtensionHookVerdict(allowed=True, value=value)
        if self.reject is not None:
            return ExtensionHookVerdict(allowed=False, value=value, reason=self.reject)
        if self.rewrite is not None:
            value = value.model_copy(update={"message": self.rewrite})
        return ExtensionHookVerdict(allowed=True, value=value)

    def tell(self, event, payload) -> None:
        return None

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_extensions():
    extension_hook_gate.install(None)
    yield
    extension_hook_gate.install(None)


class TestAPromptTheHooksBlock:
    async def test_the_configured_hook_blocks_with_its_message_and_the_team_never_runs(self):
        rig = _Rig(hooks=_Hooks(UserPromptSubmit=_block("no secrets please")))

        assert await rig.handler.handle("my password is hunter2") == "no secrets please"

        assert rig.team.calls == []
        assert rig.guardrails.seen == []
        [entry] = rig.audit.entries
        assert entry.status.value == "blocked"
        assert entry.tool == "user_prompt"
        assert entry.details.reason == "no secrets please"
        assert rig.handler.last_failure == ""

    async def test_a_block_without_a_message_says_which_hook_blocked(self):
        rig = _Rig(hooks=_Hooks(UserPromptSubmit=_block("")))

        assert await rig.handler.handle("hi") == "Blocked by UserPromptSubmit hook."
        assert rig.audit.entries[0].details.reason == "Blocked by UserPromptSubmit hook."

    async def test_an_extension_that_rejects_the_prompt_stops_it_before_the_configured_hook(self):
        _Extensions(reject="policy says no")
        rig = _Rig()

        assert await rig.handler.handle("hi") == "policy says no"

        assert rig.hooks.calls == []
        assert rig.team.calls == []
        assert rig.audit.entries[0].details.reason == "policy says no"

    async def test_an_extension_rewrite_is_what_the_hook_and_the_team_see(self):
        _Extensions(rewrite="rewritten")
        rig = _Rig()

        await rig.handler.handle("original")

        assert [p.message for p in rig.hooks.payloads("UserPromptSubmit")] == ["rewritten"]
        assert rig.guardrails.seen == ["rewritten"]
        assert rig.team.calls[0][0].endswith("\nrewritten")

    async def test_mcp_is_connected_before_any_hook_runs(self):
        rig = _Rig(hooks=_Hooks(UserPromptSubmit=_block("stop")))
        original = rig.hooks.execute

        async def _recording(*, event, payload):
            rig.order.append(event)
            return await original(event=event, payload=payload)

        rig.hooks.execute = _recording  # type: ignore[method-assign]

        await rig.handler.handle("hi")

        assert rig.order == ["mcp", "UserPromptSubmit"]


class TestTheMessageTheModelIsSent:
    async def test_it_is_the_datetime_line_then_the_message(self):
        rig = _Rig()

        await rig.handler.handle("hello there")

        sent = rig.team.calls[0][0]
        assert re.fullmatch(
            r"<system-context>Current datetime: \d{4}-\d\d-\d\d \d\d:\d\d \S+</system-context>"
            r"\nhello there",
            sent,
        )

    async def test_queued_reminders_come_first_and_are_drained_once(self):
        rig = _Rig(reminders=["build finished", "tests failed"])

        await rig.handler.handle("one")
        await rig.handler.handle("two")

        first, second = (call[0] for call in rig.team.calls)
        assert first.startswith(
            "<system-reminder>build finished\ntests failed</system-reminder>\n<system-context>"
        )
        assert "<system-reminder>" not in second

    async def test_the_guardrail_warning_precedes_everything(self):
        rig = _Rig(reminders=["r"], prefix="[warning] ")

        await rig.handler.handle("hi")

        assert rig.team.calls[0][0].startswith("[warning] <system-reminder>r</system-reminder>\n")

    async def test_unicode_and_an_empty_message_reach_the_team_untouched(self):
        rig = _Rig()

        await rig.handler.handle("héllo 世界 🎉")
        await rig.handler.handle("")

        assert rig.team.calls[0][0].endswith("</system-context>\nhéllo 世界 🎉")
        assert rig.team.calls[1][0].endswith("</system-context>\n")

    async def test_media_is_handed_to_the_team_and_absent_media_adds_nothing(self):
        rig = _Rig()

        await rig.handler.handle("look", MessageMedia(images=["img"], files=["f"]))
        await rig.handler.handle("plain")

        assert rig.team.calls[0][1] == {"stream": False, "images": ["img"], "files": ["f"]}
        assert rig.team.calls[1][1] == {"stream": False}


class TestTheStopHookSendsTheAgentBack:
    async def test_a_block_reruns_the_team_with_the_critique_and_uses_the_new_reply(self):
        team = _Team(_done("first draft"), _done("better draft"))
        rig = _Rig(
            team,
            _Hooks(Stop=[_block("too vague"), SimpleNamespace(should_continue=True, message="")]),
        )

        assert await rig.handler.handle("hi") == "better draft"

        assert team.calls[1][0] == (
            "[SYSTEM] Your previous response was rejected by a Stop hook: too vague\n"
            "Please revise your response to address this issue."
        )
        assert team.calls[1][1] == {"stream": False}
        assert [p.response for p in rig.hooks.payloads("Stop")] == ["first draft", "better draft"]

    async def test_a_hook_that_always_blocks_is_asked_three_times_and_the_last_reply_stands(self):
        team = _Team(_done("r0"), _done("r1"), _done("r2"), _done("r3"))
        rig = _Rig(team, _Hooks(Stop=_block("again")))

        assert await rig.handler.handle("hi") == "r3"

        assert len(team.calls) == 4  # the turn and three retries
        assert len(rig.hooks.payloads("Stop")) == 3
        assert rig.handler.last_failure == ""

    async def test_a_block_without_a_message_has_a_default_critique(self):
        team = _Team(_done("a"), _done("b"))
        rig = _Rig(
            team, _Hooks(Stop=[_block(""), SimpleNamespace(should_continue=True, message="")])
        )

        await rig.handler.handle("hi")

        assert "rejected by a Stop hook: Response blocked by Stop hook.\n" in team.calls[1][0]

    async def test_an_extension_can_block_the_stop_too(self):
        _Extensions(reject="extension says revise", only="Stop")
        team = _Team(_done("a"), _done("b"), _done("c"), _done("d"))
        rig = _Rig(team)

        await rig.handler.handle("hi")

        assert "rejected by a Stop hook: extension says revise\n" in team.calls[1][0]
        # The extension answered first and blocked, so the configured hook never saw Stop.
        assert rig.hooks.payloads("Stop") == []

    async def test_the_payload_carries_at_most_500_characters_of_the_reply(self):
        rig = _Rig(_Team(_done("x" * 700)))

        assert await rig.handler.handle("hi") == "x" * 700

        assert [len(p.response) for p in rig.hooks.payloads("Stop")] == [500]


class TestAFailedTurnIsReportedAndTold:
    async def test_a_raising_stop_failure_hook_does_not_mask_the_failure(self, caplog):
        rig = _Rig(
            _Team(RuntimeError("provider down")), _Hooks(StopFailure=RuntimeError("hook broke"))
        )

        with caplog.at_level("WARNING"):
            reply = await rig.handler.handle("hi")

        assert reply == "Error handling message: provider down"
        assert rig.handler.last_failure == reply
        assert rig.errors == [reply]
        assert "the StopFailure hook failed" in caplog.text

    async def test_the_failure_row_and_hook_payload_name_the_error_type(self):
        rig = _Rig(_Team(ValueError("bad")))

        await rig.handler.handle("hi")

        [entry] = rig.audit.entries
        assert entry.status.value == "error"
        assert entry.details.error == "bad"
        assert entry.details.error_type == "ValueError"
        [payload] = rig.hooks.payloads("StopFailure")
        assert (payload.error, payload.error_type) == ("bad", "ValueError")

    async def test_a_failed_run_is_a_failure_not_a_reply(self):
        failed = RunOutput(status=RunStatus.error, content="model not configured")
        rig = _Rig(_Team(failed))

        reply = await rig.handler.handle("hi")

        assert reply == "Error handling message: model not configured"
        assert rig.hooks.payloads("Stop") == []
        assert rig.hooks.payloads("StopFailure")[0].error_type == "failed"
        assert rig.audit.entries[0].details.error_type == "failed"

    async def test_a_failed_run_with_no_content_says_the_run_failed(self):
        rig = _Rig(_Team(RunOutput(status=RunStatus.error, content=None)))

        assert await rig.handler.handle("hi") == "Error handling message: The run failed."

    async def test_a_pause_with_no_evaluator_names_the_tool_it_stopped_at(self):
        paused = RunOutput(
            status=RunStatus.paused,
            content="I need confirmation.",
            tools=[ToolExecution(tool_name="run_shell_command", requires_confirmation=True)],
        )
        rig = _Rig(_Team(paused))

        reply = await rig.handler.handle("hi")

        assert "['run_shell_command']" in reply
        assert "I need confirmation." not in reply
        assert rig.hooks.payloads("StopFailure")[0].error_type == "paused"

    async def test_a_failure_does_not_stick_to_the_next_turn(self):
        rig = _Rig(_Team(RuntimeError("once"), _done("fine")))

        await rig.handler.handle("one")
        assert rig.handler.last_failure
        assert await rig.handler.handle("two") == "fine"

        assert rig.handler.last_failure == ""

    async def test_a_run_without_metrics_latches_and_compacts_nothing(self):
        rig = _Rig(_Team(RunOutput(status=RunStatus.completed, content="ok")))

        assert await rig.handler.handle("hi") == "ok"

        assert rig.compacted == []
