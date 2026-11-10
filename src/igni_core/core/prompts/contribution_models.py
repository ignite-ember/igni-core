"""What one extension contributes to one prompt.

Two shapes, both owned by the extension system and filled in by it: a
whole prompt of its own for a target (:class:`PromptOverride`), or one
change to igni's own text (:class:`PromptEdit`). What came of them —
the prompt as extensions have shaped it — is :class:`Composition`.

The table these are registered into lives in
:mod:`igni_core.core.prompts.contributions`; how they are folded into a
prompt lives in :mod:`igni_core.core.prompts.composition`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.prompts.document import Position, PromptDocument


class EditOp(StrEnum):
    """What one edit does to a prompt."""

    #: A new section at the end.
    APPEND = "append"
    #: A new section before or after a named one.
    INSERT = "insert"
    #: A named section's text, replaced.
    REPLACE = "replace"


class PromptOverride(BaseModel):
    """One extension's whole prompt for one target."""

    model_config = ConfigDict(frozen=True)

    #: ``main``, ``plan_mode``, ``agent:<name>``.
    target: str
    text: str
    #: Takes no other extension's edits.
    exclusive: bool = False
    #: A condition the extension reports; ``None`` holds whenever it is ready.
    when: str | None = None
    #: Where the text was read from, when it is a file: an agent's
    #: definition is "from" there, which is how igni tells an
    #: extension's own agents (:mod:`igni_core.core.agents.tool_audience`).
    path: Path | None = None


class PromptEdit(BaseModel):
    """One change one extension makes to one target."""

    model_config = ConfigDict(frozen=True)

    target: str
    op: EditOp
    #: The section replaced, or the one an insert is placed against.
    section: str | None = None
    #: Before or after ``section``, for an insert.
    position: Position | None = None
    #: The new section's name, for an append or an insert.
    name: str | None = None
    text: str
    when: str | None = None

    def describe(self) -> str:
        """What the edit does, in one line, for the log and for the report."""
        if self.op is EditOp.REPLACE:
            return f"replacing {self.target}#{self.section}"
        if self.op is EditOp.INSERT and self.position is not None:
            return f"inserting {self.name} {self.position.value} {self.target}#{self.section}"
        return f"appending {self.name} to {self.target}"


class Composition(BaseModel):
    """One prompt as extensions have shaped it, before igni's own substitutions."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    document: PromptDocument
    #: The extension whose override this is built on, if any.
    override_owner: str | None = None
    #: The override's file, when it was one.
    override_path: Path | None = None
    #: The extension whose text the agent's frontmatter now is, if any.
    frontmatter_owner: str | None = None
    #: Edits that did not land, and why.
    skipped: list[str] = Field(default_factory=list)
