"""The message pipeline's typed results.

What a run produced (:class:`TurnOutcome`), what a turn returned
(:class:`TurnResult`), and why a turn produced no reply
(:class:`TurnFailure`) — all as models.

The names of the tools a pause is told about are said in prose
(:func:`tools_as_prose`): the person reads the same name the tool
call carried.

The run outputs Agno hands back are read in exactly one place,
:meth:`TurnOutcome.from_run`. The pipeline used to probe every run
object with ``getattr`` and compare statuses as lower-cased strings,
which is how the same "is this run paused?" question ended up written
twice and the two copies drifted.
"""

from __future__ import annotations

from enum import Enum

from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.session.message_team import AnyRunOutput


def tools_as_prose(tools: list[str]) -> str:
    """The tool names as a person reads them, not as a Python list.

    ``repr`` gives a list its brackets and quotes; the person at the
    terminal is told which tool stopped the run, and the tool's own
    name — the one the tool call on screen and the audit row carry —
    is what they read. So the name is kept verbatim and only the list
    around it is dropped.
    """
    names = [tool for tool in tools if tool]
    if not names:
        return "unknown tool"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f" and {names[-1]}"


class OutcomeKind(str, Enum):
    """How a run ended."""

    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"


class TurnOutcome(BaseModel):
    """One run of the team, read once and typed.

    ``content`` is the run's own text — the error of a failed run, the
    placeholder of a paused one — and empty when the run said nothing
    textual. ``input_tokens`` is the count the context budget latches.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: OutcomeKind
    content: str = ""
    input_tokens: int = 0
    #: The requirements nobody has answered yet.
    requirements: list[RunRequirement] = Field(default_factory=list)
    #: The tools the pause is waiting on.
    paused_tools: list[str] = Field(default_factory=list)

    @classmethod
    def from_run(cls, run: AnyRunOutput) -> TurnOutcome:
        """Read the outcome off an Agno run output."""
        status = cls._status_name(run.status)
        if status == RunStatus.paused.value.lower():
            kind = OutcomeKind.PAUSED
        elif status == RunStatus.error.value.lower():
            kind = OutcomeKind.FAILED
        else:
            kind = OutcomeKind.COMPLETED
        metrics = run.metrics
        content = run.content
        return cls(
            kind=kind,
            content=content if isinstance(content, str) else "",
            input_tokens=metrics.input_tokens if metrics is not None else 0,
            requirements=list(run.active_requirements),
            paused_tools=cls._paused_tool_names(run),
        )

    @staticmethod
    def _status_name(status: RunStatus | str | None) -> str:
        """The run's status as a lower-case name.

        Agno's own status is an enum; a status handed over as a plain
        string is accepted too, so the reading does not depend on
        which of the two a caller produced.
        """
        if isinstance(status, RunStatus):
            return status.value.lower()
        if isinstance(status, str):
            return status.lower()
        return ""

    @staticmethod
    def _paused_tool_names(run: AnyRunOutput) -> list[str]:
        """The tools the run is waiting on."""
        return [tool.tool_name or "?" for tool in (run.tools or []) if tool.is_paused]


class FailureKind(str, Enum):
    """Why a turn produced no reply."""

    PAUSED = "paused"
    FAILED = "failed"
    ERROR = "error"


class TurnFailure(BaseModel):
    """A turn that produced no reply, and why.

    The failures a turn can end in — a pause nobody can answer, a run
    the provider failed, an error raised mid-turn — are values here,
    not exceptions raised and caught again on the way out.
    """

    kind: FailureKind
    #: What the person is told.
    message: str
    #: What kind of failure it was, for the audit row and the
    #: ``StopFailure`` hook payload.
    error_type: str = ""

    @classmethod
    def paused(cls, outcome: TurnOutcome) -> TurnFailure:
        """The run stopped at a confirmation nobody can answer.

        The tool never ran: Agno returns early with
        ``status=paused`` and the placeholder content. Only the
        backend knows how to resolve a pause (over a client RPC), so
        on this path the placeholder must not be printed as the
        assistant's answer — a turn that silently dropped the work it
        asked for is worse than a reported failure.

        The tools are named in prose (:func:`tools_as_prose`): the
        person reads the same name the tool call carried.
        """
        tools = tools_as_prose(outcome.paused_tools)
        # The remedies are QUICKSTART's, in its order. This offered only
        # ``--auto-approve`` (which the docs warn against) and "a settings
        # rule", and not ``--read-only``, which is what a question needs.
        return cls(
            kind=FailureKind.PAUSED,
            message=(
                f"Run paused for confirmation on {tools} but this session cannot resolve a "
                "pause; the tool did not run. Decide up front what it may do: --read-only "
                "to only read, or a permissions.allow rule in ~/.igni/config.yaml (with "
                "--accept-edits for edits); --auto-approve allows everything. Or use a "
                "client that supports confirmations."
            ),
            error_type=FailureKind.PAUSED.value,
        )

    @classmethod
    def failed(cls, outcome: TurnOutcome) -> TurnFailure:
        """The run came back as an error.

        A non-streamed run does not raise when it fails — no model
        configured, the model's reply cut off, a provider error — it
        returns ``status=error`` with the error as its content.
        """
        return cls(
            kind=FailureKind.FAILED,
            message=outcome.content or "The run failed.",
            error_type=FailureKind.FAILED.value,
        )

    @classmethod
    def from_exception(cls, exc: Exception) -> TurnFailure:
        """Something raised mid-turn."""
        return cls(
            kind=FailureKind.ERROR,
            message=str(exc),
            error_type=type(exc).__name__,
        )


class TurnResult(BaseModel):
    """What one turn produced: the reply, or the failure that replaced it."""

    reply: str = ""
    failure: TurnFailure | None = None

    @classmethod
    def completed(cls, reply: str) -> TurnResult:
        return cls(reply=reply)

    @classmethod
    def failed(cls, failure: TurnFailure) -> TurnResult:
        return cls(failure=failure)

    @property
    def ok(self) -> bool:
        """Whether the turn produced a reply."""
        return self.failure is None


__all__ = [
    "FailureKind",
    "OutcomeKind",
    "TurnFailure",
    "TurnOutcome",
    "TurnResult",
    "tools_as_prose",
]
