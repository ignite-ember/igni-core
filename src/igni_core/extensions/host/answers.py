"""What an extension's answer means for the event it was asked about.

Four outcomes, one reading. Allow: the event goes ahead as it is.
Reject: it does not, and the reason says why. A change: it goes ahead
with what the extension left it — only the fields the event lets it
change, and only if what it sends fits. Failed: the extension's handler
raised, which the host reads as a silence, not as a refusal.

An answer the event cannot take is the extension's bug, not a fault: it
raises :class:`~igni_core.extensions.host.hook_exchange.HookFailure`
without the ``unanswered`` flag, so the extension is not counted as
faulty for answering badly.
"""

from __future__ import annotations

from pydantic import ValidationError

from igni_core.core.hooks.extension_gate import ExtensionHookVerdict
from igni_core.core.hooks.extension_spec import ExtensionHookSpec
from igni_core.core.hooks.extension_values import HookValueT
from igni_core.extensions.host.hook_exchange import HookFailure
from igni_core.extensions.host.wire import HookAnswer, HookFrameValue, Outcome


class HookAnswers:
    """An extension's answer, read for the event it was asked about."""

    @staticmethod
    def verdict(
        spec: ExtensionHookSpec,
        extension_id: str,
        current: HookValueT,
        answer: HookAnswer,
    ) -> ExtensionHookVerdict[HookValueT]:
        """What the answer does to the event: the value to go on with, or why it does not."""
        if answer.outcome is Outcome.ALLOW:
            return ExtensionHookVerdict(allowed=True, value=current)
        if answer.outcome is Outcome.FAILED:
            raise HookFailure(f"it failed: {answer.reason or 'no reason given'}", unanswered=True)
        if answer.outcome is Outcome.REJECT:
            reason = answer.reason.strip()
            if not reason:
                raise HookFailure("it rejected without saying why")
            return ExtensionHookVerdict(
                allowed=False,
                value=current,
                by=extension_id,
                reason=f"{reason} (the extension {extension_id})",
            )
        if not spec.modify:
            raise HookFailure(f"it changed {spec.event.value}, which has nothing to change")
        if not isinstance(answer.value, dict):
            raise HookFailure("its change is not the event's value")
        # Told, not part of the value: an answer echoing it changes nothing.
        proposed = {k: v for k, v in answer.value.items() if k not in HookFrameValue.ADDED}
        handed = current.model_dump(mode="json")
        stray = sorted(
            key
            for key, new in proposed.items()
            if key not in spec.modify and handed.get(key) != new
        )
        if stray:
            raise HookFailure(f"it changed {', '.join(stray)}, which cannot be changed")
        changed = dict(handed)
        for key in spec.modify:
            if key in proposed:
                changed[key] = proposed[key]
        try:
            return ExtensionHookVerdict(allowed=True, value=type(current).model_validate(changed))
        except ValidationError as exc:
            raise HookFailure(f"its change does not fit {spec.event.value}: {exc}") from exc


__all__ = ["HookAnswers"]
