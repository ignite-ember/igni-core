"""The answer for one folder: what was decided, and why.

A :class:`TrustDecision` is the whole of what the trust service says —
the state, where the answer came from, and the reason a prompt or a
log line shows. It is a plain model rather than a class with behaviour,
because every consumer reads it and none of them changes it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from igni_core.core.workspace_trust.identity import WorkspaceIdentity

#: Whether a folder may run its own code.
DecisionState = Literal["trusted", "untrusted", "ask"]

#: Where the answer came from: the organisation's policy, this run, the
#: person's recorded decision, or nowhere yet.
DecisionSource = Literal["policy", "run", "store", "unset"]

#: What an untrusted folder does not get, for prompts and logs.
GATED_SUMMARY = (
    "project tools, hooks, workflows, language servers, monitors, plugins, "
    "markdown-command shell tokens, and its permission and safety settings"
)


class TrustDecision(BaseModel):
    """The answer for one folder, with enough to explain it."""

    identity: WorkspaceIdentity
    state: DecisionState
    source: DecisionSource
    #: Whether the person may trust this folder at all (policy permits it).
    can_trust: bool = True
    #: Human-readable reason, shown in prompts and the ``igni trust`` output.
    reason: str = ""
    policy_mode: str = "ask"

    @property
    def trusted(self) -> bool:
        return self.state == "trusted"

    @property
    def needs_answer(self) -> bool:
        return self.state == "ask"


__all__ = [
    "GATED_SUMMARY",
    "DecisionSource",
    "DecisionState",
    "TrustDecision",
]
