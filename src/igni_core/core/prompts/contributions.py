"""What extensions have done to the agent's prompts, right now.

For each prompt — the main prompt, plan mode, any agent — an extension
either **overrides** it with a whole prompt of its own, or **edits**
it: appends a section, inserts one before or after a named section, or
replaces a named section (:mod:`igni_core.core.prompts.document`).
Each may hold only while a condition the extension reports holds ("my
index covers this commit"). This is the table all of that is registered
into, and the prompt builder, the plan-mode block and the agent pool
read from.

``core`` owns it and the extension system registers into it, which
keeps the dependency pointing the right way: ``core`` does not import
the extension system, and knows no extension by name.

## One table per process, injected

There is one table per backend process, because an extension is
installed into a deployment: a second session must not see a different
set of contributions. It is **injected**, not global: whoever wires the
backend (:mod:`igni_core.backend.app`) creates it and hands the same
instance to the session, the extension system and the pool. A session
built on its own — the CLI, a test — gets a table of its own, empty.

**Conditions are reported, not polled.** An extension's process says
``notifications/igni/conditions`` when one changes; the table records
it and tells whoever watches, which rebuilds the agent.

What an override and its edits turn into is
:mod:`igni_core.core.prompts.composition`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.prompts.composition import PromptComposer
from igni_core.core.prompts.contribution_models import (
    Composition,
    PromptEdit,
    PromptOverride,
)
from igni_core.core.prompts.document import PromptTarget

logger = logging.getLogger(__name__)


class WatchFailure(BaseModel):
    """A watcher that raised instead of doing what it was asked."""

    model_config = ConfigDict(frozen=True)

    #: The watcher's name, as it says it.
    watcher: str
    #: What it raised, as text.
    error: str


class TableChange(BaseModel):
    """What one change to the table did.

    A watcher that failed is reported here, not swallowed: the caller
    that changed the table is the one that can say the agents may not
    have been rebuilt.
    """

    model_config = ConfigDict(frozen=True)

    #: Whether anything actually moved.
    changed: bool = False
    #: The watchers that failed, if any.
    failures: list[WatchFailure] = Field(default_factory=list)


class _Owner(BaseModel):
    """What one extension does to the prompts, right now."""

    name: str
    overrides: list[PromptOverride] = Field(default_factory=list)
    edits: list[PromptEdit] = Field(default_factory=list)
    holding: frozenset[str] = frozenset()

    def live(self, when: str | None) -> bool:
        """Whether this owner's contribution applies: unconditional, or its condition holds."""
        return when is None or when in self.holding


class PromptContributions:
    """Every live override and edit, by the extension that made it."""

    def __init__(self) -> None:
        self._owners: dict[str, _Owner] = {}
        self._priority: list[str] = []
        self._watchers: list[Callable[[], None]] = []

    # ── Registering ────────────────────────────────────────────────

    def set_owner(
        self,
        owner: str,
        name: str,
        *,
        overrides: list[PromptOverride] | None = None,
        edits: list[PromptEdit] | None = None,
    ) -> TableChange:
        """What ``owner`` does now, replacing what it did before.

        Conditions already reported are kept: a restart re-registers
        what it does and its process re-reports anyway.
        """
        previous = self._owners.get(owner)
        holding = previous.holding if previous is not None else frozenset()
        self._owners[owner] = _Owner(
            name=name, overrides=overrides or [], edits=edits or [], holding=holding
        )
        return self._changed()

    def remove_owner(self, owner: str) -> TableChange:
        """Forget what ``owner`` does."""
        if self._owners.pop(owner, None) is None:
            return TableChange()
        return self._changed()

    def set_priority(self, priority: list[str]) -> TableChange:
        """The super admin's priority order of extensions, highest first: edits apply in it."""
        if priority == self._priority:
            return TableChange()
        self._priority = list(priority)
        return self._changed()

    def hold(self, owner: str, conditions: dict[str, bool]) -> TableChange:
        """Record which of ``owner``'s conditions hold."""
        entry = self._owners.get(owner)
        holding = set(entry.holding) if entry is not None else set()
        for name, holds in conditions.items():
            (holding.add if holds else holding.discard)(name)
        if entry is None:
            # Reported before what it does arrived — a process that
            # starts quickly. Kept for when it does.
            self._owners[owner] = _Owner(name=owner, holding=frozenset(holding))
            return TableChange()
        if frozenset(holding) == entry.holding:
            return TableChange()
        self._owners[owner] = entry.model_copy(update={"holding": frozenset(holding)})
        return self._changed()

    def watch(self, callback: Callable[[], None]) -> None:
        """Be told whenever what the prompts say may have changed."""
        self._watchers.append(callback)

    def _changed(self) -> TableChange:
        """Tell every watcher that something changed.

        A watcher is arbitrary code, so it may fail; one that does must
        not stop the others, and its failure is reported to whoever
        changed the table rather than swallowed here.
        """
        failures: list[WatchFailure] = []
        for watcher in list(self._watchers):
            try:
                watcher()
            except Exception as exc:  # noqa: BLE001 — a watcher is arbitrary code; its failure is reported, not raised
                failure = WatchFailure(watcher=_watcher_name(watcher), error=str(exc))
                failures.append(failure)
                logger.exception("a prompt-contributions watcher failed: %s", failure.watcher)
        return TableChange(changed=True, failures=failures)

    # ── Reading ────────────────────────────────────────────────────

    def _ordered(self) -> list[str]:
        """The owners, in the priority order: the ones named first come first."""
        rank = {owner: index for index, owner in enumerate(self._priority)}
        return sorted(self._owners, key=lambda owner: (rank.get(owner, len(rank)), owner))

    def override(self, target: str) -> tuple[str, PromptOverride] | None:
        """The override in force for ``target``, and whose it is."""
        held: tuple[str, PromptOverride] | None = None
        for owner in self._ordered():
            entry = self._owners[owner]
            for override in entry.overrides:
                if override.target != target or not entry.live(override.when):
                    continue
                if held is None:
                    held = (owner, override)
                else:
                    logger.warning(
                        "%s and %s both override %s; %s keeps it — resolve the collision "
                        "on igni server's Services page",
                        held[0],
                        owner,
                        target,
                        held[0],
                    )
        return held

    def edits(self, target: str) -> list[tuple[str, PromptEdit]]:
        """The edits in force for ``target``, in the order they apply."""
        return [
            (owner, edit)
            for owner in self._ordered()
            for edit in self._owners[owner].edits
            if edit.target == target and self._owners[owner].live(edit.when)
        ]

    def agents(self) -> list[str]:
        """Every agent an extension overrides or edits right now."""
        names = {
            PromptTarget.parse(item.target).agent
            for entry in self._owners.values()
            for item in [*entry.overrides, *entry.edits]
            if item.target.startswith("agent:") and entry.live(item.when)
        }
        return sorted(name for name in names if name is not None)

    def compose(
        self,
        target: str,
        base: str | None,
        *,
        prepare: Callable[[str], str] | None = None,
    ) -> Composition | None:
        """``target`` as extensions have shaped it.

        ``base`` is igni's own text, or ``None`` when igni has none —
        an agent only an extension defines. See
        :class:`igni_core.core.prompts.composition.PromptComposer`.
        """
        return PromptComposer(
            target=target,
            base=base,
            override=self.override(target),
            edits=self.edits(target),
            prepare=prepare,
        ).compose()


def _watcher_name(watcher: Callable[[], None]) -> str:
    """A name for a watcher, for the log and for the report to the caller."""
    return getattr(watcher, "__qualname__", None) or repr(watcher)


__all__ = ["PromptContributions", "TableChange", "WatchFailure"]
