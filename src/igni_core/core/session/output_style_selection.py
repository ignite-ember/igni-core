"""The session's output styles and which one is active."""

from __future__ import annotations

from igni_core.core.output_styles import OutputStyle


class OutputStyleSelection:
    """Single writer of the active output style."""

    def __init__(self, styles: dict[str, OutputStyle] | None = None, active: str = "") -> None:
        self.styles: dict[str, OutputStyle] = dict(styles or {})
        self._active = active

    @property
    def active(self) -> str:
        return self._active

    def adopt(self, discovered: dict[str, OutputStyle]) -> None:
        """Take a rediscovered set, keeping the person's choice if it survives.

        Falls back to ``default``, then the first by name, then nothing — an
        empty name rather than one naming a file this discovery found missing.
        """
        self.styles = discovered
        if self._active in discovered:
            return
        if "default" in discovered:
            self._active = "default"
        elif discovered:
            self._active = sorted(discovered)[0]
        else:
            self._active = ""

    def select(self, name: str) -> None:
        self._active = name


__all__ = ["OutputStyleSelection"]
