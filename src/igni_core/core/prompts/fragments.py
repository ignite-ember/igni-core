"""The anchors a prompt may contain, and who fills them.

A prompt that teaches an agent about something an extension provides
has to say what that something *is*, and the description belongs with
the extension rather than copied into the prompt. ``{{ANCHOR}}`` marks
where it goes; whoever owns the anchor supplies the text when the
prompt is read.

The base cannot import from a capability that may not be installed, so
the text is supplied by whoever owns the capability rather than
imported from it: one source, substituted in, cannot drift from the
thing it describes, and a capability that is absent leaves its anchor
for whoever fills it next instead of breaking the prompt.

:class:`PromptFragments` is that table. It is injected rather than
global, so what a prompt contains is decided by the wiring and not by
import order; :class:`~igni_core.core.prompts.loader.PromptLoader`
reads igni's prompt files through it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

#: What an anchor looks like in a prompt file.
#:
#: Upper snake case between double braces. Deliberately not a template
#: language: a prompt is read by whoever is debugging the agent's
#: behaviour, and every expression it can contain is something they
#: have to evaluate in their head before they know what the model saw.
ANCHOR = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


class FragmentSupplier(BaseModel):
    """One extension's claim on one anchor.

    ``supply`` is called when a prompt is read rather than when the
    anchor is claimed: the text an extension describes is often
    expensive to produce, and it is not needed until a prompt that
    wants it is actually read.
    """

    model_config = ConfigDict(frozen=True)

    #: The extension that owns the anchor.
    owner: str
    #: What fills the anchor when a prompt is read.
    supply: Callable[[], str]


class AnchorRegistration(BaseModel):
    """What one claim on an anchor did."""

    model_config = ConfigDict(frozen=True)

    #: Whether the anchor is now claimed.
    ok: bool
    #: Why it was refused, when it was.
    reason: str = ""


class PromptFragments:
    """Which anchors exist, and what each one expands to.

    One instance per process: an anchor claimed once by an extension
    fills wherever it appears — in a prompt, in a plan-mode block, in
    an agent's body — and every session of a deployment sees the same
    set of them.
    """

    def __init__(self) -> None:
        self._suppliers: dict[str, FragmentSupplier] = {}

    def register(self, anchor: str, owner: str, supply: Callable[[], str]) -> AnchorRegistration:
        """Claim ``anchor`` for ``owner``.

        Re-claiming an anchor the same owner already holds replaces its
        supplier, which is what an extension upgrade does. A different
        owner is refused: two extensions describing one thing would
        leave the prompt saying one of two things, and which one would
        depend on the order they happened to arrive in.
        """
        if not ANCHOR.fullmatch("{{" + anchor + "}}"):
            return AnchorRegistration(
                ok=False,
                reason=(
                    f"'{anchor}' is not a usable anchor name — upper snake case, "
                    "starting with a letter"
                ),
            )
        held = self._suppliers.get(anchor)
        if held is not None and held.owner != owner:
            return AnchorRegistration(
                ok=False, reason=f"'{anchor}' is already supplied by '{held.owner}'"
            )
        self._suppliers[anchor] = FragmentSupplier(owner=owner, supply=supply)
        return AnchorRegistration(ok=True)

    def remove_owner(self, owner: str) -> None:
        """Forget every anchor one extension supplied."""
        for anchor, supplier in [*self._suppliers.items()]:
            if supplier.owner == owner:
                del self._suppliers[anchor]

    def owner_of(self, anchor: str) -> str | None:
        """Who owns ``anchor``, if anybody does."""
        held = self._suppliers.get(anchor)
        return held.owner if held is not None else None

    def anchors(self) -> list[str]:
        """Every anchor claimed, in name order."""
        return sorted(self._suppliers)

    def expand(self, text: str) -> str:
        """Fill the anchors this table knows, and leave the rest alone.

        An anchor with no supplier is left exactly as it was: this is
        one pass over a prompt and not the last one, and the placeholders
        another pass fills — ``{{PERSISTING}}``, ``{{AGENT_CATALOG}}`` —
        are none of this table's business.

        A claimed anchor whose supplier fails is a different case:
        nothing else is going to fill it, and braces in front of a model
        are worse than silence, so it is removed and the failure is
        logged with its owner. One capability failing to describe
        itself must not take out every prompt in the process.
        """
        if "{{" not in text:
            return text

        def fill(match: re.Match[str]) -> str:
            anchor = match.group(1)
            supplier = self._suppliers.get(anchor)
            if supplier is None:
                # Somebody else's placeholder, or nobody's. Not ours to
                # judge, and not ours to delete.
                return match.group(0)
            try:
                return supplier.supply()
            except Exception as exc:  # noqa: BLE001 - one anchor, not every prompt
                logger.warning(
                    "prompt anchor %s (from %s) could not be filled; removed: %s",
                    anchor,
                    supplier.owner,
                    exc,
                )
                return ""

        return ANCHOR.sub(fill, text)


__all__ = [
    "ANCHOR",
    "AnchorRegistration",
    "FragmentSupplier",
    "PromptFragments",
]
