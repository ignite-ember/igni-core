"""The developer's remembered answers about the group policy's hooks.

Read and written by :class:`~igni_core.core.hooks.group_hook_gate.GroupHookGate`
when ``hooks.group`` is ``ask``. Kept by hook digest, so a hook the group
changes is a hook nobody has answered about yet.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.paths import home_config_dir

logger = logging.getLogger(__name__)

GroupHookDecision = Literal["run", "refuse"]


class GroupHookDecisionRecord(BaseModel):
    """One answer: what the hook did, where it came from, and when it was answered."""

    decision: GroupHookDecision
    what: str
    source: str
    decided_at: datetime

    @classmethod
    def now(cls, decision: GroupHookDecision, hook: HookDefinition) -> GroupHookDecisionRecord:
        return cls(
            decision=decision,
            what=hook.describe(),
            source=hook.group_source,
            decided_at=datetime.now(UTC).replace(microsecond=0),
        )


class GroupHookDecisionsFile(BaseModel):
    """``group-hook-decisions.json``: every answer, by hook digest.

    Entries stay raw until looked up, so one hand-edited bad entry costs only
    its own answer and is written back untouched.
    """

    version: int = 1
    hooks: dict[str, Any] = Field(default_factory=dict)

    def decision(self, digest: str) -> GroupHookDecision | None:
        """The answer kept for ``digest``, or ``None`` if there is none readable."""
        entry = self.hooks.get(digest)
        if entry is None:
            return None
        try:
            return GroupHookDecisionRecord.model_validate(entry).decision
        except ValidationError as exc:
            logger.warning("Ignoring the unreadable answer for %s (%s).", digest, exc)
            return None


class GroupHookDecisionStore(Protocol):
    """Where :class:`GroupHookGate` looks up and records answers."""

    def get(self, digest: str) -> GroupHookDecision | None: ...

    def set(self, digest: str, decision: GroupHookDecision, hook: HookDefinition) -> None: ...


class GroupHookDecisions:
    """The developer's answers about group hooks, by hook digest.

    Kept in ``~/.igni/group-hook-decisions.json`` — the developer's directory,
    which the group policy cache never writes to. Delete the file to be asked
    again about every hook.
    """

    FILENAME = "group-hook-decisions.json"

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def in_home(cls) -> GroupHookDecisions:
        """The developer's own file, under ``~/.igni``."""
        return cls(home_config_dir() / cls.FILENAME)

    def _read(self) -> GroupHookDecisionsFile:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return GroupHookDecisionsFile()
        except OSError as exc:
            logger.warning("Could not read %s (%s); asking again.", self.path, exc)
            return GroupHookDecisionsFile()
        try:
            return GroupHookDecisionsFile.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("Could not read %s (%s); asking again.", self.path, exc)
            return GroupHookDecisionsFile()

    def get(self, digest: str) -> GroupHookDecision | None:
        return self._read().decision(digest)

    def set(self, digest: str, decision: GroupHookDecision, hook: HookDefinition) -> None:
        stored = self._read()
        stored.hooks[digest] = GroupHookDecisionRecord.now(decision, hook).model_dump(mode="json")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".group-hooks-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(stored.model_dump_json(indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            # Not remembered means asked again next time — the safe side.
            logger.warning("Could not record the answer in %s: %s", self.path, exc)


class ForgetfulGroupHookDecisions:
    """Remembers nothing — for :meth:`GroupHookGate.unwired`."""

    def get(self, digest: str) -> GroupHookDecision | None:
        return None

    def set(self, digest: str, decision: GroupHookDecision, hook: HookDefinition) -> None:
        return None


__all__ = [
    "ForgetfulGroupHookDecisions",
    "GroupHookDecision",
    "GroupHookDecisionRecord",
    "GroupHookDecisionStore",
    "GroupHookDecisions",
    "GroupHookDecisionsFile",
]
