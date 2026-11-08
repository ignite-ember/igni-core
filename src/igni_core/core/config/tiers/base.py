"""``Tier`` — one precedence layer in the settings merge."""

from __future__ import annotations

from igni_core.core.config.accumulator import SettingsAccumulator


class Tier:
    """Base polymorphic tier — one precedence layer in the merge stack.

    Subclasses override :meth:`apply` with the tier-specific merge
    logic. The default no-op implementation exists so a plan with
    a "skipped" tier just returns the accumulator unchanged.
    """

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        return accumulator
