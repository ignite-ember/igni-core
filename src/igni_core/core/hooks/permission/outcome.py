"""What one permission stage decided about a call.

Each outcome also says what decided it (``by``) and, when the permission
policy evaluator decided, its reason — read by the audit log
(``core/utils/tool_audit.py``). Neither takes part in equality: two
outcomes are the same decision whoever made it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from igni_core.core.utils.tool_audit import DecidedBy


@dataclass(frozen=True)
class Continue:
    """Stage passed — run the next stage.

    ``by`` is set when the stage positively allowed the call (a rule, a
    mode, the person), and left ``None`` when it had nothing to say.
    """

    by: DecidedBy | None = field(default=None, compare=False)
    reason: str = field(default="", compare=False)


@dataclass(frozen=True)
class Block:
    """Stage stops the pipeline; ``message`` goes back to the model."""

    message: str
    by: DecidedBy = field(default=DecidedBy.POLICY, compare=False)
    reason: str = field(default="", compare=False)


@dataclass(frozen=True)
class AllowSkip:
    """A ``PreToolUse`` hook allowed the call (CC's ``permissionDecision: allow``).

    The safety-list stages still run; the stages a hook's ``allow`` may
    skip (:attr:`PermissionStage.skipped_by_hook_allow`) do not.
    """


StageOutcome = Continue | Block | AllowSkip
"""Union alias used by every stage's ``evaluate`` return type."""


@dataclass(frozen=True)
class PermissionVerdict:
    """What the whole pipeline decided about one call, and what decided it.

    ``message`` is the block text the model reads, ``None`` when the call
    may run.
    """

    message: str | None
    by: DecidedBy
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.message is None
