"""The findings the matrix exposes today, and which cells each one turns red."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tests.session_lifecycle.schemas.finding import Finding


class KnownFindings(BaseModel):
    """The cells a set of findings explains, and the invariants they may break there."""

    finding_ids: list[str] = Field(default_factory=list)
    invariants: frozenset[str] = Field(default_factory=frozenset)

    def describe(self) -> str:
        names = ", ".join(f"{f} ({FINDINGS[f].title})" for f in self.finding_ids)
        return f"known finding(s) {names}"

    @classmethod
    def for_cell(cls, cell: str) -> KnownFindings | None:
        ids = CELLS.get(cell)
        if not ids:
            return None
        invariants: frozenset[str] = frozenset()
        for finding_id in ids:
            invariants |= FINDINGS[finding_id].invariants
        return cls(finding_ids=list(ids), invariants=invariants)


def _finding(finding_id: str, title: str, *invariants: str) -> tuple[str, Finding]:
    return finding_id, Finding(finding_id=finding_id, title=title, invariants=frozenset(invariants))


FINDINGS: dict[str, Finding] = dict(
    [
        _finding(
            "F01", "a message sent while a turn is paused cannot see the paused turn", "history"
        ),
        _finding(
            "F02",
            "Stop on a paused turn leaves the session running for ever",
            "cancel-clears",
            "goes-idle",
            "persisted",
            "stop-affordance",
        ),
        _finding(
            "F03",
            "Stop while a reply streams can wedge the session: nothing is answered again",
            "answers",
            "cancel-clears",
            "goes-idle",
            "stop-affordance",
        ),
        _finding("F04", "a cancelled turn disappears from what the model is shown", "history"),
        _finding(
            "F05",
            "a turn whose model call failed disappears from what the model is shown",
            "history",
        ),
        _finding(
            "F06",
            "approvals a new message drops are never withdrawn from the windows",
            "stop-affordance",
        ),
        _finding(
            "F07", "a run that paused is counted again in the session's tokens", "tokens-once"
        ),
        _finding(
            "F08",
            "the session's token total loses a run that ran beside another save",
            "tokens-lost",
        ),
        _finding(
            "F09",
            "an approval answer that resumes nothing starts the queued message beside the resumed run",
            "single-run",
            "history",
        ),
        _finding("F10", "two quick follow-ups reach the model in the wrong order", "history"),
        _finding("F11", "/fork during a turn leaves that turn out of the fork", "history"),
        _finding(
            "F12",
            "after a restart the interrupted-run note repeats a turn the model already sees",
            "history",
        ),
        _finding(
            "F13",
            "/clear carries a paused approval into the new chat, where answering it wedges the session",
            "goes-idle",
            "persisted",
            "stop-affordance",
        ),
        _finding("F14", "Bypass shows the model .env and igni's own credentials file", "no-secret"),
        _finding("F15", "nothing ends a turn whose model keeps calling tools", "bounded-run"),
    ]
)

# cell id (``state.action``, or a scenario's id) -> the findings that turn it red.
CELLS: dict[str, tuple[str, ...]] = {
    "background.approve": ("F07",),
    "background.cancel": ("F07",),
    "background.cancel_twice": ("F07",),
    "background.deny": ("F07",),
    "background.deny_feedback": ("F07",),
    "background.follow_up": ("F07",),
    "background.reconnect": ("F07",),
    "background.reload": ("F07",),
    "background.restart": ("F07",),
    "background.second_window": ("F07",),
    "background.switch_model": ("F07",),
    "background.two_follow_ups": ("F07",),
    "cancelled.approve": ("F03",),
    "cancelled.cancel": ("F03",),
    "cancelled.cancel_twice": ("F03",),
    "cancelled.clear": ("F03",),
    "cancelled.deny": ("F03",),
    "cancelled.deny_feedback": ("F03",),
    "cancelled.follow_up": ("F03",),
    "cancelled.fork": ("F03",),
    "cancelled.reconnect": ("F03",),
    "cancelled.reload": ("F03",),
    "cancelled.restart": ("F12",),
    "cancelled.second_window": ("F03",),
    "cancelled.switch_model": ("F03",),
    "cancelled.two_follow_ups": ("F03",),
    "continuing.approve": ("F07",),
    "continuing.cancel": ("F04", "F07"),
    "continuing.cancel_twice": ("F02", "F07"),
    "continuing.deny": ("F07",),
    "continuing.deny_feedback": ("F09",),
    "continuing.follow_up": ("F07",),
    "continuing.fork": ("F11",),
    "continuing.reconnect": ("F07",),
    "continuing.reload": ("F07",),
    "continuing.second_window": ("F07",),
    "continuing.switch_model": ("F07",),
    "continuing.two_follow_ups": ("F07",),
    "errored.approve": ("F05",),
    "errored.cancel": ("F05",),
    "errored.cancel_twice": ("F05",),
    "errored.deny": ("F05",),
    "errored.deny_feedback": ("F05",),
    "errored.follow_up": ("F05",),
    "errored.fork": ("F05",),
    "errored.reconnect": ("F05",),
    "errored.reload": ("F05",),
    "errored.second_window": ("F05",),
    "errored.switch_model": ("F05",),
    "errored.two_follow_ups": ("F05",),
    "paused.approve": ("F07",),
    "paused.cancel": ("F02", "F04"),
    "paused.cancel_twice": ("F02", "F04"),
    "paused.clear": ("F13",),
    "paused.deny": ("F07",),
    "paused.deny_feedback": ("F07",),
    "paused.follow_up": ("F01", "F06"),
    "paused.fork": ("F11",),
    "paused.reconnect": ("F07",),
    "paused.reload": ("F07",),
    "paused.restart": ("F07", "F12"),
    "paused.second_window": ("F07",),
    "paused.switch_model": ("F07",),
    "paused.two_follow_ups": ("F01", "F06"),
    "streaming.cancel": ("F04",),
    "streaming.cancel_twice": ("F04",),
    "streaming.deny_feedback": ("F08",),
    "streaming.follow_up": ("F08",),
    "streaming.two_follow_ups": ("F08",),
    "subagent.approve": ("F07",),
    "subagent.cancel": ("F04", "F08"),
    "subagent.cancel_twice": ("F04", "F08"),
    "subagent.deny": ("F07",),
    "subagent.follow_up": ("F07",),
    "subagent.reconnect": ("F07",),
    "subagent.reload": ("F07",),
    "subagent.restart": ("F08",),
    "subagent.second_window": ("F07",),
    "subagent.switch_model": ("F07",),
    "subagent.two_follow_ups": ("F07",),
    "tool.approve": ("F07",),
    "tool.cancel": ("F04", "F07"),
    "tool.cancel_twice": ("F04", "F07"),
    "tool.deny": ("F07",),
    "tool.fork": ("F11",),
    "tool.reconnect": ("F07",),
    "tool.reload": ("F07",),
    "tool.restart": ("F07",),
    "tool.second_window": ("F07",),
    "tool.switch_model": ("F07",),
    "tool.two_follow_ups": ("F10",),
    "incident.6fa3e09e": ("F01",),
    "live.paused_follow_up": ("F01",),
    "runaway.stop": ("F03",),
    "runaway.unattended": ("F15",),
    "secret.bypass.dotenv": ("F14",),
    "secret.bypass.igni-credentials": ("F14",),
}
