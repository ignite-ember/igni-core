"""The findings the matrix exposes today, and which cells each one turns red."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tests.session_lifecycle.schemas.finding import Finding


class KnownFindings(BaseModel):
    """The cells a set of findings explains, and the invariants they may break there."""

    finding_ids: list[str] = Field(default_factory=list)
    invariants: frozenset[str] = Field(default_factory=frozenset)
    strict: bool = Field(default=True, description="Whether the cell fails on every run.")

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
        found = [FINDINGS[f] for f in ids]
        masked = {m for f in found if f.racy for m in f.masks}
        # Strict unless everything here is a race, or masked by one: then a run can pass.
        strict = any(not f.racy and f.finding_id not in masked for f in found)
        return cls(finding_ids=list(ids), invariants=invariants, strict=strict)


FINDINGS: dict[str, Finding] = {
    f.finding_id: f
    for f in [
        Finding.of("F04", "a cancelled turn disappears from what the model is shown", "history"),
        Finding.of(
            "F05",
            "a turn whose model call failed disappears from what the model is shown",
            "history",
        ),
        Finding.of(
            "F06",
            "approvals a new message drops are never withdrawn from the windows",
            "stop-affordance",
        ),
        Finding.of(
            "F07",
            "a sub-agent run that paused is counted again in the session's tokens",
            "tokens-once",
        ),
        Finding.of(
            "F08",
            "the session's token total loses a run that ran beside another save",
            "tokens-lost",
            racy=True,
            masks=("F07",),
        ),
        Finding.of("F11", "/fork during a turn leaves that turn out of the fork", "history"),
        Finding.of(
            "F12",
            "after a restart the interrupted-run note repeats a turn the model already sees",
            "history",
            racy=True,
        ),
        Finding.of(
            "F13",
            "/clear carries a paused approval into the new chat, where answering it wedges the session",
            "goes-idle",
            "persisted",
            "stop-affordance",
        ),
        Finding.of("F14", "Bypass shows the model the project's .env", "no-secret"),
        Finding.of(
            "F18",
            "a message sent while a turn works is taken into it, but its own stream never ends",
            "stream-ends",
            racy=True,
        ),
        Finding.of("F15", "nothing ends a turn whose model keeps calling tools", "bounded-run"),
        Finding.of(
            "F16",
            "a cancelled run's model call is counted in the session's tokens, or not, by timing",
            "tokens-lost",
            "tokens-once",
            racy=True,
        ),
        Finding.of(
            "F17",
            "a resumed run that ends after /fork or /clear is announced but not saved in that chat",
            "persisted",
            racy=True,
        ),
    ]
}

# cell id (``state.action``, or a scenario's id) -> the findings that turn it red, on main
# once #76 has landed (F01, F02, F03, F09 and F10 are fixed by #74 and #76; the main agent's
# part of F07 by #63, igni's credentials in F14 by #65).
# A cell whose findings are all races (or masked by one) is a non-strict xfail: Linux CI
# and macOS hit them differently (see the PR). Every other cell is strict.
CELLS: dict[str, tuple[str, ...]] = {
    "background.deny_feedback": ("F08",),
    "background.follow_up": ("F08",),
    "background.two_follow_ups": ("F08",),
    "cancelled.approve": ("F04", "F16"),
    "cancelled.cancel": ("F04", "F16"),
    "cancelled.cancel_twice": ("F04", "F16"),
    "cancelled.deny": ("F04", "F16"),
    "cancelled.deny_feedback": ("F04", "F16"),
    "cancelled.follow_up": ("F04", "F16"),
    "cancelled.fork": ("F04", "F16"),
    "cancelled.reconnect": ("F04", "F16"),
    "cancelled.reload": ("F04", "F16"),
    "cancelled.restart": ("F12", "F16"),
    "cancelled.second_window": ("F04", "F16"),
    "cancelled.switch_model": ("F04", "F16"),
    "cancelled.two_follow_ups": ("F04", "F16"),
    "continuing.cancel": ("F04", "F16"),
    "continuing.cancel_twice": ("F04", "F16"),
    "continuing.clear": ("F17",),
    "continuing.deny_feedback": ("F08", "F18"),
    "continuing.follow_up": ("F08",),
    "continuing.fork": ("F11", "F17"),
    "continuing.restart": ("F08",),
    "continuing.two_follow_ups": ("F08",),
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
    "incident.6fa3e09e": ("F04", "F16"),
    "paused.cancel": ("F04", "F16"),
    "paused.cancel_twice": ("F04", "F16"),
    "paused.clear": ("F13",),
    "paused.deny_feedback": ("F08",),
    "paused.follow_up": ("F06", "F08"),
    "paused.fork": ("F11",),
    "paused.restart": ("F12",),
    "paused.two_follow_ups": ("F06", "F08"),
    "runaway.unattended": ("F15",),
    "secret.bypass.dotenv": ("F14",),
    "streaming.cancel": ("F04", "F16"),
    "streaming.cancel_twice": ("F04", "F16"),
    "streaming.deny_feedback": ("F08", "F18"),
    "subagent.approve": ("F07",),
    "subagent.cancel": ("F04", "F16"),
    "subagent.cancel_twice": ("F04", "F16"),
    "subagent.deny": ("F07",),
    "subagent.deny_feedback": ("F07", "F08", "F18"),
    "subagent.follow_up": ("F07", "F08"),
    "subagent.reconnect": ("F07",),
    "subagent.reload": ("F07",),
    "subagent.restart": ("F08",),
    "subagent.second_window": ("F07",),
    "subagent.switch_model": ("F07",),
    "subagent.two_follow_ups": ("F07", "F08"),
    "tool.cancel": ("F04", "F16"),
    "tool.cancel_twice": ("F04", "F16"),
    "tool.deny_feedback": ("F08", "F18"),
    "tool.follow_up": ("F08", "F18"),
    "tool.fork": ("F11",),
    "tool.two_follow_ups": ("F08", "F18"),
}
