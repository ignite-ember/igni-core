"""The context window compaction is measured against, and the last run's input."""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.settings import Settings


class ContextBudget(BaseModel):
    """The effective window for the current model and the latest input-token count."""

    window: int
    last_input_tokens: int = 0

    @classmethod
    def for_settings(cls, settings: Settings) -> ContextBudget:
        return cls(window=cls._window_for(settings))

    def resize(self, settings: Settings) -> None:
        """Follow a model switch: the threshold must track the window the
        model actually has."""
        self.window = self._window_for(settings)

    def latch(self, n: int) -> None:
        """Record the input-token count of the most recent completed run."""
        self.last_input_tokens = n

    @staticmethod
    def _window_for(settings: Settings) -> int:
        # The same expression the agent builder uses for the compression
        # threshold, capped by ``models.max_context_window``.
        return min(
            ModelRegistry(settings).get_context_window(),
            settings.models.max_context_window,
        )


__all__ = ["ContextBudget"]
