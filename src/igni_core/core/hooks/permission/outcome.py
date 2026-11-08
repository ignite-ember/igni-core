"""What one permission stage decided about a call."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Continue:
    """Stage passed — run the next stage."""


@dataclass(frozen=True)
class Block:
    """Stage stops the pipeline; ``message`` goes back to the model."""

    message: str


@dataclass(frozen=True)
class AllowSkip:
    """A ``PreToolUse`` hook allowed the call (CC's ``permissionDecision: allow``).

    The safety-list stages still run; the stages a hook's ``allow`` may
    skip (:attr:`PermissionStage.skipped_by_hook_allow`) do not.
    """


StageOutcome = Continue | Block | AllowSkip
"""Union alias used by every stage's ``evaluate`` return type."""
