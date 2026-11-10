"""What a hook load produced: the registry and the warnings on the way."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import HookLoadWarning


class HookLoadResult(BaseModel):
    """Outcome of a :meth:`HookLoader.load` (or plugin-hooks) call.

    Pairs the populated :class:`HookRegistry` with any structured
    warnings that surfaced during parsing. Callers unwrap
    ``.registry`` to attach the hooks to a session and log
    ``.warnings`` through whatever channel is appropriate.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry: HookRegistry
    warnings: list[HookLoadWarning] = Field(default_factory=list)

    def merge(self, other: HookLoadResult) -> HookLoadResult:
        """Fold *other* into this result and return a fresh instance.

        The two registries share the SAME underlying dict — plugin
        loads mutate the settings-load registry in place — so this
        method's job is really just to concatenate warning lists.
        The returned :class:`HookLoadResult` references the shared
        registry so downstream mutation on either side stays
        visible.
        """
        return HookLoadResult(
            registry=self.registry,
            warnings=[*self.warnings, *other.warnings],
        )
