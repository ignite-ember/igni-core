"""How one target's contributions become one prompt.

An override is the new starting point. Every other extension's edits
apply on top of it, in the super admin's priority order: an append
always lands; an insert or a replace lands when the text has the
section it names, and is skipped when it does not. An ``exclusive``
override takes no edits from anybody else.

**Who may do what is not decided here.** Two extensions replacing one
section, two overriding one prompt, an exclusive override against
someone's edits — those are collisions the priority order settles, and
:mod:`igni_core.extensions.impact` applies it before anything reaches
the table (:mod:`igni_core.core.prompts.contributions`). Should two
claims arrive anyway, the one higher in the order keeps it and the
other is logged — never a merge.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from igni_core.core.prompts.contribution_models import (
    Composition,
    EditOp,
    PromptEdit,
    PromptOverride,
)
from igni_core.core.prompts.document import (
    FRONTMATTER,
    EditRefused,
    PromptDocument,
    PromptKind,
    PromptTarget,
)

logger = logging.getLogger(__name__)


class PromptComposer:
    """Folds one target's override and edits into one prompt.

    Split out of :mod:`igni_core.core.prompts.contributions`: the table
    that holds the contributions is one thing, and the rules for folding
    them into a prompt are another.
    """

    def __init__(
        self,
        *,
        target: str,
        base: str | None,
        override: tuple[str, PromptOverride] | None,
        edits: list[tuple[str, PromptEdit]],
        prepare: Callable[[str], str] | None = None,
    ) -> None:
        """
        Args:
            target: What is being composed: ``main``, ``plan_mode``, ``agent:<name>``.
            base: igni's own text, or ``None`` when igni has none — an
                agent only an extension defines.
            override: The override in force, and whose it is.
            edits: The edits in force, in the order they apply, each
                with the extension that made it.
            prepare: Runs on whichever text is the starting point before
                it is read as sections, for includes like ``{{PERSISTING}}``
                whose sections belong to it.
        """
        self._target = target
        self._base = base
        self._override = override
        self._edits = edits
        self._prepare = prepare

    def compose(self) -> Composition | None:
        """The prompt as extensions have shaped it, or ``None`` when there is none."""
        starting = self._starting_text()
        if starting is None:
            return None
        composition = self._composition(starting)
        self._apply_edits(composition)
        return composition

    def _starting_text(self) -> str | None:
        """The text the composition starts from: the override's, or igni's own."""
        if self._override is not None:
            starting = self._override[1].text
        elif self._base is not None:
            starting = self._base
        else:
            return None
        if self._prepare is not None:
            starting = self._prepare(starting)
        return starting

    def _composition(self, starting: str) -> Composition:
        """The composition the edits apply to."""
        agent = self._is_agent()
        owner = self._override[0] if self._override is not None else None
        return Composition(
            document=PromptDocument(starting, agent=agent),
            override_owner=owner,
            override_path=self._override[1].path if self._override is not None else None,
            frontmatter_owner=owner if agent else None,
        )

    def _apply_edits(self, composition: Composition) -> None:
        """Every edit in force, in order; the ones that do not land say why."""
        # The sections the starting text had: an edit may only be
        # placed against one of them, never against a section an
        # earlier edit just added.
        anchors = set(composition.document.sections())
        replaced: dict[str, str] = {}
        for owner, edit in self._edits:
            refusal = self._refusal(owner, edit, anchors)
            if refusal is not None:
                composition.skipped.append(refusal)
                continue
            section = edit.section
            if edit.op is EditOp.REPLACE and section is not None and section in replaced:
                self._warn_collision(owner, section, replaced[section])
                continue
            try:
                composition.document = self._apply(composition.document, edit)
            except (EditRefused, ValueError) as exc:
                composition.skipped.append(f"{owner}: {edit.describe()} — {exc}")
                continue
            if edit.op is EditOp.REPLACE and edit.section is not None:
                replaced[edit.section] = owner
                if edit.section == FRONTMATTER and self._is_agent():
                    composition.frontmatter_owner = owner
        for line in composition.skipped:
            logger.info("prompt %s: %s", self._target, line)

    def _refusal(self, owner: str, edit: PromptEdit, anchors: set[str]) -> str | None:
        """Why this edit does not land, or ``None`` when it does."""
        override = self._override
        if override is not None and override[1].exclusive and owner != override[0]:
            return f"{owner}: {edit.describe()} — {override[0]} overrides it exclusively"
        if edit.op is not EditOp.APPEND and edit.section not in anchors:
            whose = f"{override[0]}'s override" if override is not None else "it"
            return f"{owner}: {edit.describe()} — {whose} has no section {edit.section!r}"
        return None

    def _warn_collision(self, owner: str, section: str, kept: str) -> None:
        """Two extensions replacing one section: the earlier one keeps it."""
        logger.warning(
            "%s and %s both replace %s#%s; %s keeps it — resolve the collision "
            "on igni server's Services page",
            kept,
            owner,
            self._target,
            section,
            kept,
        )

    def _is_agent(self) -> bool:
        """Whether the target is one of igni's agents."""
        return PromptTarget.parse(self._target).kind is PromptKind.AGENT

    @staticmethod
    def _apply(document: PromptDocument, edit: PromptEdit) -> PromptDocument:
        """The document with one edit applied."""
        if edit.op is EditOp.REPLACE and edit.section is not None:
            return document.replace(edit.section, edit.text)
        if edit.op is EditOp.INSERT and edit.section is not None and edit.position is not None:
            return document.insert(edit.section, edit.position, edit.name or "added", edit.text)
        return document.append(edit.name or "added", edit.text)
